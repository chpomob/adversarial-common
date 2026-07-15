"""Acceptance tests for the dependency-free static HTML report."""

import json
from pathlib import Path

import pytest

from adversarial_common.report import render_html_report, render_report


def _write_final(directory, payload):
    path = directory / "final.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_render_report_is_one_self_contained_file_with_all_sections(tmp_path):
    final_path = _write_final(tmp_path, {
        "verdict": "REQUEST_CHANGES",
        "summary": "Two issues remain",
        "findings": [{
            "id": "F1",
            "severity": "major",
            "summary": "Gate output is ignored",
            "confidence": "high",
            "basis": "code",
        }],
        "epistemic_labels": {
            "confidence": {"high": 1, "medium": 0, "low": 0},
            "basis": {"spec": 0, "code": 1, "inference": 0, "external": 0},
        },
        "costs": {
            "models": {"test-model": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "est_cost_usd": 0.001,
            }},
            "total": {"prompt_tokens": 10, "completion_tokens": 5, "est_cost_usd": 0.001},
        },
        "gates": [{
            "gate": "post_build",
            "command": "python -m pytest",
            "ok": False,
            "exit_code": 1,
            "log": "one failure",
        }],
        "warnings": [{"code": "sample_warning", "message": "Check this result"}],
    })
    (tmp_path / "01_architect.txt").write_text("raw architect output", encoding="utf-8")
    (tmp_path / "02_inspector.json").write_text('{"findings": []}', encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("must stay private", encoding="utf-8")

    report_path = render_report(final_path)

    assert report_path == tmp_path / "report.html"
    assert report_path.is_file()
    assert not (tmp_path / "report_files").exists()
    output = report_path.read_text(encoding="utf-8")
    for text in (
        "REQUEST_CHANGES", "Findings", "confidence: high", "basis: code",
        "Epistemic labels", "Costs", "Verification gates", "Warnings",
        "Raw phase outputs", "raw architect output", "Source artifact",
    ):
        assert text in output
    assert output.count("<details>") >= 2
    assert "must stay private" not in output
    lowered = output.lower()
    assert "<script" not in lowered
    assert "<link" not in lowered
    assert " src=" not in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "url(" not in lowered


def test_every_dynamic_report_field_and_raw_artifact_is_html_escaped(tmp_path):
    hostile = '<script title="x">&\'</script></details>'
    payload = {
        "verdict": hostile,
        hostile: hostile,
        "findings": [{
            "id": hostile,
            "severity": hostile,
            "confidence": "high",
            "basis": "code",
            hostile: hostile,
        }],
        "epistemic_distribution": {hostile: {hostile: hostile}},
        "costs": {hostile: {hostile: {hostile: hostile}}},
        "gates": [{"gate": hostile, "command": hostile, "log": hostile, "error": hostile}],
        "warnings": [{"code": hostile, "message": hostile}],
    }
    final_path = _write_final(tmp_path, payload)
    (tmp_path / "01_hostile.txt").write_text(hostile, encoding="utf-8")

    output = render_report(final_path).read_text(encoding="utf-8")

    assert hostile not in output
    assert "<script title=" not in output
    assert "</details></details>" not in output
    assert "&lt;script title=&quot;x&quot;&gt;&amp;&#x27;&lt;/script&gt;&lt;/details&gt;" in output


def test_missing_or_malformed_optional_fields_are_safely_represented(tmp_path):
    final_path = _write_final(tmp_path, {
        "status": "blocked",
        "findings": "unavailable",
        "epistemic_labels": ["unexpected"],
        "costs": "unknown",
        "gates": {"error": "not run"},
        "warnings": "partial output",
    })

    output = render_html_report(final_path).read_text(encoding="utf-8")

    assert "blocked" in output
    assert "unavailable" in output
    assert "unexpected" in output
    assert "unknown" in output
    assert "not run" in output
    assert "partial output" in output


def test_explicit_artifacts_are_contained_bounded_and_support_mappings(tmp_path):
    final_path = _write_final(tmp_path, {"verdict": "APPROVE"})
    outside = tmp_path.parent / "outside-report-secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    local = tmp_path / "phase.txt"
    local.write_text("local output", encoding="utf-8")

    output = render_report(final_path, artifacts=[local, outside]).read_text(encoding="utf-8")
    assert "local output" in output
    assert "outside secret" not in output

    output = render_report(final_path, artifacts={"custom phase": "mapped output"}).read_text(encoding="utf-8")
    assert "custom phase" in output
    assert "mapped output" in output


@pytest.mark.parametrize("contents", ["not JSON", "[]", "null", '"text"'])
def test_invalid_final_artifact_is_rejected(tmp_path, contents):
    final_path = tmp_path / "final.json"
    final_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError):
        render_report(final_path)


def test_report_runtime_imports_only_requested_standard_library_modules():
    source = (Path(__file__).parents[1] / "report.py").read_text(encoding="utf-8")

    assert "import requests" not in source
    assert "import yaml" not in source
    assert "jinja" not in source.lower()
