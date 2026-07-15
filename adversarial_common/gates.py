"""Pre-flight context, size, complexity, and verification gates."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final


TRUNCATION_MARKER: Final = "\n[TRUNCATED]\n"

_DEFAULT_THRESHOLDS: Final[dict[str, dict[str, Any]]] = {
    "brief": {"min_chars": 40, "min_tokens": 10, "required_sections": [], "min_source_lines": 0},
    "spec": {"min_chars": 100, "min_tokens": 25, "required_sections": ["Requirements"], "min_source_lines": 0},
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
        if section.casefold() not in headings:
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
        argv, error = _command_argv(command)
        if error:
            return _gate_failure(result, 127, error, True)
        resolved = _resolve_executable(argv[0], root)
        result["resolved_executable"] = resolved or ""
        if resolved is None:
            return _gate_failure(
                result, 127, f"verification command not found: {argv[0]}", True
            )
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
    argv, error = _command_argv(command)
    if error:
        return _gate_failure(result, 127, error, True)
    if _resolve_executable(argv[0], root) is None:
        return _gate_failure(result, 127, f"verification command not found: {argv[0]}", True)
    timeout_value = _positive_number("timeout", timeout)
    log_cap = _non_negative_int("max_log_chars", max_log_chars)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return _gate_failure(result, 126, f"could not start gate: {exc}", True)
    try:
        log, _ = proc.communicate(timeout=timeout_value)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        log, _ = proc.communicate()
        bounded, truncated = _bounded_log(log, log_cap)
        result.update({
            "ok": False, "exit_code": 124, "infra": True,
            "log": bounded, "truncated": truncated,
            "error": f"gate timed out after {timeout_value}s",
        })
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


def _resolve_executable(executable: str, workdir: Path) -> str | None:
    expanded = os.path.expanduser(executable)
    if os.sep in expanded:
        path = Path(expanded)
        if not path.is_absolute():
            path = workdir / path
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(expanded)


def _gate_base(name: str, command: Any) -> dict[str, Any]:
    if isinstance(command, (list, tuple)):
        rendered = shlex.join(command)
    else:
        rendered = command or ""
    return {"gate": name, "command": rendered}


def _gate_failure(result: dict[str, Any], code: int, error: str, infra: bool) -> dict[str, Any]:
    result.update({"ok": False, "exit_code": code, "infra": infra, "log": "", "error": error})
    return result


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
    "post_fix_gate", "pre_build_gate",
]
