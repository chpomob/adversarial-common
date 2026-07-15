"""Schema tests for epistemic labels in reviewer personas."""

import json
import re

import pytest

from adversarial_common import load_persona


FINDING_PERSONAS = (
    "architect",
    "inspector",
    "critic",
    "cross_review",
    "plan-challenger",
    "spec-challenger",
)
DECISION_PERSONAS = {
    "verifier": "results",
    "judge": "decisions",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
BASIS_TYPES = {"spec", "code", "inference", "external"}


def _load_json_example(persona_name):
    """Load and parse the first fenced JSON schema in a persona."""
    text = load_persona(persona_name)
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    assert match is not None, f"{persona_name} has no fenced JSON schema"
    return text, json.loads(match.group(1))


@pytest.mark.parametrize("persona_name", FINDING_PERSONAS)
def test_finding_persona_schema_requires_epistemic_labels(persona_name):
    text, schema = _load_json_example(persona_name)

    assert isinstance(schema["findings"], list) and schema["findings"]
    finding = schema["findings"][0]
    assert set(finding["confidence"].split("|")) == CONFIDENCE_LEVELS
    assert set(finding["basis"].split("|")) == BASIS_TYPES
    assert "Evidence must match `basis`" in text
    for basis in BASIS_TYPES:
        assert f"- `{basis}`:" in text


@pytest.mark.parametrize("persona_name,list_key", DECISION_PERSONAS.items())
def test_decision_persona_schema_preserves_labels_and_distribution(
    persona_name, list_key
):
    _, schema = _load_json_example(persona_name)

    assert isinstance(schema[list_key], list) and schema[list_key]
    decision = schema[list_key][0]
    assert set(decision["confidence"].split("|")) == CONFIDENCE_LEVELS
    assert set(decision["basis"].split("|")) == BASIS_TYPES
    distribution = schema["epistemic_distribution"]
    assert set(distribution["confidence"]) == CONFIDENCE_LEVELS
    assert set(distribution["basis"]) == BASIS_TYPES


@pytest.mark.parametrize(
    "persona_name",
    ("synthesis", "verifier", "judge"),
)
def test_epistemic_consumers_report_distribution_and_downweight_inference(
    persona_name,
):
    text = load_persona(persona_name)

    assert "inference-only" in text
    assert "confidence" in text and "basis" in text
    assert "corroborat" in text or "determine" in text
    if persona_name == "synthesis":
        assert "**Epistemic Distribution**" in text
        for label in CONFIDENCE_LEVELS | BASIS_TYPES:
            assert f"`{label}`" in text
    else:
        assert "`epistemic_distribution`" in text
        assert "Evidence must match `basis`" in text
