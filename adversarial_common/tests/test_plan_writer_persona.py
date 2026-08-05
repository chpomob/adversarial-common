"""P13 — plan-writer persona: non-trivial criteria, caller table, full-branch review gate."""

import pytest

from adversarial_common import load_persona


@pytest.fixture(scope="module")
def plan_writer_text():
    return load_persona("plan-writer")


# --- AC1 (R1): non-trivial criteria + anti-overload ---


def test_plan_writer_has_branching_criterion(plan_writer_text):
    assert "branching" in plan_writer_text.lower()


def test_plan_writer_has_boundary_criterion(plan_writer_text):
    assert "boundary" in plan_writer_text.lower()


def test_plan_writer_has_compiler_criterion(plan_writer_text):
    assert "compiler" in plan_writer_text.lower()


def test_plan_writer_has_blast_radius_criterion(plan_writer_text):
    assert "blast radius" in plan_writer_text.lower()


def test_plan_writer_has_anti_overload_rule(plan_writer_text):
    assert "anti-overload" in plan_writer_text.lower() or (
        "trivial" in plan_writer_text.lower()
        and "cosmetic" in plan_writer_text.lower()
        and "deep analysis" in plan_writer_text.lower()
    )


# --- AC2 (R2): caller table columns ---


def test_plan_writer_has_caller_table(plan_writer_text):
    assert "caller table" in plan_writer_text.lower()


def test_plan_writer_has_file_column(plan_writer_text):
    assert "| File" in plan_writer_text


def test_plan_writer_has_function_method_column(plan_writer_text):
    assert "Function/Method" in plan_writer_text


def test_plan_writer_has_migration_note_column(plan_writer_text):
    assert "Migration Note" in plan_writer_text


# --- AC3 (R3): full-branch review gate ---


def test_plan_writer_has_full_branch_review(plan_writer_text):
    assert "full-branch review" in plan_writer_text.lower()



