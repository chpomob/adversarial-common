"""P10 — spec-writer persona: template, retry, callers, non-trivial criteria."""

import pytest

from adversarial_common import load_persona


@pytest.fixture(scope="module")
def spec_writer_text():
    return load_persona("spec-writer")


# --- AC1 (R1/R2): template keywords ---


def test_spec_writer_has_requirements_template(spec_writer_text):
    assert "## Requirements" in spec_writer_text


def test_spec_writer_has_acceptance_criteria_template(spec_writer_text):
    assert "## Acceptance criteria" in spec_writer_text


def test_spec_writer_mentions_yaml_frontmatter(spec_writer_text):
    assert "YAML frontmatter" in spec_writer_text or "frontmatter" in spec_writer_text


def test_spec_writer_has_numeric_only_rule(spec_writer_text):
    assert "numeric only" in spec_writer_text


def test_spec_writer_has_every_r_has_at_least_one_ac(spec_writer_text):
    assert "every R has at least one AC" in spec_writer_text.lower() or (
        "≥1" in spec_writer_text and "orphan" in spec_writer_text
    )


# --- AC2 (R3): retry keywords ---


def test_spec_writer_has_retry(spec_writer_text):
    assert "retry" in spec_writer_text.lower()


def test_spec_writer_has_max_retries_default_3(spec_writer_text):
    assert "max retries" in spec_writer_text.lower()
    assert "default 3" in spec_writer_text or "default: 3" in spec_writer_text


def test_spec_writer_has_validator_error_feedback(spec_writer_text):
    assert "validator error" in spec_writer_text.lower()


# --- AC3 (R4): non-trivial criteria terms ---


def test_spec_writer_has_branching(spec_writer_text):
    assert "branching" in spec_writer_text.lower()


def test_spec_writer_has_boundary(spec_writer_text):
    assert "boundary" in spec_writer_text.lower()


def test_spec_writer_has_compiler(spec_writer_text):
    assert "compiler" in spec_writer_text.lower()


def test_spec_writer_has_blast_radius(spec_writer_text):
    assert "blast radius" in spec_writer_text.lower()


# --- AC4 (R5): caller table columns ---


def test_spec_writer_has_caller_table_file_column(spec_writer_text):
    assert "| File" in spec_writer_text


def test_spec_writer_has_caller_table_function_method_column(spec_writer_text):
    assert "Function/Method" in spec_writer_text


def test_spec_writer_has_caller_table_migration_note_column(spec_writer_text):
    assert "Migration Note" in spec_writer_text
