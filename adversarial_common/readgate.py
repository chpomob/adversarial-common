"""ReadGate shared helper — proof-of-read enforcement for agent outputs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ReadGateResult:
    """Result of a read-gate marker check.

    Attributes:
        marker_found: True if a valid READ marker was detected in the output.
        status: One of ``"pass"``, ``"WARNING"``, or ``"HARD_ERROR"``.
    """

    marker_found: bool
    status: str

    def __post_init__(self) -> None:
        if self.status not in ("pass", "WARNING", "HARD_ERROR"):
            raise ValueError(
                f"Invalid status {self.status!r}; "
                "expected 'pass', 'WARNING', or 'HARD_ERROR'"
            )


def validate_agent_output(output: str, path: str) -> ReadGateResult:
    """Check whether *output* contains a READ marker for *path*.

    Two marker forms are recognized:

    1. A plain-text line matching ``READ: <path>`` anywhere in the output.
       ``READ:`` is case-sensitive; the path is matched case-sensitively
       by full path first, falling back to basename.

    2. A ``read_files`` array in JSON output (scanned up to depth 10).
       Matching is case-insensitive on the basename component only.

    Returns a ``ReadGateResult`` with ``marker_found=True`` and
    ``status="pass"`` when a marker is found, or ``marker_found=False``
    and ``status="WARNING"`` otherwise.
    """
    if _check_plain_text_marker(output, path) or _check_json_marker(output, path):
        return ReadGateResult(marker_found=True, status="pass")
    return ReadGateResult(marker_found=False, status="WARNING")


# -- plain-text marker detection -------------------------------------------

_PLAIN_MARKER_RE = re.compile(r"^READ:\s*(.+)$", re.MULTILINE)


def _check_plain_text_marker(output: str, path: str) -> bool:
    """Scan for ``READ: <path>`` lines (case-sensitive keyword)."""
    target_basename = Path(path).name
    for m in _PLAIN_MARKER_RE.finditer(output):
        candidate = m.group(1).strip()
        if candidate == path:
            return True
        if candidate == target_basename:
            return True
    return False


# -- JSON marker detection --------------------------------------------------

def _check_json_marker(output: str, path: str) -> bool:
    """Try to parse *output* as JSON and scan for ``read_files`` arrays."""
    try:
        root = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return False
    target = Path(path).name.lower()
    return _scan_read_files(root, target, depth=0)


def _scan_read_files(node: Any, target: str, depth: int) -> bool:
    """Recursively scan *node* up to 10 levels deep for ``read_files`` arrays."""
    if depth > 10:
        return False
    if isinstance(node, dict):
        if "read_files" in node:
            files = node["read_files"]
            if isinstance(files, list):
                for f in files:
                    if isinstance(f, str) and Path(f).name.lower() == target:
                        return True
        for value in node.values():
            if _scan_read_files(value, target, depth + 1):
                return True
    elif isinstance(node, list):
        for item in node:
            if _scan_read_files(item, target, depth + 1):
                return True
    return False


# -- role classification ---------------------------------------------------

_RELAXED_ROLES: frozenset[str] = frozenset({"build", "fix"})


def is_enforced_role(role: str) -> bool:
    """Return ``False`` for relaxed roles (build, fix), ``True`` for all others.

    Relaxed roles still get the READ marker instruction in their prompt for
    auditability, but the gate itself does not enforce it.
    """
    return role not in _RELAXED_ROLES


# -- stateful policy --------------------------------------------------------

class ReadGatePolicy:
    """Stateful read-gate policy with warn → hard-error escalation.

    Tracks consecutive misses per path.  The first miss on a path returns
    ``WARNING``; the second consecutive miss returns ``HARD_ERROR``.
    A successful marker read resets the counter for that path.

    Calls for unrelated paths do not affect each other's streaks.

    When *enforce* is ``False``, ``check()`` always returns a PASS result
    without inspecting the output or counting misses.  This is intended for
    non-reading-critical roles (BUILD, FIX) where the marker instruction is
    still emitted for auditability but never hard-fails.

    Example::

        policy = ReadGatePolicy()
        result = policy.check(output, path="spec.md")
        # First miss → status="WARNING"
        result = policy.check(output2, path="spec.md")
        # Second consecutive miss → status="HARD_ERROR"
    """

    def __init__(self, *, enforce: bool = True) -> None:
        self._misses: dict[str, int] = {}
        self._enforce = enforce

    def check(self, output: str, path: str) -> ReadGateResult:
        """Check *output* for a READ marker for *path*, tracking state.

        Returns a ``ReadGateResult`` whose ``status`` reflects the
        escalation policy for consecutive misses on the same path.

        When ``enforce=False``, always returns a PASS result.
        """
        if not self._enforce:
            return ReadGateResult(marker_found=True, status="pass")
        result = validate_agent_output(output, path)
        if result.marker_found:
            self._misses[path] = 0
            return result

        # Marker not found — increment and escalate.
        count = self._misses.get(path, 0) + 1
        self._misses[path] = count
        if count >= 2:
            return ReadGateResult(marker_found=False, status="HARD_ERROR")
        return ReadGateResult(marker_found=False, status="WARNING")
