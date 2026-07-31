"""Contract-locking tests for the shared runtime boundary (R1-R5, R7-R12).

These tests encode the runtime contract that every adversarial skill depends
on. A failure here means a downstream skill's assumptions are broken.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

import adversarial_common as ac
from adversarial_common import costs, gates, runner


# -- R1: Threshold / env precedence ------------------------------------------

_THRESHOLD_KINDS = frozenset({"brief", "spec", "diff", "input"})


def test_check_context_kinds_match_default_threshold_keys():
    for kind in _THRESHOLD_KINDS:
        result = ac.check_context(kind, "x" * 200)
        assert "thresholds" in result
        assert set(result["thresholds"]) == {
            "min_chars", "min_tokens", "required_sections", "min_source_lines",
        }


def test_env_no_override_when_none_supplied():
    result = ac.check_context("brief", "x" * 100)
    assert result["thresholds"]["min_chars"] == 40


def test_env_override_takes_precedence():
    result = ac.check_context(
        "input", "short", {"min_chars": 10, "min_tokens": 0},
    )
    assert result["ok"] is False
    assert result["reason"] == "below_min_chars"
    assert result["thresholds"]["min_chars"] == 10


# -- R2: Retry / cap defaults -------------------------------------------------

def test_retry_defaults_are_present_and_positive():
    assert runner.DEFAULT_MAX_INPUT_CHARS > 0
    assert runner.DEFAULT_MAX_OUTPUT_CHARS > 0
    assert isinstance(runner.DEFAULT_MAX_INPUT_CHARS, int)
    assert isinstance(runner.DEFAULT_MAX_OUTPUT_CHARS, int)


def test_cap_constants_are_exported():
    assert ac.DEFAULT_MAX_INPUT_CHARS == runner.DEFAULT_MAX_INPUT_CHARS
    assert ac.DEFAULT_MAX_OUTPUT_CHARS == runner.DEFAULT_MAX_OUTPUT_CHARS


# -- R3: Cost / complexity schemas -------------------------------------------

def test_cost_ledger_summary_has_required_sections():
    ledger = costs.CostLedger(env={})
    summary = ledger.summary()
    for section in ("models", "phases", "personas", "total", "records"):
        assert section in summary, f"summary missing {section!r}"
    for field in ("prompt_tokens", "completion_tokens", "est_cost_usd"):
        assert field in summary["total"]


def test_complexity_result_has_required_fields():
    result = ac.estimate_complexity("x" * 100)
    for field in ("score", "level", "recommended_agents", "stats", "tier",
                   "max_agents", "summary"):
        assert field in result, f"complexity result missing {field!r}"
    assert result["level"] in ("trivial", "low", "medium", "high")


# -- R4: Epistemic normalization ---------------------------------------------

def test_valid_confidence_and_basis_are_exported_and_consistent():
    assert ac.VALID_CONFIDENCE == frozenset({"high", "medium", "low"})
    assert ac.VALID_BASIS == frozenset({"spec", "code", "inference", "external"})


def test_normalize_findings_defaults_on_missing_labels():
    payload = {"findings": [{"id": "X1"}]}
    result = ac.normalize_findings(payload)
    assert result["findings"][0]["confidence"] == "low"
    assert result["findings"][0]["basis"] == "inference"


def test_epistemic_distribution_shape():
    findings = [{"confidence": "high", "basis": "code"}]
    dist = ac.epistemic_distribution(findings)
    assert set(dist) == {"confidence", "basis", "combined"}
    assert dist["confidence"]["high"] == 1
    assert dist["combined"]["high/code"] == 1


# -- R5: Report fields -------------------------------------------------------

def test_render_report_requires_an_object_root(tmp_path):
    p = tmp_path / "final.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="object"):
        ac.render_report(p)


def test_render_report_accepts_any_verdict_without_becoming_non_html(tmp_path):
    p = tmp_path / "final.json"
    p.write_text('{"verdict":"CUSTOM_STATUS","summary":"ok"}')
    path = ac.render_report(p)
    assert path.suffix == ".html"
    content = path.read_text()
    assert "<!doctype html>" in content
    assert "CUSTOM_STATUS" in content


# -- R7: CI exit policy ------------------------------------------------------

_VALID_CI_EXIT_CODES = frozenset({
    ac.CI_EXIT_CLEAN, ac.CI_EXIT_INFRASTRUCTURE, ac.CI_EXIT_BLOCKING,
    ac.CI_EXIT_NON_BLOCKING, ac.CI_EXIT_CONTEXT_BLOCKED,
})


def test_ci_exit_codes_are_distinct_and_non_negative():
    assert len(_VALID_CI_EXIT_CODES) == 5
    assert all(code >= 0 for code in _VALID_CI_EXIT_CODES)


def test_ci_exit_code_returns_valid_constant():
    for verdict in ("APPROVE", "REJECT", "WARN", "ERROR", "CONTEXT_BLOCK"):
        code = ac.ci_exit_code(verdict)
        assert code in _VALID_CI_EXIT_CODES, f"{verdict} -> {code}"


def test_parse_fail_on_none_selects_findings():
    assert ac.parse_fail_on(None) == frozenset({"findings"})


def test_parse_fail_on_none_never_returns_clearly():
    assert ac.parse_fail_on("none") == frozenset()
    assert ac.parse_fail_on("never") == frozenset()


def test_ensure_final_payload_never_mutates_caller_dict():
    original = {"verdict": "PASS"}
    result = ac.ensure_final_payload(original)
    assert result is not original
    assert original == {"verdict": "PASS"}
    assert result["verdict"] == "PASS"
    assert result["status"] == "clean"


# -- R8: Research no-op ------------------------------------------------------

def test_run_research_disabled_returns_none():
    assert ac.run_research("query", enabled=False) is None


def test_run_research_noop_returns_none():
    result = ac.run_research("query", enabled=False, provider_cmd="nonexistent")
    assert result is None


# -- R9: Delegation metadata ------------------------------------------------

def test_run_parallel_empty_returns_list():
    assert ac.run_parallel([]) == []


def test_delegated_metadata_has_required_sections(monkeypatch):
    def fake_run_cli(cmd, stdin_text=None, **kwargs):
        if cmd == "decompose":
            return json.dumps({"tasks": [
                {"id": "a", "scope": "a.py"},
                {"id": "b", "scope": "b.py"},
            ]}), "", 0
        if cmd.startswith("worker-"):
            return json.dumps({"findings": [
                {"id": cmd, "confidence": "high", "basis": "code"}
            ]}), "", 0
        if cmd == "synthesize":
            return json.dumps({"findings": [
                {"id": "combined", "confidence": "high", "basis": "code"}
            ]}), "", 0
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)
    result = ac.run_delegated(
        "x" * 30000,
        {"cmd": "decompose"},
        lambda task: {"cmd": f"worker-{task['id']}"},
        {"cmd": "synthesize"},
        complexity={"level": "high", "recommended_agents": 3},
        concurrency=2,
    )
    for field in ("delegated", "mode", "status", "complexity",
                   "decomposition", "tasks_total", "tasks_dispatched",
                   "workers", "survivors", "partial", "synthesis"):
        assert field in result, f"delegated result missing {field!r}"


# -- R10: Stdlib-only import audit for gates / costs / runner ---------------

_MODULE_AUDIT_NAMES = {
    "gates": "gates.py",
    "costs": "costs.py",
    "providers": "providers.py",
    "runner": "runner.py",
}


@pytest.mark.parametrize("module_name,filename", _MODULE_AUDIT_NAMES.items())
def test_stdlib_only_imports(module_name, filename):
    path = Path(__file__).parents[1] / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module != "__future__"
    )
    imported_roots.discard("adversarial_common")  # self-imports

    assert imported_roots <= sys.stdlib_module_names, (
        f"{module_name} imports non-stdlib modules: "
        f"{imported_roots - sys.stdlib_module_names}"
    )


# -- R11: __init__.py exports full contract ----------------------------------

_INIT_EXPORTS = frozenset(ac.__all__)


def test_all_contract_exports_are_importable():
    for name in ac.__all__:
        assert hasattr(ac, name), f"{name!r} is listed in __all__ but not importable"


_CONTRACT_MUST_EXPORT = frozenset({
    # costs
    "BudgetReservation", "CostLedger", "UsageRecord",
    "estimate_tokens", "MODEL_PRICES", "PROVIDER_PRICE_ALIASES",
    # gates
    "check_context", "enforce_input_cap", "enforce_output_cap",
    "estimate_complexity", "post_build_gate", "post_fix_gate",
    "pre_build_gate", "TRUNCATION_MARKER",
    # jsonio
    "VALID_BASIS", "VALID_CONFIDENCE", "epistemic_distribution",
    "normalize_findings", "parse_json_output", "save_artifact",
    "resume_artifact", "write_final_json",
    # runner
    "run_cli", "run_parallel", "run_delegated", "run_research",
    "terminate_active_processes",
    "ci_exit_code", "ensure_final_payload", "parse_fail_on",
    "CI_EXIT_CLEAN", "CI_EXIT_BLOCKING", "CI_EXIT_NON_BLOCKING",
    "CI_EXIT_INFRASTRUCTURE", "CI_EXIT_CONTEXT_BLOCKED",
    "DEFAULT_MAX_INPUT_CHARS", "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_RESEARCH_MAX_QUERIES", "DEFAULT_RESEARCH_MAX_RESULTS",
    "DEFAULT_RESEARCH_TIMEOUT",
    "DEFAULT_RESEARCH_MAX_INPUT_CHARS", "DEFAULT_RESEARCH_MAX_OUTPUT_CHARS",
    "COST_BUDGET_EXIT_CODE", "RunResult", "build_final_payload",
    "ci_mode", "ci_print", "fail_phase",
    # report
    "render_html_report", "render_report",
    # providers
    "classify_transient_error", "detect_provider",
    "extract_usage_metadata", "is_transient_error",
    "inject_persona", "persona_for_role", "resolve_role_cmd",
    "enhance_cmd_for_project", "default_wrapper_cmd", "run_cmd",
    # snapshot
    "snapshot_workdir",
    # personas
    "PERSONAS_DIR", "load_persona", "persona_path",
    # jsonio extras
    "strip_json_wrapper", "extract_frontmatter", "parse_frontmatter",
})


def test_contract_exports_match_expected():
    missing = _CONTRACT_MUST_EXPORT - _INIT_EXPORTS
    assert not missing, f"missing from __all__: {sorted(missing)}"


def test_process_termination_api_is_exported():
    assert ac.terminate_active_processes is runner.terminate_active_processes


# -- R12: Shared fixtures are available -------------------------------------

def test_tmp_workdir_fixture_has_project_marker(tmp_workdir):
    assert (tmp_workdir / "pyproject.toml").is_file()


def test_clean_ledger_fixture_has_no_records(clean_ledger):
    assert len(clean_ledger.records) == 0
    assert clean_ledger.total_cost_usd == 0.0


def test_priced_ledger_fixture_has_test_model_price(priced_ledger):
    price = priced_ledger.price_for("test-model")
    assert price["prompt"] == 1.0
    assert price["completion"] == 2.0
