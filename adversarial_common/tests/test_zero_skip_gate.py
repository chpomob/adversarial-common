"""Unit tests for ZeroSkipGate — 6 plan scenarios (AC1–AC3)."""

from __future__ import annotations

import json
from pathlib import Path

from adversarial_common.gates import zero_skip_gate

_ALLOWLIST_REL = ".adversarial-gate/skip-allowlist.txt"


def _make_pytest_log(*, passed=(), failed=(), skipped=()):
    """Build a pytest verbose log from the given test-name sequences."""
    lines = ["============================= test session starts =============================="]
    for name in passed:
        lines.append(f"tests/{name}.py::test_case PASSED")
    for name in failed:
        lines.append(f"tests/{name}.py::test_case FAILED")
    for name in skipped:
        lines.append(f"tests/{name}.py::test_case SKIPPED")
    p = len(passed)
    f = len(failed)
    s = len(skipped)
    total = p + f + s
    lines.append(f"======================= {p} passed, {f} failed, {s} skipped in 1.23s =======================")
    return "\n".join(lines)


def _write_allowlist(root, *entries):
    path = root / _ALLOWLIST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(entries))


# -- Scenario 1: 0 executed, all skipped, no allow-list → fail (R3) --
def test_all_skipped_no_allowlist_fails(tmp_path):
    log = _make_pytest_log(skipped=("test_a", "test_b"))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is False
    assert result["executed"] == 0
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["skipped"] == 2
    assert result["allowed_skipped"] == 0
    assert result["blocked_skipped"] == 2
    _assert_result_shape(result)


# -- Scenario 2: 0 executed, all skipped, all in allow-list → pass (R2) --
def test_all_skipped_allowlisted_passes(tmp_path):
    _write_allowlist(tmp_path, "test_a", "test_b")
    log = _make_pytest_log(skipped=("test_a", "test_b"))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is True
    assert result["executed"] == 0
    assert result["skipped"] == 2
    assert result["allowed_skipped"] == 2
    assert result["blocked_skipped"] == 0


# Scenario 2b: substring matching — one entry covers multiple skipped tests.
def test_allowlist_substring_covers_multiple_skips(tmp_path):
    _write_allowlist(tmp_path, "integration_")
    log = _make_pytest_log(skipped=("integration_db", "integration_api"))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is True
    assert result["allowed_skipped"] == 2
    assert result["blocked_skipped"] == 0


# -- Scenario 3: executed + 0 failures → pass --
def test_executed_all_pass_passes(tmp_path):
    log = _make_pytest_log(passed=("test_x", "test_y"))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is True
    assert result["executed"] == 2
    assert result["passed"] == 2
    assert result["failed"] == 0
    assert result["skipped"] == 0


# -- Scenario 4: executed + failures → fail --
def test_executed_with_failures_fails(tmp_path):
    log = _make_pytest_log(passed=("test_x",), failed=("test_y",))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is False
    assert result["executed"] == 2
    assert result["passed"] == 1
    assert result["failed"] == 1


# -- Scenario 5: mixed executed+skipped → executed decides --
def test_mixed_pass_plus_allowed_skip_passes(tmp_path):
    _write_allowlist(tmp_path, "test_slow")
    log = _make_pytest_log(passed=("test_fast",), skipped=("test_slow",))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is True
    assert result["executed"] == 1
    assert result["passed"] == 1
    assert result["skipped"] == 1
    assert result["allowed_skipped"] == 1
    assert result["blocked_skipped"] == 0


def test_mixed_fail_plus_skipped_fails_regardless_of_allowlist(tmp_path):
    _write_allowlist(tmp_path, "test_skip")
    log = _make_pytest_log(passed=("test_ok",), failed=("test_bad",), skipped=("test_skip",))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is False
    assert result["failed"] == 1
    # executed failures dominate; skips are tallied but don't rescue
    assert result["skipped"] == 1
    assert result["allowed_skipped"] == 1
    assert result["blocked_skipped"] == 0


def test_mixed_pass_plus_blocked_skip_fails(tmp_path):
    _write_allowlist(tmp_path, "other")
    log = _make_pytest_log(passed=("test_fast",), skipped=("test_unlisted",))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is False
    assert result["blocked_skipped"] == 1
    assert result["failed"] == 0


# -- Scenario 6: allow-list missing → all skips blocked (R3) --
def test_missing_allowlist_blocks_all_skips(tmp_path):
    log = _make_pytest_log(passed=("test_ok",), skipped=("test_a", "test_b"))
    # no allow-list file written

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is False
    assert result["skipped"] == 2
    assert result["allowed_skipped"] == 0
    assert result["blocked_skipped"] == 2


# -- AC3: structured return shape --
def test_return_is_json_serializable_and_has_all_counts(tmp_path):
    log = _make_pytest_log(passed=("t_aaa",), failed=("t_bbb",), skipped=("t_ccc", "t_ddd"))
    _write_allowlist(tmp_path, "t_ccc")

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is False  # one failed
    assert result["executed"] == 2
    assert result["passed"] == 1
    assert result["failed"] == 1
    assert result["skipped"] == 2
    assert result["allowed_skipped"] == 1
    assert result["blocked_skipped"] == 1
    json.dumps(result)  # must not raise

    assert result["pass"] is False  # one failed


# -- Pluggable parser (R1) --
def test_custom_parser_is_used_instead_of_default():
    def _custom_parser(log):
        return {"passed": ["custom_a"], "failed": [], "skipped": []}

    result = zero_skip_gate("anything", parser=_custom_parser)

    assert result["pass"] is True
    assert result["passed"] == 1
    assert result["executed"] == 1


# -- Allow-list comment lines are ignored --
def test_allowlist_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / _ALLOWLIST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# header comment\n\n  test_x  \n   # inline comment?\n# another\n\n")

    log = _make_pytest_log(skipped=("test_x_file",))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is True
    assert result["allowed_skipped"] == 1


# -- Empty allow-list file: all skips blocked --
def test_empty_allowlist_blocks_all(tmp_path):
    path = tmp_path / _ALLOWLIST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")

    log = _make_pytest_log(skipped=("test_a",))

    result = zero_skip_gate(log, repo_root=tmp_path)

    assert result["pass"] is False
    assert result["blocked_skipped"] == 1


def _assert_result_shape(result):
    expected_keys = {
        "executed", "passed", "failed", "skipped",
        "allowed_skipped", "blocked_skipped", "pass",
    }
    assert expected_keys <= result.keys()
    json.dumps(result)
