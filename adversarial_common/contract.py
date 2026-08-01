"""Parse spec.md ``ac-directive`` fenced blocks into validated directives.

A directive is a fenced code block whose info string is exactly ``ac-directive``
and whose body is a YAML 1.2 mapping binding an acceptance criterion to a
machine-enforceable check (a grep, a shell command, or a no-diff assertion).

The grammar, validation rules, and AC-binding/location rules are defined in
the contract-directive-parser spec. Every malformed directive surfaces as a
:class:`ParseError` naming the AC and a cause — nothing is silently dropped.
"""

from __future__ import annotations

import hashlib
import os
import re
import select
import shlex
import signal
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Final

import yaml

from .gates import TRUNCATION_MARKER


# -- YAML 1.2 core-schema loader --------------------------------------------
# PyYAML resolves plain scalars under YAML 1.1 rules, but the directive
# grammar requires YAML 1.2: under 1.1 ``yes/no/on/off`` are booleans and
# ``1:30`` is the sexagesimal integer 90. _YAML12Loader restores the 1.2 core
# schema by dropping the 1.1 bool/int/float/timestamp implicit resolvers and
# re-adding 1.2 versions, so those scalars stay strings until field-level
# validation decides.
class _YAML12Loader(yaml.SafeLoader):
    pass


_YAML12_DROP_TAGS = frozenset({
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
})

# Give the subclass its OWN (filtered) resolver table copied from the
# inherited one, so the global SafeLoader/Resolver table used elsewhere
# (jsonio, providers) keeps its YAML 1.1 behavior.
_YAML12Loader.yaml_implicit_resolvers = {
    ch: [
        (tag, regex)
        for tag, regex in entries
        if tag not in _YAML12_DROP_TAGS
    ]
    for ch, entries in _YAML12Loader.yaml_implicit_resolvers.items()
}

# YAML 1.2 core schema: plain ints/floats only, true/false-only booleans —
# no sexagesimal and no yes/no/on/off.
_YAML12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    "tTfF",
)
_YAML12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    "-+0123456789",
)
_YAML12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
    ),
    "-+0123456789.",
)

__all__ = [
    "Directive",
    "MAX_DIRECTIVE_OUTPUT",
    "ParseError",
    "ParseResult",
    "parse_spec",
    "parse_spec_file",
    "run_directive",
]


# -- schema constants --------------------------------------------------------

_VALID_KINDS: Final = frozenset({"grep", "shell", "no-diff"})
_DEFAULT_TIMEOUT: Final = 60
_DEFAULT_FILES: Final = ("*",)
_FILES_KINDS: Final = frozenset({"grep", "no-diff"})
_INFO_STRING: Final = "ac-directive"

# A markdown fence opener: optional indent, three backticks, then the info
# string (the rest of the line). Tilde fences are intentionally not treated as
# directive containers — the spec uses backtick fences only.
_FENCE_RE: Final = re.compile(r"^[ \t]*```(.*)$")

# An acceptance-criterion bullet, e.g. ``- AC1: ...``. The id is captured.
_AC_BULLET_RE: Final = re.compile(r"^[ \t]*[-*+]\s+(AC\d+)\b")


# -- result types ------------------------------------------------------------

@dataclass(frozen=True)
class Directive:
    """One validated, AC-bound directive."""

    ac: str
    kind: str
    command: bytes
    expected: Any | None = None
    timeout: int = _DEFAULT_TIMEOUT
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParseError:
    """A single validation failure attributed to an AC (when identifiable)."""

    ac: str | None
    cause: str


@dataclass
class ParseResult:
    """Collected directives and errors from one ``parse_spec`` call."""

    directives: list[Directive] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# -- public API --------------------------------------------------------------

