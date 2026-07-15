"""Shared engine for the adversarial skill suite."""

from pathlib import Path

from .costs import BudgetReservation, CostLedger, UsageRecord
from .gates import (
    check_context,
    enforce_input_cap,
    enforce_output_cap,
    estimate_complexity,
    post_build_gate,
    post_fix_gate,
    pre_build_gate,
)


PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"


def persona_path(name):
    """Return the absolute path to a named persona file."""
    path = PERSONAS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Persona not found: {path}")
    return str(path)


def load_persona(name):
    """Return the text of a named persona."""
    return Path(persona_path(name)).read_text()


__all__ = [
    "BudgetReservation", "CostLedger", "PERSONAS_DIR", "UsageRecord", "check_context",
    "enforce_input_cap", "enforce_output_cap", "estimate_complexity",
    "load_persona", "persona_path", "post_build_gate", "post_fix_gate",
    "pre_build_gate",
]
