"""Test that critic.md contains the fake-done shortcuts checklist."""

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


def test_all_11_shortcut_names_present():
    text = load_persona("critic")
    for name in SHORTCUT_NAMES:
        assert name in text, f"Missing shortcut: {name}"
        # Each shortcut must have a heuristic block following the name
        idx = text.find(name)
        assert idx >= 0
        tail = text[idx + len(name):]
        assert "*Heuristic:*" in tail[:512], (
            f"Shortcut '{name}' has no *Heuristic:* block within 512 chars"
        )


def test_checklist_section_precedes_output_format():
    text = load_persona("critic")
    checklist_idx = text.find("## Fake-Done Shortcuts")
    output_idx = text.find("Output format:")
    assert checklist_idx >= 0, "Fake-Done Shortcuts section not found"
    assert output_idx >= 0, "Output format section not found"
    assert checklist_idx < output_idx, (
        "Fake-Done Shortcuts section must precede Output format section"
    )


def test_verdict_must_name_shortcut():
    text = load_persona("critic")
    section_start = text.find("## Fake-Done Shortcuts")
    section_end = text.find("Output format:")
    section = text[section_start:section_end]
    assert (
        "REQUEST_CHANGES" in section and "REJECT" in section
    ), "Shortcuts section must mention REQUEST_CHANGES or REJECT verdict"
    assert (
        "MUST name" in section or "name the shortcut" in section
    ), "Shortcuts section must require naming the shortcut"


def test_each_shortcut_has_body():
    """Each of the 11 numbered items has a *Heuristic:* block."""
    text = load_persona("critic")
    section_start = text.find("## Fake-Done Shortcuts")
    section_end = text.find("Output format:")
    section = text[section_start:section_end]
    for i in range(1, 12):
        prefix = f"{i}. **"
        idx = section.find(prefix)
        assert idx >= 0, f"Numbered item {i} not found in shortcuts section"
        tail = section[idx:]
        assert "*Heuristic:*" in tail[:512], (
            f"Shortcut #{i} has no *Heuristic:* block within 512 chars"
        )
