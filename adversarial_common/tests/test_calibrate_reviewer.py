"""Tests for the P20 calibrate_reviewer.py CLI runner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "calibrate_reviewer.py"
_CORPUS = Path(__file__).resolve().parents[2] / "references" / "calibration_corpus"
_NULL_REVIEW_CMD = f'{sys.executable} -c "print(\'[]\')"'


def _run_cli(*extra_args):
    return subprocess.run(
        [
            sys.executable, str(_SCRIPT),
            "--review-cmd", _NULL_REVIEW_CMD,
            "--corpus", str(_CORPUS),
            *extra_args,
        ],
        capture_output=True, text=True,
    )


def test_cli_emits_json():
    """AC1: --output json produces valid JSON with the expected metric keys."""
    result = _run_cli("--output", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    for key in ("precision", "recall", "position_bias", "self_enhancement"):
        assert key in payload


def test_cli_reproducible():
    """AC2: two runs over the same corpus + reviewer produce identical metrics."""
    first = _run_cli("--output", "json")
    second = _run_cli("--output", "json")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout) == json.loads(second.stdout)


def test_cli_writes_to_output_file(tmp_path):
    out_path = tmp_path / "metrics.json"

    result = _run_cli("--output", str(out_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    payload = json.loads(out_path.read_text())
    assert "precision" in payload


def test_cli_exits_nonzero_on_missing_corpus(tmp_path):
    missing = tmp_path / "does-not-exist"

    result = subprocess.run(
        [
            sys.executable, str(_SCRIPT),
            "--review-cmd", _NULL_REVIEW_CMD,
            "--corpus", str(missing),
        ],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "does-not-exist" in result.stderr


def test_cli_exits_nonzero_on_malformed_gold(tmp_path):
    fixture_dir = tmp_path / "bad-fixture"
    fixture_dir.mkdir()
    (fixture_dir / "diff.txt").write_text("diff --git a/a.py b/a.py\n")
    (fixture_dir / "gold.json").write_text("[]")  # must be a JSON object, not a list

    result = subprocess.run(
        [
            sys.executable, str(_SCRIPT),
            "--review-cmd", _NULL_REVIEW_CMD,
            "--corpus", str(tmp_path),
        ],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
