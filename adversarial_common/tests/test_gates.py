"""Unit tests for stdlib-only context, size, and complexity primitives."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from adversarial_common import (
    check_context,
    enforce_input_cap,
    enforce_output_cap,
    estimate_complexity,
)
from adversarial_common.gates import TRUNCATION_MARKER


def test_empty_input_is_blocked_with_named_reason():
    result = check_context("brief", " \n\t", {"min_chars": 0, "min_tokens": 0})

    assert result["ok"] is False
    assert result["reason"] == "empty_input"
    assert result["thresholds"]["min_chars"] == 0


def test_input_below_character_floor_is_blocked():
    result = check_context(
        "input",
        "short",
        {"min_chars": 10, "min_tokens": 0},
    )

    assert result["ok"] is False
    assert result["reason"] == "below_min_chars"


def test_spec_requires_requirements_section():
    text = "# Overview\n" + "A detailed design without the required heading. " * 4

    result = check_context("spec", text)

    assert result["ok"] is False
    assert result["reason"] == "missing_required_section:Requirements"


def test_diff_with_no_source_lines_is_blocked():
    result = check_context("diff", "diff --git a/a.py b/a.py\n")

    assert result["ok"] is False
    assert result["reason"] == "below_min_source_lines"


@pytest.mark.parametrize("cap", [enforce_input_cap, enforce_output_cap])
def test_cap_truncates_with_marker_and_honors_hard_limit(cap):
    text = "abcdefghijklmnopqrstuvwxyz"
    limited, was_truncated = cap(text, 20)

    assert was_truncated is True
    assert limited.endswith(TRUNCATION_MARKER)
    assert limited.startswith(text[: 20 - len(TRUNCATION_MARKER)])
    assert len(limited) == 20


@pytest.mark.parametrize("cap", [enforce_input_cap, enforce_output_cap])
def test_cap_leaves_text_at_limit_unchanged(cap):
    assert cap("exact", 5) == ("exact", False)


def test_zero_cap_is_safe_and_reported():
    assert enforce_input_cap("content", 0) == ("", True)


def test_complexity_tiers_and_agent_counts_are_strictly_increasing():
    samples = [
        "x" * 100,
        "x" * 1_500,
        "x" * 7_500,
        "x" * 25_000,
    ]

    results = [estimate_complexity(sample) for sample in samples]

    assert [result["tier"] for result in results] == [
        "trivial",
        "low",
        "medium",
        "high",
    ]
    agent_counts = [result["max_agents"] for result in results]
    assert all(left < right for left, right in zip(agent_counts, agent_counts[1:]))


def test_diff_structure_increases_complexity_and_summary_is_auditable():
    plain = estimate_complexity("small change")
    diff = estimate_complexity(
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        + "-old\n+new\n" * 20
    )

    assert diff["max_agents"] > plain["max_agents"]
    assert "1 files" in diff["summary"]
    assert "40 source lines" in diff["summary"]
    json.dumps(diff)


def test_gate_module_imports_only_standard_library_modules():
    path = Path(__file__).parents[1] / "gates.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
    )

    assert imported_roots <= sys.stdlib_module_names


@pytest.mark.parametrize("bad_limit", [-1, True, 1.5])
def test_cap_rejects_invalid_limits(bad_limit):
    with pytest.raises((TypeError, ValueError)):
        enforce_output_cap("text", bad_limit)
