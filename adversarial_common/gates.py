"""Pre-flight context, size, complexity, and verification gates."""

from __future__ import annotations

import importlib
import os
import re
import shlex
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final


TRUNCATION_MARKER: Final = "\n[TRUNCATED]\n"

_DEFAULT_THRESHOLDS: Final[dict[str, dict[str, Any]]] = {
    "brief": {"min_chars": 40, "min_tokens": 10, "required_sections": [], "min_source_lines": 0},
    "spec": {
        "min_chars": 100,
        "min_tokens": 25,
        "required_sections": ["Requirements"],
        "min_source_lines": 0,
    },
    "diff": {"min_chars": 1, "min_tokens": 1, "required_sections": [], "min_source_lines": 1},
    "input": {"min_chars": 20, "min_tokens": 5, "required_sections": [], "min_source_lines": 0},
}
_SECTION_HEADING_RE: Final = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+)?(?P<name>[^\n:#]+?)\s*:?[ \t]*$", re.MULTILINE
)
_DIFF_FILE_RE: Final = re.compile(r"^\+\+\+\s+(?!/dev/null)(?:b/)?(.+)$", re.MULTILINE)
_DIFF_HUNK_RE: Final = re.compile(r"^@@(?:@)?\s", re.MULTILINE)
_REQUIREMENT_RE: Final = re.compile(
    r"^\s*(?:[-*]\s+)?(?:R(?:EQ)?[-_ ]?\d+|requirement\s+\d+)\s*[:.)-]",
    re.IGNORECASE | re.MULTILINE,
)
_PROJECT_MARKERS: Final = (
    ".git", "pyproject.toml", "setup.py", "package.json", "Cargo.toml",
    "go.mod", "Makefile", "CMakeLists.txt", "build.gradle", "build.gradle.kts",
)
_POST_KILL_DRAIN_TIMEOUT: Final = 1.0


def check_context(
    kind: str,
    input: str,  # noqa: A002 - public API name from the specification.
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate primary pipeline input before any provider is started."""
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("kind must be a non-empty string")
    if not isinstance(input, str):
        raise TypeError("input must be a string")
    effective = _effective_thresholds(kind.strip().lower(), thresholds)
    if not input.strip():
        return _context_result(False, "empty_input", effective)
    if len(input) < effective["min_chars"]:
        return _context_result(False, "below_min_chars", effective)
    if _estimate_tokens(input) < effective["min_tokens"]:
        return _context_result(False, "below_min_tokens", effective)
    headings = {
        match.group("name").strip().casefold()
        for match in _SECTION_HEADING_RE.finditer(input)
    }
    for section in effective["required_sections"]:
        scase = section.casefold()
        if not any(scase in heading for heading in headings):
            return _context_result(False, f"missing_required_section:{section}", effective)
    if effective["min_source_lines"]:
        if _count_diff_source_lines(input) < effective["min_source_lines"]:
            return _context_result(False, "below_min_source_lines", effective)
    return _context_result(True, "ok", effective)


def enforce_input_cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Head-truncate input to ``max_chars`` and add a visible marker."""
    return _enforce_cap(text, max_chars)


def enforce_output_cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Head-truncate provider output to ``max_chars`` with a visible marker."""
    return _enforce_cap(text, max_chars)


def estimate_complexity(
    input: str,  # noqa: A002 - public API name from the specification.
    diff_stats: Mapping[str, Any] | None = None,
    *,
    max_agents: int = 6,
) -> dict[str, Any]:
    """Return a deterministic score, tier, and bounded agent recommendation."""
    if not isinstance(input, str):
        raise TypeError("input must be a string")
    cap = _non_negative_int("max_agents", max_agents)
    if diff_stats is not None and not isinstance(diff_stats, Mapping):
        raise TypeError("diff_stats must be a mapping or None")
    stats = diff_stats or {}
    chars = len(input)
    parsed_files = len(set(_DIFF_FILE_RE.findall(input)))
    files = _stat_value(stats, ("files", "files_changed"), parsed_files)
    lines = _stat_value(
        stats, ("lines", "source_lines", "lines_changed"), _count_diff_source_lines(input)
    )
    hunks = _stat_value(stats, ("hunks",), len(_DIFF_HUNK_RE.findall(input)))
    requirements = len(_REQUIREMENT_RE.findall(input))
    modules = _stat_value(stats, ("modules", "modules_touched"), files)
    score = chars + lines * 40 + files * 400 + hunks * 200 + requirements * 300 + modules * 250
    if score < 1_000:
        level, recommended = "trivial", 1
    elif score < 5_000:
        level, recommended = "low", 2
    elif score < 20_000:
        level, recommended = "medium", 4
    else:
        level, recommended = "high", 6
    recommended = min(recommended, cap)
    summary = (
        f"{level}: {chars} chars, {files} files, {lines} source lines, "
        f"{hunks} hunks, {requirements} requirements, {modules} modules"
    )
    return {
        "score": score,
        "level": level,
        "recommended_agents": recommended,
        "stats": {
            "chars": chars, "files": files, "source_lines": lines,
            "hunks": hunks, "requirements": requirements, "modules": modules,
        },
        "tier": level,
        "max_agents": recommended,
        "summary": summary,
    }


def pre_build_gate(
    workdir: str | os.PathLike[str],
    command: str | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Check project markers and command resolution before a BUILD model call."""
    root = Path(workdir)
    result = _gate_base("pre_build", command)
    if not root.is_dir():
        return _gate_failure(result, 126, f"workdir is not a directory: {root}", True)
    markers = [marker for marker in _PROJECT_MARKERS if (root / marker).exists()]
    result["project_markers"] = markers
    if not markers:
        return _gate_failure(result, 2, "no recognized project marker found", False)
    if command is not None:
        _, resolved, error = _execution_command(command, root)
        result["resolved_executable"] = resolved or ""
        if error:
            return _gate_failure(result, 127, error, True)
    result.update({"ok": True, "exit_code": 0, "infra": False, "log": ""})
    return result


def post_build_gate(
    workdir: str | os.PathLike[str],
    command: str | list[str] | tuple[str, ...],
    *,
    timeout: int = 600,
    max_log_chars: int = 4000,
) -> dict[str, Any]:
    """Run configured verification after BUILD and return bounded evidence."""
    return _run_verification_gate("post_build", workdir, command, timeout, max_log_chars)


def post_fix_gate(
    workdir: str | os.PathLike[str],
    command: str | list[str] | tuple[str, ...],
    *,
    timeout: int = 600,
    max_log_chars: int = 4000,
) -> dict[str, Any]:
    """Run configured verification after FIX and return bounded evidence."""
    return _run_verification_gate("post_fix", workdir, command, timeout, max_log_chars)


def _run_verification_gate(
    name: str,
    workdir: str | os.PathLike[str],
    command: str | list[str] | tuple[str, ...],
    timeout: int,
    max_log_chars: int,
) -> dict[str, Any]:
    root = Path(workdir)
    result = _gate_base(name, command)
    timeout_value = _positive_number("timeout", timeout)
    log_cap = _non_negative_int("max_log_chars", max_log_chars)
    if not root.is_dir():
        return _gate_failure(result, 126, f"workdir is not a directory: {root}", True)
    execution_argv, resolved, error = _execution_command(command, root)
    result["resolved_executable"] = resolved or ""
    if error:
        return _gate_failure(result, 127, error, True)
    try:
        proc = subprocess.Popen(
            execution_argv,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return _gate_failure(result, 126, f"could not start gate: {exc}", True)
    try:
        log, _ = proc.communicate(timeout=timeout_value)
    except subprocess.TimeoutExpired:
        cleanup_error = _kill_process_group(proc)
        try:
            log, _ = proc.communicate(timeout=_POST_KILL_DRAIN_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            partial_output = exc.output or ""
            log = (
                partial_output.decode("utf-8", errors="replace")
                if isinstance(partial_output, bytes) else partial_output
            )
            cleanup_error = cleanup_error or (
                "gate process remained alive after cleanup; output drain timed out"
            )
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError as exc:
            log = ""
            cleanup_error = cleanup_error or f"could not collect gate output: {exc}"
        bounded, truncated = _bounded_log(log, log_cap)
        result.update({
            "ok": False, "exit_code": 124, "infra": True,
            "log": bounded, "truncated": truncated,
            "error": f"gate timed out after {timeout_value}s",
        })
        if cleanup_error:
            result["cleanup_error"] = cleanup_error
        return result
    bounded, truncated = _bounded_log(log, log_cap)
    result.update({
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "infra": False,
        "log": bounded,
        "truncated": truncated,
    })
    return result


_ALLOWLIST_PATH: Final = ".adversarial-gate/skip-allowlist.txt"

_PYTEST_TEST_LINE_RE: Final = re.compile(
    r"^(\S+)\s+(PASSED|FAILED|SKIPPED|ERROR)", re.MULTILINE
)


def _parse_pytest_output(log: str) -> dict[str, list[str]]:
    """Parse pytest-style verbose output into passed/failed/skipped test names.

    Returns ``{"passed": [...], "failed": [...], "skipped": [...]}``.
    """
    result: dict[str, list[str]] = {"passed": [], "failed": [], "skipped": []}
    for match in _PYTEST_TEST_LINE_RE.finditer(log):
        status = match.group(2).lower()
        key = "failed" if status in ("failed", "error") else status
        result[key].append(match.group(1))
    return result


def _load_allowlist(repo_root: Path) -> list[str] | None:
    """Load skip allow-list entries from *repo_root*.

    Returns a list of non-empty, non-comment substrings, or ``None`` when the
    file is missing (R3).
    """
    path = repo_root / _ALLOWLIST_PATH
    if not path.is_file():
        return None
    entries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    return entries


def _entry_matches(entry: str, name: str) -> bool:
    """Return True when *entry* matches *name* with bounded prefix semantics (A2).

    Entries ending with ``_`` or ``-`` are deliberate prefix markers and
    match any continuation.  Otherwise the character immediately after the
    matched substring must be a separator (``_``, ``.``, ``:``, ``/``,
    ``-``) or the end of *name*, preventing short entries like ``test_a``
    from silently widening to ``test_abc``.
    """
    idx = name.find(entry)
    if idx == -1:
        return False
    # Deliberate prefix marker — any continuation is fine.
    if entry and entry[-1] in ("_", "-"):
        return True
    after = idx + len(entry)
    # Require a separator or end-of-string after the match.
    if after >= len(name) or name[after] in ("_", ".", ":", "/", "-"):
        return True
    return False


def _resolve_skips(
    skipped_names: list[str], repo_root: Path
) -> tuple[list[str], list[str]]:
    """Partition *skipped_names* into (allowed, blocked) via the allow-list."""
    entries = _load_allowlist(repo_root)
    if entries is None:
        return [], list(skipped_names)
    allowed: list[str] = []
    blocked: list[str] = []
    for name in skipped_names:
        if any(_entry_matches(entry, name) for entry in entries):
            allowed.append(name)
        else:
            blocked.append(name)
    return allowed, blocked


def zero_skip_gate(
    log: str,
    repo_root: str | os.PathLike[str] | None = None,
    parser: Callable[[str], dict[str, list[str]]] | None = None,
) -> dict[str, Any]:
    """Parse test-runner output and gate on executed/skipped test states (R1).

    *parser* receives the log and must return ``{"passed": [names],
    "failed": [names], "skipped": [names]}``.  Defaults to a pytest
    verbose-output parser.  Pass any callable with the same signature to
    support other runners (pluggable interface, R1).

    *repo_root* locates ``.adversarial-gate/skip-allowlist.txt`` (R2/R3).
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    parse = parser or _parse_pytest_output

    parsed = parse(log)
    passed_names = parsed["passed"]
    failed_names = parsed["failed"]
    skipped_names = parsed["skipped"]

    executed = len(passed_names) + len(failed_names)
    allowed, blocked = _resolve_skips(skipped_names, root)

    # R4: executed failures decide immediately; skips resolved via allow-list.
    # Zero recognized test lines = unparseable or empty log → fail (A1).
    total_recognized = len(passed_names) + len(failed_names) + len(skipped_names)
    if total_recognized == 0:
        return {
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "allowed_skipped": 0,
            "blocked_skipped": 0,
            "pass": False,
            "diagnostic": "no_recognizable_test_lines",
        }

    pass_gate = len(failed_names) == 0 and len(blocked) == 0

    return {
        "executed": executed,
        "passed": len(passed_names),
        "failed": len(failed_names),
        "skipped": len(skipped_names),
        "allowed_skipped": len(allowed),
        "blocked_skipped": len(blocked),
        "pass": pass_gate,
    }


def run_contract_gate(
    spec_path: str | os.PathLike[str],
    repo_root: str | os.PathLike[str],
    sandbox_profile: Any = None,
) -> dict[str, Any]:
    """Parse, execute, and settle *spec_path*'s ac-directive contract (F1).

    Parses the ``ac-directive`` blocks bound to acceptance criteria in
    *spec_path* (P2), executes each one in *repo_root* through the shared
    execution engine under containment (P3/P4), and settles ``"APPROVE"``
    only when every directive — and every AC it is bound to — passes; a
    spec with zero directives settles ``"APPROVE"`` vacuously (nothing to
    enforce). *sandbox_profile*, when given, is threaded through to each
    directive as its P1 constrained profile instead of per-directive
    auto-resolution (a caller-scoped override, not global state).

    This is the single gate entry point the spec/plan/review/loop pipelines
    all import (R2): the same *spec_path* + *repo_root* settle identically
    regardless of which pipeline calls it, because none of them can alter
    how a directive is parsed or enforced — only whether they call this at
    all.

    Returns ``{settle, ac_status, failures, directives}``:

    * ``settle`` — ``"APPROVE"`` or ``"REJECT"``;
    * ``ac_status`` — ``{ac_id: "pass" | "fail"}``, one entry per AC that
      carried at least one directive or parse error; an AC with multiple
      directives is ``"fail"`` if any of them failed;
    * ``failures`` — ``[{ac, id, reason}, ...]`` for every failing directive
      or parse error (``id`` is None for a parse error, which has no
      directive to identify);
    * ``directives`` — the raw ``run_directive`` result for every
      well-formed directive that executed, even when other directives in
      the same spec failed to parse (a parse error on one AC never
      suppresses the well-formed directives bound to the rest).

    :mod:`adversarial_common.contract` (PyYAML-dependent) is imported via
    ``importlib`` rather than a top-level import so this module — audited
    elsewhere as stdlib-only — keeps that guarantee, mirroring the
    ``importlib.import_module("yaml")`` seam already used in providers.py.
    """
    contract = importlib.import_module("adversarial_common.contract")
    result: dict[str, Any] = {
        "settle": "REJECT",
        "ac_status": {},
        "failures": [],
        "directives": [],
    }

    parsed = contract.parse_spec_file(spec_path)
    for err in parsed.errors:
        ac_id = err.ac or "UNKNOWN"
        result["ac_status"][ac_id] = "fail"
        result["failures"].append(
            {"ac": ac_id, "id": None, "reason": f"parse error: {err.cause}"}
        )

    all_pass = parsed.ok
    for directive in parsed.directives:
        outcome = contract.run_directive(directive, repo_root, sandbox_profile)
        result["directives"].append(outcome)
        status = outcome["status"]
        prior = result["ac_status"].get(directive.ac, "pass")
        result["ac_status"][directive.ac] = (
            "fail" if status != "pass" or prior == "fail" else "pass"
        )
        if status != "pass":
            all_pass = False
            result["failures"].append(
                {"ac": directive.ac, "id": outcome["id"], "reason": outcome["message"]}
            )

    result["settle"] = "APPROVE" if all_pass else "REJECT"
    return result


def _effective_thresholds(kind: str, overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    defaults = _DEFAULT_THRESHOLDS.get(kind, _DEFAULT_THRESHOLDS["input"])
    effective = {
        "min_chars": defaults["min_chars"], "min_tokens": defaults["min_tokens"],
        "required_sections": list(defaults["required_sections"]),
        "min_source_lines": defaults["min_source_lines"],
    }
    if overrides is None:
        return effective
    if not isinstance(overrides, Mapping):
        raise TypeError("thresholds must be a mapping or None")
    unknown = set(overrides).difference(effective)
    if unknown:
        raise ValueError("unknown context threshold(s): " + ", ".join(sorted(map(str, unknown))))
    for name in ("min_chars", "min_tokens", "min_source_lines"):
        if name in overrides:
            effective[name] = _non_negative_int(name, overrides[name])
    if "required_sections" in overrides:
        sections = overrides["required_sections"]
        if isinstance(sections, str) or not isinstance(sections, (list, tuple)):
            raise TypeError("required_sections must be a list or tuple of strings")
        if any(not isinstance(section, str) or not section.strip() for section in sections):
            raise ValueError("required_sections entries must be non-empty strings")
        effective["required_sections"] = [section.strip() for section in sections]
    return effective


def _context_result(ok: bool, reason: str, thresholds: dict[str, Any]) -> dict[str, Any]:
    audited = dict(thresholds)
    audited["required_sections"] = list(thresholds["required_sections"])
    return {"ok": ok, "reason": reason, "thresholds": audited}


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _count_diff_source_lines(text: str) -> int:
    return sum(
        1 for line in text.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def _enforce_cap(text: str, max_chars: int) -> tuple[str, bool]:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    limit = _non_negative_int("max_chars", max_chars)
    if len(text) <= limit:
        return text, False
    if limit == 0:
        return "", True
    if limit <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:limit], True
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER, True


def _stat_value(stats: Mapping[str, Any], names: tuple[str, ...], default: int) -> int:
    for name in names:
        if name in stats:
            return _non_negative_int(name, stats[name])
    return default


def _command_argv(command: Any) -> tuple[list[str], str | None]:
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return [], f"invalid command syntax: {exc}"
    elif isinstance(command, (list, tuple)) and all(isinstance(arg, str) for arg in command):
        argv = list(command)
    else:
        return [], "command must be a string or sequence of strings"
    if not argv:
        return [], "verification command is empty"
    return argv, None


def _execution_command(
    command: Any, workdir: Path
) -> tuple[list[str], str | None, str | None]:
    """Resolve a gate while preserving the configured command contract.

    String commands historically accepted shell syntax, so execute them through
    ``sh -c``. Sequence commands remain literal argv and never invoke a shell.
    In both cases Popen itself receives an argv vector with ``shell=False``.
    """
    if isinstance(command, str):
        if not command.strip():
            return [], None, "verification command is empty"
        shell = shutil.which("sh")
        if shell is None:
            return [], None, "verification shell not found: sh"
        return [shell, "-c", command], shell, None

    argv, error = _command_argv(command)
    if error:
        return [], None, error
    resolved = _resolve_executable(argv[0], workdir)
    if resolved is None:
        return [], None, f"verification command not found: {argv[0]}"
    return [resolved, *argv[1:]], resolved, None


def _resolve_executable(executable: str, workdir: Path) -> str | None:
    expanded = os.path.expanduser(executable)
    if os.sep in expanded:
        path = Path(expanded)
        if not path.is_absolute():
            path = workdir / path
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(expanded)


def _gate_base(name: str, command: Any) -> dict[str, Any]:
    if isinstance(command, (list, tuple)) and all(
        isinstance(argument, str) for argument in command
    ):
        rendered = shlex.join(command)
    elif isinstance(command, str):
        rendered = command
    elif command is None:
        rendered = ""
    else:
        rendered = repr(command)
    return {
        "gate": name,
        "command": rendered,
        "ok": False,
        "exit_code": None,
        "infra": False,
        "log": "",
        "truncated": False,
    }


def _gate_failure(result: dict[str, Any], code: int, error: str, infra: bool) -> dict[str, Any]:
    result.update({"ok": False, "exit_code": code, "infra": infra, "log": "", "error": error})
    return result


def _kill_process_group(proc: subprocess.Popen[str]) -> str | None:
    """Kill the session created for a timed-out verification gate.

    ``start_new_session=True`` makes the child's PID its process-group ID. We
    intentionally use that stable ID directly: looking it up through a leader
    that has already exited can fail while descendants in the group keep
    running and mutating the worktree.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return None
    except OSError as group_error:
        try:
            proc.kill()
        except OSError as process_error:
            return (
                f"could not kill process group ({group_error}); "
                f"could not kill gate process ({process_error})"
            )
        return f"could not kill process group: {group_error}"
    return None


def _bounded_log(log: str, max_chars: int) -> tuple[str, bool]:
    if len(log) <= max_chars:
        return log, False
    marker = "[...truncated...]\n"
    if max_chars <= len(marker):
        return marker[:max_chars], True
    return marker + log[-(max_chars - len(marker)):], True


def _non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _positive_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


__all__ = [
    "TRUNCATION_MARKER", "check_context", "enforce_input_cap",
    "enforce_output_cap", "estimate_complexity", "post_build_gate",
    "post_fix_gate", "pre_build_gate", "run_contract_gate", "zero_skip_gate",
]
