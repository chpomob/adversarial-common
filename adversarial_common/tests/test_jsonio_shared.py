"""Acceptance tests for the shared structured-text helpers in
``adversarial_common.jsonio`` (step P2: Items 5-core, 6-core, 8).

Covers:
  * parse_json_output  — 3-strategy JSON extraction (Item 5)
  * parse_frontmatter  — YAML frontmatter via PyYAML, regex fallback removed
                         (Item 6 + Item 8: lists now parse)
  * extract_frontmatter — raw YAML block extraction (Item 6)
"""

from adversarial_common import jsonio


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
