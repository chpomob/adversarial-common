"""Pytest bootstrap: put the skill root on sys.path so the
``adversarial_common`` package is importable from the tests."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# -- Shared fixtures available to all adversarial-common tests --


@pytest.fixture
def tmp_workdir() -> Generator[Path, None, None]:
    """Temporary directory with a minimal pyproject.toml project marker."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "pyproject.toml").write_text("[project]\nname = 'adversarial-test'\n")
        yield root


@pytest.fixture
def clean_ledger():
    """Fresh CostLedger with no records and no environment overrides."""
    from adversarial_common.costs import CostLedger
    return CostLedger(env={})


@pytest.fixture
def priced_ledger():
    """CostLedger with a single priced model for deterministic tests."""
    from adversarial_common.costs import CostLedger
    return CostLedger(
        prices={"test-model": {"prompt": 1.0, "completion": 2.0}},
        env={},
    )
