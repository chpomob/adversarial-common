"""Parse spec.md ``ac-directive`` fenced blocks into validated directives.

A directive is a fenced code block whose info string is exactly ``ac-directive``
and whose body is a YAML 1.2 mapping binding an acceptance criterion to a
machine-enforceable check (a grep, a shell command, or a no-diff assertion).

The grammar, validation rules, and AC-binding/location rules are defined in
the contract-directive-parser spec. Every malformed directive surfaces as a
:class:`ParseError` naming the AC and a cause — nothing is silently dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final

import yaml


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
    "ParseError",
    "ParseResult",
    "parse_spec",
    "parse_spec_file",
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

    print("contract.py self-check ok:", d)
