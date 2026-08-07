"""Unit tests for adversarial_common.readgate."""

from __future__ import annotations

import pytest

from adversarial_common.readgate import (
    ReadGatePolicy,
    ReadGateResult,
    is_enforced_role,
    validate_agent_output,
)


# -- validate_agent_output (stateless) ---------------------------------------

def test_marker_present_plain_text():
    """READ: line with exact path → marker_found=True, status=pass."""
    result = validate_agent_output("READ: spec.md\nsome output", "spec.md")
    assert result.marker_found is True
    assert result.status == "pass"


def test_marker_present_plain_text_basename():
    """READ: line with basename-only match succeeds."""
    result = validate_agent_output("READ: spec.md\nmore text", "docs/spec.md")
    assert result.marker_found is True
    assert result.status == "pass"


def test_marker_present_json_read_files():
    """JSON read_files array with matching path → marker_found=True."""
    output = '{"read_files": ["spec.md"], "verdict": "APPROVE"}'
    result = validate_agent_output(output, "spec.md")
    assert result.marker_found is True
    assert result.status == "pass"


def test_marker_present_json_nested_read_files():
    """read_files nested inside another object (depth>0) is found."""
    output = '{"data": {"meta": {"read_files": ["spec.md"]}}}'
    result = validate_agent_output(output, "spec.md")
    assert result.marker_found is True


def test_json_case_insensitive_basename():
    """JSON read_files basename match is case-insensitive."""
    output = '{"read_files": ["SPEC.MD"]}'
    result = validate_agent_output(output, "docs/spec.md")
    assert result.marker_found is True


def test_plain_text_case_sensitive_keyword():
    """read: (lowercase) does NOT match — keyword is case-sensitive."""
    result = validate_agent_output("read: spec.md\noutput", "spec.md")
    assert result.marker_found is False


def test_json_read_files_non_string_entries():
    """Non-string entries in read_files are skipped gracefully."""
    output = '{"read_files": [null, 42, true, {"nested": 1}, "spec.md"]}'
    result = validate_agent_output(output, "spec.md")
    assert result.marker_found is True


def test_json_read_files_not_an_array():
    """Non-array read_files value is skipped gracefully, no crash.

    Regression: guards isinstance(files, list) check at readgate.py _scan_read_files.
    """
    output = '{"data": {"read_files": "spec.md"}}'
    result = validate_agent_output(output, "spec.md")
    assert result.marker_found is False


def test_json_not_an_object():
    """Non-dict JSON root still scanned (list at root)."""
    output = '[{"read_files": ["spec.md"]}]'
    result = validate_agent_output(output, "spec.md")
    assert result.marker_found is True


def test_trailing_content_after_read_path():
    """READ: line with trailing chars after path (e.g. spec.md.bak) does NOT match.

    Regression for end-of-line anchoring in _PLAIN_MARKER_RE (finding P1).
    """
    result = validate_agent_output("READ: spec.md.bak\nsome output", "spec.md")
    assert result.marker_found is False


def test_marker_absent():
    """No marker at all → marker_found=False, status=WARNING."""
    result = validate_agent_output("no markers here", "spec.md")
    assert result.marker_found is False
    assert result.status == "WARNING"


def test_invalid_json_falls_back_to_plain_text():
    """Unparseable JSON doesn't raise, plain-text marker still works."""
    result = validate_agent_output("READ: spec.md\n{invalid json", "spec.md")
    assert result.marker_found is True
    assert result.status == "pass"


def test_json_depth_limit():
    """read_files beyond depth 10 is not found."""
    inner = '{"read_files": ["spec.md"]}'
    # wrap 11 times in a dict to exceed the depth limit
    node = inner
    for _ in range(11):
        node = '{"outer": ' + node + "}"
    result = validate_agent_output(node, "spec.md")
    assert result.marker_found is False


# -- ReadGatePolicy (stateful) -----------------------------------------------

def test_policy_first_miss_warning():
    """First consecutive miss on a path → WARNING."""
    policy = ReadGatePolicy()
    result = policy.check("no marker", "spec.md")
    assert result.marker_found is False
    assert result.status == "WARNING"