def parse_spec(markdown: str) -> ParseResult:
    """Parse *markdown* (the full text of a spec.md) into directives.

    Returns a :class:`ParseResult` collecting every well-formed directive and
    every parse error. Errors are never raised: a bad directive becomes a
    :class:`ParseError` so the caller can report it.
    """
    result = ParseResult()
    known_acs = _collect_ac_ids(markdown)

    current_ac: str | None = None
    current_ac_indent: int = -1
    in_fence = False
    fence_info = ""
    fence_body: list[str] = []

    for line in markdown.splitlines():
        fence_match = _FENCE_RE.match(line)
        if in_fence:
            if fence_match is not None:
                # closing fence — body lines collected so far are the content.
                _process(fence_info, fence_body, current_ac, known_acs, result)
                in_fence = False
                fence_body = []
            else:
                fence_body.append(line)
            continue

        if fence_match is not None:
            # opening fence: info string is whatever follows the backticks.
            fence_info = fence_match.group(1).strip()
            in_fence = True
            fence_body = []
            continue

        # Outside a fence, AC bullets set the placement cursor. The cursor
        # must expire once content leaves the bullet's indentation scope — a
        # later section heading, a sibling/non-AC bullet, or top-level prose
        # — otherwise a directive in an appendix stays bound to the last AC
        # and passes the within/after check. Blank lines and more-indented
        # continuation lines stay within the bullet's scope.
        bullet = _AC_BULLET_RE.match(line)
        if bullet is not None:
            current_ac = bullet.group(1)
            current_ac_indent = len(line) - len(line.lstrip(" \t"))
        elif current_ac is not None and line.strip():
            if len(line) - len(line.lstrip(" \t")) <= current_ac_indent:
                current_ac = None

    if in_fence:
        # ponytail: an unterminated directive fence is a structural error
        # rather than silently swallowed content.
        _process(fence_info, fence_body, current_ac, known_acs, result)

    return result


def parse_spec_file(path: str | Any) -> ParseResult:
    """Convenience wrapper: read a spec.md from *path* and parse it."""
    return parse_spec(_read_text(path))


# -- scanning helpers --------------------------------------------------------

def _collect_ac_ids(markdown: str) -> set[str]:
    """First pass: every AC id declared by a bullet, ignoring fence interiors."""
    ids: set[str] = set()
    in_fence = False
    for line in markdown.splitlines():
        is_fence = _FENCE_RE.match(line) is not None
        if is_fence:
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        bullet = _AC_BULLET_RE.match(line)
        if bullet is not None:
            ids.add(bullet.group(1))
    return ids


