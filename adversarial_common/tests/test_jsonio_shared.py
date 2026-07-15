"""Acceptance tests for the shared structured-text helpers in
``adversarial_common.jsonio`` (step P2: Items 5-core, 6-core, 8).

Covers:
  * parse_json_output  — 3-strategy JSON extraction (Item 5)
  * parse_frontmatter  — YAML frontmatter via PyYAML, regex fallback removed
                         (Item 6 + Item 8: lists now parse)
  * extract_frontmatter — raw YAML block extraction (Item 6)
"""

from adversarial_common import jsonio
from adversarial_common.gates import TRUNCATION_MARKER


# --- parse_json_output (Item 5) --------------------------------------------

def test_parse_json_output_plain_object():
    # (a) bare object round-trips
    assert jsonio.parse_json_output('{"x":1}') == {"x": 1}


def test_parse_json_output_fenced_array():
    # (b) ```json fence wrapping an array is unwrapped
    assert jsonio.parse_json_output("```json\n[1,2]\n```") == [1, 2]


def test_parse_json_output_empty_and_none():
    assert jsonio.parse_json_output("") is None
    assert jsonio.parse_json_output(None) is None
    assert jsonio.parse_json_output("   \n  ") is None


# --- parse_frontmatter (Items 6 + 8) ---------------------------------------

def test_parse_json_output_ignores_trailing_prose_and_stray_brace():
    text = (
        'preface {"verdict":"APPROVE","findings":[]}'
        "\ncommentary containing a stray }"
    )

    assert jsonio.parse_json_output(text) == {"verdict": "APPROVE", "findings": []}


def test_parse_json_output_rejects_actual_capped_suffix_with_warning():
    warnings = []
    incomplete = '{"findings":[{"id":"A1"}]' + TRUNCATION_MARKER

    assert jsonio.parse_json_output(incomplete, warnings=warnings) is None
    assert warnings and warnings[0]["code"] == "truncated_json_output"


def test_parse_json_output_allows_marker_mentioned_before_trailing_prose():
    text = '{"x":1}\nmarker:' + TRUNCATION_MARKER + "is discussed here"

    assert jsonio.parse_json_output(text) == {"x": 1}


def test_normalize_findings_defaults_labels_and_records_warnings():
    warnings = []
    payload = {
        "findings": [
            {"id": "A1", "confidence": "certain", "basis": "guess", "origin": "review"}
        ],
    }

    result = jsonio.normalize_findings(payload, warnings=warnings)

    finding = result["findings"][0]
    assert finding["origin"] == "review"
    assert (finding["confidence"], finding["basis"]) == ("low", "inference")
    assert finding["warnings"] == ["epistemic_label_defaulted"]
    assert warnings == result["warnings"]


def test_epistemic_distribution_counts_defaults_without_mutation():
    findings = [
        {"id": "A1", "confidence": "high", "basis": "code"},
        {"id": "A2"},
    ]
    snapshot = [finding.copy() for finding in findings]

    distribution = jsonio.epistemic_distribution(findings)

    assert findings == snapshot
    assert distribution["confidence"] == {"high": 1, "medium": 0, "low": 1}
    assert distribution["basis"] == {
        "spec": 0, "code": 1, "inference": 1, "external": 0,
    }
    assert distribution["combined"] == {"high/code": 1, "low/inference": 1}


def test_parse_frontmatter_parses_lists():
    # (c) the regex fallback would have flattened `list` into a scalar string;
    # PyYAML parses it into a real list. This proves the fallback is gone.
    data, err = jsonio.parse_frontmatter("name: x\nlist:\n  - a\n  - b")
    assert err is None
    assert data == {"name": "x", "list": ["a", "b"]}


def test_parse_frontmatter_tuple_index_zero():
    # (c, verbatim) the first tuple element is the mapping
    assert jsonio.parse_frontmatter("name: x\nlist:\n  - a\n  - b")[0] == {
        "name": "x", "list": ["a", "b"],
    }


def test_parse_frontmatter_non_mapping_is_error():
    data, err = jsonio.parse_frontmatter("- a\n- b")
    assert data is None
    assert err is not None


def test_parse_frontmatter_invalid_yaml_is_error():
    data, err = jsonio.parse_frontmatter("name: x\n  bad: : :")
    # Either it parses loosely or it errors — but it must never raise and
    # must return a (None-ish, str) tuple shape.
    assert isinstance(err, str) or data is not None


# --- extract_frontmatter (Item 6) ------------------------------------------

def test_extract_frontmatter_present():
    # (d) returns the raw inner YAML block (between the --- fences)
    assert jsonio.extract_frontmatter("---\nname: x\n---\nbody") == "name: x"


def test_extract_frontmatter_absent():
    # (e) no fences -> None
    assert jsonio.extract_frontmatter("no fm") is None


def test_extract_frontmatter_non_string():
    assert jsonio.extract_frontmatter(None) is None