def test_policy_second_miss_hard_error():
    """Second consecutive miss on the same path → HARD_ERROR."""
    policy = ReadGatePolicy()
    policy.check("no marker", "spec.md")  # WARNING
    result = policy.check("still no marker", "spec.md")  # HARD_ERROR
    assert result.marker_found is False
    assert result.status == "HARD_ERROR"


def test_policy_reset_after_marker():
    """A present marker resets the miss counter → next miss is WARNING."""
    policy = ReadGatePolicy()
    policy.check("no marker", "spec.md")  # WARNING, counter=1
    policy.check("READ: spec.md", "spec.md")  # pass, counter reset to 0
    result = policy.check("no marker again", "spec.md")  # WARNING again
    assert result.status == "WARNING"


def test_policy_unrelated_path_does_not_escalate():
    """Miss on path Y does not reset path X's streak."""
    policy = ReadGatePolicy()
    policy.check("no marker", "spec.md")  # X: WARNING, counter=1
    policy.check("no marker either", "plan.md")  # Y: WARNING, separate counter
    # Y's miss does NOT reset X's counter — X still at 1, so second miss → HARD_ERROR
    result = policy.check("still no marker for spec", "spec.md")  # X: second consecutive miss
    assert result.status == "HARD_ERROR"


def test_policy_pass_on_marker():
    """Marker present → pass, regardless of previous misses."""
    policy = ReadGatePolicy()
    policy.check("no marker", "spec.md")  # WARNING
    result = policy.check("READ: spec.md", "spec.md")
    assert result.marker_found is True
    assert result.status == "pass"


def test_policy_independent_paths():
    """Two paths escalate independently."""
    policy = ReadGatePolicy()
    policy.check("no marker", "a.md")  # a: WARNING
    policy.check("no marker", "b.md")  # b: WARNING
    policy.check("no marker again", "a.md")  # a: HARD_ERROR
    result = policy.check("still no", "b.md")  # b: HARD_ERROR
    assert result.status == "HARD_ERROR"


# -- ReadGateResult ----------------------------------------------------------

def test_result_rejects_invalid_status():
    """ReadGateResult raises ValueError for invalid status strings."""
    with pytest.raises(ValueError):
        ReadGateResult(marker_found=False, status="INVALID")


def test_result_accepts_valid_statuses():
    """All three valid statuses are accepted."""
    for s in ("pass", "WARNING", "HARD_ERROR"):
        r = ReadGateResult(marker_found=True, status=s)
        assert r.status == s


# -- ReadGatePolicy enforce=False ------------------------------------------

def test_policy_enforce_false_returns_pass_no_marker():
    """enforce=False returns PASS even when output has no marker."""
    policy = ReadGatePolicy(enforce=False)
    result = policy.check("no marker at all", "spec.md")
    assert result.marker_found is True
    assert result.status == "pass"


def test_policy_enforce_false_no_miss_counting():
    """enforce=False does not count misses — two calls still PASS."""
    policy = ReadGatePolicy(enforce=False)
    policy.check("no marker", "spec.md")
    result = policy.check("still no marker", "spec.md")
    assert result.marker_found is True
    assert result.status == "pass"


# -- is_enforced_role ------------------------------------------------------

def test_is_enforced_role_build_false():
    """build role is relaxed."""
    assert is_enforced_role("build") is False


def test_is_enforced_role_fix_false():
    """fix role is relaxed."""
    assert is_enforced_role("fix") is False


def test_is_enforced_role_review_true():
    """review role is enforced."""
    assert is_enforced_role("review") is True


def test_is_enforced_role_verify_true():
    """verify role is enforced."""
    assert is_enforced_role("verify") is True


def test_is_enforced_role_arbiter_true():
    """arbiter role is enforced."""
    assert is_enforced_role("arbiter") is True


def test_is_enforced_role_challenge_true():
    """challenge role is enforced."""
    assert is_enforced_role("challenge") is True