def _process(info, body_lines, placement_ac, known_acs, result):
    """Validate one fenced block, appending a Directive or ParseError."""
    # A fence that is not an ac-directive container is just ordinary content.
    if not info:
        return
    first_token = info.split()[0] if info.split() else ""

    if first_token != _INFO_STRING:
        return
    if info != _INFO_STRING:
        result.errors.append(
            ParseError(placement_ac, f"bad info string: {info!r}")
        )
        return

    # ponytail: reconstruct each line WITH its terminator rather than
    # "\n".join — the latter drops the last line's newline, and a trailing
    # YAML block scalar (clip chomping) would then lose its final line break
    # and silently stop round-tripping verbatim.
    body = "".join(line + "\n" for line in body_lines)
    try:
        data = yaml.load(body, Loader=_YAML12Loader)
    except yaml.YAMLError as exc:
        result.errors.append(
            ParseError(placement_ac, f"malformed YAML: {exc}")
        )
        return

    if data is None:
        result.errors.append(ParseError(placement_ac, "empty directive body"))
        return
    if not isinstance(data, dict):
        result.errors.append(
            ParseError(placement_ac, "directive body is not a YAML mapping")
        )
        return

    # Attribute errors to the declared ac when it is readable, else placement.
    ac = data.get("ac")
    err_ac = ac if isinstance(ac, str) and ac else placement_ac

    kind = data.get("kind")
    if kind not in _VALID_KINDS:
        cause = "missing kind" if kind is None else f"unknown kind: {kind!r}"
        result.errors.append(ParseError(err_ac, cause))
        return

    if not isinstance(ac, str) or not ac:
        result.errors.append(ParseError(placement_ac, "missing ac"))
        return

    command = data.get("command")
    if command is None:
        result.errors.append(ParseError(ac, "missing command"))
        return
    if not isinstance(command, str):
        result.errors.append(ParseError(ac, "command must be a string"))
        return

    if "expected" in data and not _legal_expected(kind, data["expected"]):
        result.errors.append(
            ParseError(ac, f"illegal expected for kind {kind!r}")
        )
        return

    timeout = data.get("timeout", _DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        result.errors.append(ParseError(ac, f"invalid timeout: {timeout!r}"))
        return

    files = data.get("files")
    if files is not None:
        if kind not in _FILES_KINDS:
            result.errors.append(
                ParseError(ac, f"files not valid for kind {kind!r}")
            )
            return
        if not isinstance(files, list) or not all(isinstance(x, str) for x in files):
            result.errors.append(ParseError(ac, "files must be a list of strings"))
            return
        files_t = tuple(files)
    else:
        files_t = _DEFAULT_FILES if kind in _FILES_KINDS else ()

    # binding: the declared ac must exist in the spec.
    if ac not in known_acs:
        result.errors.append(ParseError(ac, f"unbound ac: {ac!r}"))
        return

    # location: the block must sit within/after its declared ac's bullet.
    if placement_ac != ac:
        where = (
            f"under {placement_ac!r}"
            if placement_ac is not None
            else "outside any AC bullet"
        )
        result.errors.append(
            ParseError(ac, f"misplaced directive: declared ac {ac!r} placed {where}")
        )
        return

    # at-most-one directive per (ac, kind).
    for prior in result.directives:
        if prior.ac == ac and prior.kind == kind:
            result.errors.append(
                ParseError(ac, f"duplicate directive for ({ac}, {kind})")
            )
            return

    result.directives.append(
        Directive(
            ac=ac,
            kind=kind,
            command=command.encode("utf-8"),
            expected=data.get("expected"),
            timeout=timeout,
            files=files_t,
        )
    )


def _legal_expected(kind: str, value: Any) -> bool:
    """Kind-specific legality for the optional ``expected`` field.

    grep  -> int (match count) | str (match text) | list[str] (matches)
    shell -> int (exit code) | str (stdout)
    no-diff -> not allowed (a no-diff assertion has nothing to expect)
    """
    if isinstance(value, bool):
        return False
    if kind == "shell":
        return isinstance(value, (int, str))
    if kind == "grep":
        if isinstance(value, (int, str)):
            return True
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    # no-diff: expected is illegal.
    return False


def _read_text(path) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# -- execution engine (P3) --------------------------------------------------
#
# run_directive executes one parsed Directive under fixed, pipeline-invariant
# semantics: cwd is repo_root; the command runs in a non-interactive POSIX sh
# with an explicit, documented env (no ambient secrets, no operator rc); a
# per-directive timeout fails fast instead of hanging; captured output is
# length-bounded and marked when truncated; a signal-terminated child is a
# recorded interruption, never a pass.

# Explicit, ordered PATH covering every standard binary location so a
# directive never depends on the operator's shell PATH.
_DIRECTIVE_PATH: Final = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# One bounded buffer for stdout+stderr. Exposed so callers/tests can generate
# output known to exceed it.
MAX_DIRECTIVE_OUTPUT: Final = 8192

# Non-interactive POSIX shell. ``-c`` reads no rc file, so no operator rc.
_POSIX_SH: Final = "/bin/sh"


def _directive_env(repo_root: str) -> dict[str, str]:
    """Fixed, documented command env: explicit PATH, no ambient leak.

    HOME/TMPDIR are passed through (not secrets, required by real tooling);
    everything else is pinned. ``sh`` is non-interactive and sources no rc, so
    the operator's shell config never reaches the directive.
    """
    return {
        "PATH": _DIRECTIVE_PATH,
        "HOME": os.environ.get("HOME", repo_root),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": "C",
        "LC_ALL": "C",
    }


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group so backgrounded work can't leak."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_shell(command: bytes, repo_root: str, timeout: int):
    """Run *command* (a sh script) in *repo_root* under the fixed env.

    Returns ``(stdout, stderr, returncode, timed_out, truncated,
    stdout_lines)``. Output is read incrementally and capped at
    :data:`MAX_DIRECTIVE_OUTPUT` bytes (stdout+stderr combined); anything past
    that is discarded with ``truncated`` set, so a noisy or infinite-output
    directive cannot grow memory unbounded — ``communicate`` is NOT used. A
    runaway that keeps writing after the cap simply blocks on the full pipe
    until the per-directive timeout fires and SIGKILLs the group.

    ``stdout_lines`` counts every ``\n`` in stdout, including discarded
    bytes, so a grep match-line count stays accurate even when output is
    truncated.
    """
    proc = subprocess.Popen(
        [_POSIX_SH],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=repo_root,
        env=_directive_env(repo_root),
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    out: bytearray = bytearray()
    err: bytearray = bytearray()
    truncated = False
    stdout_lines = 0
    budget = MAX_DIRECTIVE_OUTPUT

    if proc.stdin is not None:
        try:
            proc.stdin.write(command)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    pipes = [p for p in (proc.stdout, proc.stderr) if p is not None]
    timed_out = False
    try:
        while pipes:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            ready, _, _ = select.select(pipes, [], [], min(0.2, deadline - now))
            if not ready:
                continue
            for fd in ready:
                try:
                    chunk = os.read(fd.fileno(), 8192)
                except OSError:
                    chunk = b""
                if not chunk:
                    pipes.remove(fd)
                    continue
                if fd is proc.stdout:
                    stdout_lines += chunk.count(b"\n")
                used = len(out) + len(err)
                if not truncated:
                    if used >= budget:
                        truncated = True
                    else:
                        room = budget - used
                        target = out if fd is proc.stdout else err
                        if len(chunk) <= room:
                            target.extend(chunk)
                        else:
                            target.extend(chunk[:room])
                            truncated = True
                # past the budget: discard (newlines already counted above)
        if timed_out:
            _kill_group(proc)
        try:
            rc = proc.wait(
                timeout=5.0 if timed_out else max(0.0, deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            try:
                rc = proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                rc = proc.returncode
    except KeyboardInterrupt:
        # SIGINT to our own process while the child runs: clean up, then fail.
        _kill_group(proc)
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    return bytes(out), bytes(err), rc, timed_out, truncated, stdout_lines


def _signal_name(code: int) -> str:
    """Name the signal that terminated a child given a negative returncode."""
    try:
        return signal.Signals(-code).name
    except (ValueError, KeyError):
        return f"signal {-code}"


def _bound_output(stdout: bytes, stderr: bytes, truncated: bool = False) -> str:
    """Length-bound stdout+stderr, head-truncated with the shared marker.

    *truncated* is the reader's authoritative "more output was produced than
    fit the budget" signal; without it a capped capture would be shorter than
    the limit and the marker would never appear even though output was cut.
    """
    text = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    if err:
        text = text + "\n" + err if text else err
    limit = MAX_DIRECTIVE_OUTPUT
    if not truncated and len(text) <= limit:
        return text
    if limit == 0:
        return ""
    if limit <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:limit]
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _git_tracked(repo_root: str) -> list[str]:
    """git-tracked paths (NUL-separated), or [] when not a git repo."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            capture_output=True, cwd=repo_root, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


# A literal (no ``*?[]``) file entry is a real path, not a glob.
_GLOB_META: Final = re.compile(r"[*?\[\]]")


def _named_files(
    repo_root: str, files: tuple[str, ...], *, include_literal: bool = False
) -> list[str]:
    """Resolve the directive's named file set.

    ``("*",)`` (the default) or empty -> every tracked file. Otherwise tracked
    paths matching any glob in *files*.

    With *include_literal* (no-diff), a literal (non-glob) entry is part of the
    named set even when currently untracked or absent, so a directive that
    CREATES it shows up as a real diff. Globs only ever resolve to currently
    tracked paths — a wildcard cannot be expanded to files that do not exist
    yet, so creation under a default ``*`` is out of scope by design.
    """
    tracked = _git_tracked(repo_root)
    if not files or files == ("*",):
        return tracked
    matched: list[str] = []
    seen: set[str] = set()
    for path in tracked:
        if any(fnmatch(path, pat) for pat in files) and path not in seen:
            seen.add(path)
            matched.append(path)
    if include_literal:
        for pat in files:
            if _GLOB_META.search(pat):
                continue
            if pat not in seen:
                seen.add(pat)
                matched.append(pat)
    return matched


def _fingerprint(path: str) -> str | None:
    """``"x|-":sha256`` of a file, or None when missing/unreadable.

    The executable bit is folded in (git tracks 100644 vs 100755), so a
    mode-only change (``chmod +x``) is a diff even when the content hash is
    unchanged.
    """
    try:
        st = os.stat(path)
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None
    exe = "x" if (st.st_mode & 0o100) else "-"
    return f"{exe}:{digest}"


def _snapshot(repo_root: str, named: list[str]) -> dict[str, str | None]:
    """``{relpath: fingerprint}`` over the named set (missing -> None)."""
    return {rel: _fingerprint(os.path.join(repo_root, rel)) for rel in named}


def _diff_names(before: dict[str, str | None], after: dict[str, str | None]) -> list[str]:
    """Sorted named paths whose fingerprint changed, appeared, or disappeared."""
    return sorted(
        rel for rel in before.keys() | after.keys()
        if before.get(rel) != after.get(rel)
    )


def _grep_head(command: bytes) -> list[str]:
    """Grep program + options (+ option args) + pattern, with file operands dropped.

    The engine is the SOLE source of the grep search set: a directive's
    ``command`` is ``grep [flags] PATTERN`` and ``directive.files`` names what
    is searched, so file operands baked into the command must be REMOVED, not
    kept. Leaving them in unions them with the named set and lets an excluded
    file satisfy the directive (A2) or double-count a file under ``-c`` (A5).

    Options that consume their following token (short ``e``/``f``/``m``/
    ``A``/``B``/``C`` and their ``--regexp``/``--file``/... long forms) take
    that token as their argument so it is not mistaken for the pattern.
    ``-e``/``--regexp`` supply the pattern themselves, so once one is seen
    there is no positional pattern and every later positional is a file
    (dropped). ponytail: an ``-A2``-style combined flag-arg and a literal
    ``--`` mid-command are not modeled; directives are flags + pattern +
    optional trailing files.
    """
    try:
        tokens = shlex.split(command.decode("utf-8", "replace"))
    except ValueError:
        # unbalanced quotes: leave the raw command untouched rather than guess.
        return [command.decode("utf-8", "replace")]
    if not tokens:
        return []
    head: list[str] = [tokens[0]]  # argv[0] is the program (grep), never a pattern
    pattern_seen = False
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            head.append(tok)  # keep the option/positional separator
            i += 1
            break
        if not pattern_seen and tok.startswith("-") and len(tok) > 1:
            head.append(tok)
            via_regexp = False
            if tok.startswith("--"):
                name = tok[2:].split("=", 1)[0]
                via_regexp = name == "regexp"
                if "=" not in tok and name in _GREP_LONG_ARG_OPTS and i + 1 < n:
                    head.append(tokens[i + 1])
                    i += 2
                else:
                    i += 1
                pattern_seen = pattern_seen or via_regexp
                continue
            elif any(ch in tok[1:] for ch in _GREP_ARG_OPTS) and i + 1 < n:
                if "e" in tok[1:]:
                    via_regexp = True
                head.append(tokens[i + 1])
                i += 2
                pattern_seen = pattern_seen or via_regexp
                continue
            i += 1
            continue
        # positional: first unseen is the pattern; the rest are files (dropped)
        if not pattern_seen:
            head.append(tok)
            pattern_seen = True
        i += 1
    # positionals after a mid-command `--`: first is the pattern, rest are files
    while i < n:
        if not pattern_seen:
            head.append(tokens[i])
            pattern_seen = True
        i += 1
    return head


def _append_files(command: bytes, named: list[str]) -> bytes:
    """Replace the grep command's file operands with the named file set.

    Strips file operands from *command* via :func:`_grep_head`, then appends
    the named set (default: every tracked file) after a ``--`` separator, so
    ``directive.files`` is the authoritative search set: a path baked into the
    command can neither leak an excluded file (A2) nor be double-counted under
    ``-c`` (A5).
    """
    head = shlex.join(_grep_head(command)).encode("utf-8", "replace")
    if not named:
        return head
    return head + b" -- " + b" ".join(shlex.quote(n).encode() for n in named)


# grep short flags whose group consumes the following argv token as an
# argument (so a value like ``-c`` is not misread as the count flag); ``e``
# also marks the regexp as supplied-via-option in :func:`_grep_head`.
_GREP_ARG_OPTS: Final = "efmABC"
# long options whose following token is an argument rather than a file operand.
_GREP_LONG_ARG_OPTS: Final = frozenset({
    "regexp", "file", "max-count",
    "after-context", "before-context", "context",
    "binary-files", "devices", "directories", "label",
    "color", "colour",
})


def _grep_count_mode(command: bytes) -> bool:
    """True if *command* asks grep for count output (``-c`` / ``--count``).

    Scans shlex tokens up to the first ``--``: a ``--count`` long option or a
    short-flag group containing ``c`` (e.g. ``-c``, ``-cn``) switches grep to
    one-count-line-per-file output, where an integer ``expected`` must read
    the printed counts rather than count match lines. ``-e``/``-f``/``-m``/
    ``-A``/``-B``/``-C`` consume their following token so a pattern or number
    like ``-c`` is not mistaken for the flag. ponytail: exotic argv shapes (a
    file literally named ``-c`` passed after a bare pattern) are not modeled.
    """
    try:
        tokens = shlex.split(command.decode("utf-8", "replace"))
    except ValueError:
        return False
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            break
        if tok == "--count":
            return True
        if tok.startswith("--"):
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            if "c" in tok[1:]:
                return True
            if any(ch in tok[1:] for ch in _GREP_ARG_OPTS):
                i += 2  # this flag's argument occupies the next token
                continue
        i += 1
    return False


def _grep_count_total(stdout: bytes) -> int:
    """Sum the per-file counts ``grep -c`` prints (``N`` or ``path:N`` per line).

    Count-mode output is one short line per searched file, so it stays well
    under the capture cap in practice; the bounded *stdout* is therefore an
    accurate source (ceiling: thousands of files could truncate it).
    """
    total = 0
    for line in stdout.split(b"\n"):
        if not line.strip():
            continue
        tail = line.rsplit(b":", 1)[-1]
        if tail.isdigit():
            total += int(tail)
    return total


def _execute(command: bytes, repo_root: str, timeout: int):
    """Run *command* once; return bounded output + outcome.

    Returns ``(output, stdout, rc, timed_out, interrupt, stdout_lines)`` where
    *output* is the length-bounded stdout+stderr string, *stdout* the raw
    captured stdout bytes, and *stdout_lines* the total newline count in stdout
    (counting discarded bytes, so it is safe to use as a grep match-line
    count even when output was truncated). *interrupt* is a message string when
    the child was signal-terminated, else None.
    """
    stdout, stderr, rc, timed_out, truncated, stdout_lines = _run_shell(
        command, repo_root, timeout,
    )
    output = _bound_output(stdout, stderr, truncated)
    interrupt = f"interrupted: {_signal_name(rc)}" if rc is not None and rc < 0 else None
    return output, stdout, rc, timed_out, interrupt, stdout_lines


def run_directive(directive: "Directive", repo_root: str | Any) -> dict[str, Any]:
    """Execute one parsed *directive* in *repo_root* under fixed semantics.

    Returns ``{id, status, command, timed_out, message, truncated_output}``:

    * ``id`` — ``"{ac}:{kind}"``;
    * ``status`` — ``"pass"`` or ``"fail"``;
    * ``timed_out`` — True only when the per-directive timeout fired (fail);
    * ``message`` — short human reason (exit code, ``timeout``, interruption,
      grep present/absent, no-diff diff list);
    * ``truncated_output`` — length-bounded stdout+stderr, marked if cut.

    shell: ``expected`` int (default 0) -> exit code; str -> exact stdout.
    grep:  pattern over the named files (default all tracked); ``expected``
           None -> present, int -> match count (match lines, or the printed
           count under ``-c``/``--count``), str -> substring,
           list[str] -> every line present; absence / grep error is fail.
    no-diff: snapshot the named set (default all tracked; a literal named path
           counts even when untracked), run, re-snapshot; any content, mode, or
           existence change in the named set is a diff (fail). Untracked content
           outside the named set is ignored.
    """
    repo_root = str(repo_root)
    result: dict[str, Any] = {
        "id": f"{directive.ac}:{directive.kind}",
        "status": "fail",
        "command": directive.command.decode("utf-8", "replace"),
        "timed_out": False,
        "message": "",
        "truncated_output": "",
    }

    if directive.kind == "no-diff":
        named = _named_files(repo_root, directive.files, include_literal=True)
        before = _snapshot(repo_root, named)
        output, _stdout, rc, timed_out, interrupt, _lines = _execute(
            directive.command, repo_root, directive.timeout,
        )
        result["truncated_output"] = output
        if timed_out:
            result["timed_out"] = True
            result["message"] = f"timeout after {directive.timeout}s"
            return result
        if interrupt:
            result["message"] = interrupt
            return result
        if rc != 0:
            result["message"] = f"command exited {rc}"
            return result
        changed = _diff_names(before, _snapshot(repo_root, named))
        if changed:
            result["message"] = "diff: " + ", ".join(changed[:20])
            return result
        result["status"] = "pass"
        result["message"] = "no diff"
        return result

    if directive.kind == "grep":
        command = _append_files(
            directive.command, _named_files(repo_root, directive.files),
        )
    else:
        command = directive.command

    output, stdout, rc, timed_out, interrupt, stdout_lines = _execute(
        command, repo_root, directive.timeout,
    )
    result["truncated_output"] = output
    if timed_out:
        result["timed_out"] = True
        result["message"] = f"timeout after {directive.timeout}s"
        return result
    if interrupt:
        result["message"] = interrupt
        return result

    if directive.kind == "shell":
        exp = directive.expected
        if exp is None:
            ok = rc == 0
            result["message"] = f"exit {rc}" if ok else f"exit {rc}, expected 0"
        elif isinstance(exp, bool):
            ok = False
            result["message"] = "illegal expected"
        elif isinstance(exp, int):
            ok = rc == exp
            result["message"] = f"exit {rc}" if ok else f"exit {rc}, expected {exp}"
        else:  # str: expected is the required stdout (exact match)
            stdout_text = stdout.decode("utf-8", "replace")
            ok = stdout_text == exp
            result["message"] = "stdout match" if ok else "stdout mismatch"
        result["status"] = "pass" if ok else "fail"
        return result

    if directive.kind == "grep":
        present = rc == 0
        exp = directive.expected
        if exp is None:
            ok = present
        elif isinstance(exp, bool):
            ok = False
        elif isinstance(exp, int):
            # ponytail: the integer expectation is a MATCH COUNT. Default grep
            # prints one line per match, so the count is the newline tally over
            # the FULL stdout (counted past the capture budget, so a truncated
            # capture never miscounts). Under ``-c``/``--count`` grep prints a
            # per-file count line instead, so the count is read off the output.
            count = (
                _grep_count_total(stdout)
                if _grep_count_mode(directive.command)
                else stdout_lines
            )
            ok = present and count == exp
        elif isinstance(exp, str):
            ok = present and exp in output
        elif isinstance(exp, list):
            ok = present and all(
                isinstance(item, str) and item in output for item in exp
            )
        else:
            ok = False
        result["status"] = "pass" if ok else "fail"
        if present:
            result["message"] = (
                f"count {count}" if isinstance(exp, int) else "present"
            )
        elif rc == 1:
            result["message"] = "absent"
        else:
            result["message"] = f"grep error {rc}"
        return result

    result["message"] = f"unknown kind: {directive.kind!r}"
    return result


# -- self-check --------------------------------------------------------------

if __name__ == "__main__":
    _demo = """# Spec

## Acceptance criteria

- AC1: one
  ```ac-directive
  ac: AC1
  kind: grep
  command: grep foo bar
  ```
- AC2: two
"""

    res = parse_spec(_demo)
    assert res.ok, res.errors
    assert len(res.directives) == 1, res.directives
    d = res.directives[0]
    assert d.ac == "AC1" and d.kind == "grep" and d.command == b"grep foo bar"
    assert d.timeout == _DEFAULT_TIMEOUT and d.files == _DEFAULT_FILES

    # A1: YAML 1.2 plain-scalar typing — on/yes stay strings, 1:30 stays str.
    on_spec = (
        "# Spec\n\n## Acceptance criteria\n\n- AC1: one\n"
        "  ```ac-directive\n  ac: AC1\n  kind: shell\n  command: on\n  ```\n"
    )
    on_res = parse_spec(on_spec)
    assert on_res.ok, on_res.errors
    assert on_res.directives[0].command == b"on", on_res.directives

    bad_timeout = (
        "# Spec\n\n## Acceptance criteria\n\n- AC1: one\n"
        "  ```ac-directive\n  ac: AC1\n  kind: shell\n"
        "  timeout: 1:30\n  command: echo hi\n  ```\n"
    )
    bt_res = parse_spec(bad_timeout)
    assert not bt_res.ok and "timeout" in bt_res.errors[0].cause, bt_res.errors

    # A2: a directive in a later section is misplaced, not bound to AC1.
    appendix = (
        "# Spec\n\n## Acceptance criteria\n\n- AC1: one\n\n"
        "## Appendix\n\nprose\n\n"
        "```ac-directive\nac: AC1\nkind: shell\ncommand: echo hi\n```\n"
    )
    ap_res = parse_spec(appendix)
    assert not ap_res.ok and any(
        "misplaced" in e.cause for e in ap_res.errors
    ), ap_res.errors

    # Execution-engine fixes A1-A5, exercised in a throwaway git repo.
    import tempfile
    import subprocess as _sp

    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co",
    }
    full_env = {**os.environ, **env}

    with tempfile.TemporaryDirectory() as repo:
        _sp.run(["git", "init", "-q"], cwd=repo, check=True, env=full_env)
        with open(os.path.join(repo, "tracked.txt"), "w") as handle:
            handle.write("foo line\nfoo again\nbar line\n")
        _sp.run(["git", "add", "-A"], cwd=repo, check=True)
        _sp.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, env=full_env)

        def D(kind, command, **kw):
            return Directive(ac="AC1", kind=kind, command=command.encode(), **kw)

        # A1: a string shell expectation matches stdout (was always-fail before).
        assert run_directive(D("shell", "printf hello", expected="hello"), repo)["status"] == "pass"
        assert run_directive(D("shell", "printf hi", expected="hello"), repo)["status"] == "fail"

        # A2: grep consults the named files (default -> all tracked).
        assert run_directive(D("grep", "grep foo"), repo)["status"] == "pass"
        assert run_directive(D("grep", "grep missing"), repo)["status"] == "fail"
        # A2: the named set scopes the grep — a token present only outside it
        # is not seen, while the default set (all tracked) does see it.
        with open(os.path.join(repo, "scope_other.txt"), "w") as handle:
            handle.write("uniquetoken\n")
        _sp.run(["git", "add", "scope_other.txt"], cwd=repo, check=True)
        assert run_directive(
            D("grep", "grep uniquetoken", files=("tracked.txt",)), repo,
        )["status"] == "fail"
        assert run_directive(D("grep", "grep uniquetoken"), repo)["status"] == "pass"

        # A5: int expectation counts match lines (2 'foo' lines), not output
        # lines; and stays correct when output is truncated past the cap.
        # Command is pattern-only; ``files`` names the search set (A2).
        assert run_directive(D("grep", "grep foo", expected=2), repo)["status"] == "pass"
        assert run_directive(D("grep", "grep foo", expected=1), repo)["status"] == "fail"
        # A5: ``grep -c``/``--count`` print a per-file count, not match lines,
        # so an int expectation reads the printed count.
        assert run_directive(
            D("grep", "grep -c foo", expected=2, files=("tracked.txt",)), repo,
        )["status"] == "pass"
        assert run_directive(
            D("grep", "grep --count foo", expected=1, files=("tracked.txt",)), repo,
        )["status"] == "fail"
        # A2/A5 round 3: a file operand baked into the command is REPLACED by
        # the named set, not unioned with it. scope_other.txt holds uniquetoken
        # but is excluded when files=(tracked.txt,) -> absent even though the
        # command names it; and ``grep -c foo tracked.txt`` under the default
        # (all-tracked) set counts tracked.txt once (2), not twice (4).
        assert run_directive(
            D("grep", "grep uniquetoken scope_other.txt", files=("tracked.txt",)),
            repo,
        )["status"] == "fail"
        assert run_directive(
            D("grep", "grep -c foo tracked.txt", expected=2), repo,
        )["status"] == "pass"
        with open(os.path.join(repo, "many.txt"), "w") as handle:
            handle.write("foo\n" * 1500)
        _sp.run(["git", "add", "many.txt"], cwd=repo, check=True)
        big_count = run_directive(
            D("grep", "grep foo", expected=1500, files=("many.txt",)), repo,
        )
        assert big_count["status"] == "pass", big_count  # counted past the cap

        # A3: no-diff flags a newly created literal named path...
        created = run_directive(
            D("no-diff", "echo body > made.txt", files=("made.txt",)), repo,
        )
        assert created["status"] == "fail" and "made.txt" in created["message"], created
        # ...and a mode-only change on a tracked file.
        mode = run_directive(D("no-diff", "chmod +x tracked.txt"), repo)
        assert mode["status"] == "fail" and "tracked.txt" in mode["message"], mode

        # A4: captured output is truly bounded with the marker (not buffered
        # whole via communicate()).
        big = run_directive(
            D("shell", "awk 'BEGIN{for(i=0;i<20000;i++)printf \"x\"}'"), repo,
        )
        assert len(big["truncated_output"]) <= MAX_DIRECTIVE_OUTPUT
        assert TRUNCATION_MARKER in big["truncated_output"]

    print("contract.py self-check ok:", d)
