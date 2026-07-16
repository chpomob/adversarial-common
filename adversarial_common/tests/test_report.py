"""Acceptance tests for the dependency-free static HTML report."""

import json
from pathlib import Path
import threading

import pytest

from adversarial_common.report import render_html_report, render_report
from adversarial_common.runner import (
    RunResult,
    collect_provider_history,
    ensure_final_payload,
)


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


def _provider_decision(phase, alias="codex", **overrides):
    decision = {
        "phase": phase,
        "alias": alias,
        "quota_state": "OK",
        "fallback": False,
        "forced": False,
        "reason": "selected first eligible provider",
        "raw_snapshot": {"private_quota": {"remaining": 42}},
    }
    decision.update(overrides)
    return decision


class _DeepcopyFailure:
    def __deepcopy__(self, memo):
        raise RuntimeError("cannot copy provider state")


def test_collect_provider_history_validates_and_preserves_execution_order():
    results = [
        RunResult(("", "", 0), {
            "provider_decision": _provider_decision("build")
        }),
        RunResult(("", "", 0)),
        RunResult(("", "", 0), {
            "provider_decision": _provider_decision(
                "review", "claude", fallback=True,
                reason="selected fallback provider after earlier rejection",
            )
        }),
        RunResult(("", "", 0), {"provider_decision": {"phase": "bad"}}),
        RunResult(("", "", 0), {
            "provider_decision": _provider_decision(
                "verify", None, quota_state="UNKNOWN",
                reason="no provider available",
            )
        }),
    ]

    history = collect_provider_history(results)

    assert [entry["phase"] for entry in history] == [
        "build", "review", "verify"
    ]
    assert history[1]["alias"] == "claude"
    assert history[1]["fallback"] is True
    assert history[0]["raw_snapshot"] == {
        "private_quota": {"remaining": 42}
    }


def test_ensure_final_payload_injects_normalized_provider_history():
    decision = _provider_decision("build")

    payload = ensure_final_payload(
        verdict="APPROVED", provider_history=[decision]
    )

    assert payload["provider_history"] == [decision]
    assert payload["provider_history"][0] is not decision
    assert payload["status"] == "clean"


@pytest.mark.parametrize("value", [threading.Lock(), _DeepcopyFailure()])
def test_non_deepcopyable_provider_snapshot_is_omitted_safely(value):
    decision = _provider_decision("build", raw_snapshot={"value": value})

    history = collect_provider_history([
        RunResult(("", "", 0), {"provider_decision": decision})
    ])
    payload = ensure_final_payload(
        verdict="APPROVED", provider_history=[decision]
    )

    assert history == []
    assert payload["provider_history"] == []


def test_provider_history_is_rendered_in_markdown_without_raw_snapshot(tmp_path):
    final_path = _write_final(tmp_path, {
        "verdict": "APPROVED",
        "provider_history": [
            _provider_decision("build"),
            _provider_decision(
                "review", "claude", quota_state="DRAINING",
                fallback=True,
                reason="selected fallback provider after earlier rejection",
            ),
        ],
    })

    render_report(final_path)
    output = (tmp_path / "final.md").read_text(encoding="utf-8")

    assert "## Provider history" in output
    assert "| Phase | Provider | State | Fallback | Forced | Reason |" in output
    assert (
        "| review | claude | DRAINING | Yes | No | "
        "selected fallback provider after earlier rejection |"
    ) in output
    assert "raw_snapshot" not in output
    assert "private_quota" not in output


def test_provider_history_reports_forced_selection(tmp_path):
    final_path = _write_final(tmp_path, {
        "verdict": "APPROVED",
        "provider_history": [
            _provider_decision(
                "build", forced=True, reason="forced primary provider"
            )
        ],
    })

    render_report(final_path)

    markdown = (tmp_path / "final.md").read_text(encoding="utf-8")
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "| build | codex | OK | No | Yes | forced primary provider |" in markdown
    assert "<th>Fallback</th><th>Forced</th><th>Reason</th>" in html
    assert "<td>No</td><td>Yes</td><td>forced primary provider</td>" in html


def test_provider_history_accepts_missing_optional_alias(tmp_path, capsys):
    decision = _provider_decision(
        "build", None, quota_state="UNKNOWN", reason="no provider available"
    )
    del decision["alias"]
    final_path = _write_final(tmp_path, {
        "verdict": "APPROVED",
        "provider_history": [decision],
    })

    render_report(final_path)

    markdown = (tmp_path / "final.md").read_text(encoding="utf-8")
    assert "| build | — | UNKNOWN | No | No | no provider available |" in markdown
    assert capsys.readouterr().err == ""


def test_provider_history_html_table_escapes_all_dynamic_cells(tmp_path):
    hostile = '<img src=x onerror="alert(1)">'
    final_path = _write_final(tmp_path, {
        "verdict": "APPROVED",
        "provider_history": [
            _provider_decision(hostile, hostile, reason=hostile)
        ],
    })

    output = render_html_report(final_path).read_text(encoding="utf-8")

    assert "<h2>Provider history</h2>" in output
    assert "<th>Phase</th><th>Provider</th><th>State</th>" in output
    assert hostile not in output
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in output
    assert "private_quota" not in output


@pytest.mark.parametrize("provider_history", [None, []])
def test_empty_provider_history_has_an_english_note(tmp_path, provider_history):
    payload = {"verdict": "APPROVED"}
    if provider_history is not None:
        payload["provider_history"] = provider_history
    final_path = _write_final(tmp_path, payload)

    render_report(final_path)

    markdown = (tmp_path / "final.md").read_text(encoding="utf-8")
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "No provider history recorded." in markdown
    assert "No provider history recorded." in html


def test_malformed_provider_history_is_skipped_with_stderr_warning(
    tmp_path, capsys
):
    final_path = _write_final(tmp_path, {
        "verdict": "APPROVED",
        "provider_history": [
            {"alias": "missing-phase"},
            _provider_decision("review"),
        ],
    })

    render_report(final_path)

    markdown = (tmp_path / "final.md").read_text(encoding="utf-8")
    warning = capsys.readouterr().err
    assert "missing-phase" not in markdown
    assert "| review | codex | OK | No | No |" in markdown
    assert "Provider history validation warning" in warning
    assert "missing or invalid 'phase'" in warning
