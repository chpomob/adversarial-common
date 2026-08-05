"""Test that inspector.md contains fake-done shortcuts, non-trivial criteria, and anti-overload threshold."""

from adversarial_common import load_persona


SHORTCUT_NAMES = [
    "Relaxed tests",
    "Swallowed errors",
    "Fake renames",
    "Stub returns",
    "Comment-as-fix",
    "Happy-path only",
    "Scope creep",
    "Invented API",
    "Silent decision",
    "Pass-by-mock",
    "Off-spec done",
]

NON_TRIVIAL_CRITERIA = [
    "Branching logic",
    "Module/service boundary",
    "Compiler-unverifiable",
    "Irreversible blast radius",
]


def test_all_11_shortcut_names_present():
    """AC1: All 11 shortcut names present in inspector.md."""
    text = load_persona("inspector")
    for name in SHORTCUT_NAMES:
        assert name in text, f"Missing shortcut: {name}"


def test_all_4_non_trivial_criteria_present():
    """AC2: The 4 non-trivial criteria present."""
    text = load_persona("inspector")
    for name in NON_TRIVIAL_CRITERIA:
        assert name in text, f"Missing non-trivial criterion: {name}"


def test_anti_overload_rule_present():
    """AC3: Anti-overload rule present — 'trivial' changes pass without deep inspection."""
    text = load_persona("inspector")
    assert "trivial" in text.lower(), "Anti-overload rule not found (missing 'trivial')"
    assert (
        "pass without deep inspection" in text
        or "pass through" in text
    ), "Anti-overload rule missing pass-through language"


def test_checklist_before_output_format():
    """AC4: Checklist section precedes output-format section."""
    text = load_persona("inspector")
    checklist_idx = text.find("## Fake-Done Shortcuts")
    output_idx = text.find("Output JSON:")
    assert checklist_idx >= 0, "Fake-Done Shortcuts section not found"
    assert output_idx >= 0, "Output JSON section not found"
    assert checklist_idx < output_idx, (
        "Fake-Done Shortcuts section must precede Output JSON section"
    )


def test_non_trivial_before_output_format():
    """AC4: Non-trivial criteria section precedes output-format."""
    text = load_persona("inspector")
    nontriv_idx = text.find("## Non-Trivial Criteria")
    output_idx = text.find("Output JSON:")
    assert nontriv_idx >= 0, "Non-Trivial Criteria section not found"
    assert nontriv_idx < output_idx, (
        "Non-Trivial Criteria section must precede Output JSON section"
    )


def test_anti_overload_before_output_format():
    """AC4: Anti-overload section precedes output-format."""
    text = load_persona("inspector")
    overload_idx = text.find("## Anti-Overload Threshold")
    output_idx = text.find("Output JSON:")
    assert overload_idx >= 0, "Anti-Overload Threshold section not found"
    assert overload_idx < output_idx, (
        "Anti-Overload Threshold section must precede Output JSON section"
    )


def test_git_aware_block_intact():
    """AC4: The git-aware block is not in inspector (it's critic-only), but the original persona content remains."""
    text = load_persona("inspector")
    # Inspector role description and focus areas intact
    assert "Inspector" in text
    assert "edge cases" in text
    assert "error handling" in text
    # Git-aware workflow block must NOT be present (critic-only)
    assert "BUILD phase" not in text, "Git-aware BUILD phase leaked into inspector"
    assert "FIX phase" not in text, "Git-aware FIX phase leaked into inspector"
    assert "Git workflow rules" not in text, "Git workflow rules leaked into inspector"
