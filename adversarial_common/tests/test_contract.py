"""Contract-locking tests for the shared runtime boundary (R1-R5, R7-R12).

These tests encode the runtime contract that every adversarial skill depends
on. A failure here means a downstream skill's assumptions are broken.
"""

from __future__ import annotations

import ast
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import adversarial_common as ac
from adversarial_common import costs, gates, gitops, pipeline_base as pb, providers, runner


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
    # pipeline lifecycle
    "SETTLED_STATUSES", "FinishPolicy", "GitSetupPolicy", "PreflightPolicy",
    "PreflightResult", "RestorePolicy", "RestoreResult",
    "RetrospectivePolicy", "banner", "ci_exit_from_final",
    "ensure_finding_ids", "finish_pipeline", "is_settled_status",
    "log_retrospective", "non_negative_int", "phase_failure",
    "positive_int", "preflight", "record_phase", "restore_git", "setup_git",
    "threshold_overrides", "unresolved_findings", "write_json",
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


def test_pipeline_lifecycle_exports_are_package_level_aliases():
    from adversarial_common import pipeline_base

    for name in pipeline_base.__all__:
        assert getattr(ac, name) is getattr(pipeline_base, name)


def test_pipeline_base_dependency_boundary_excludes_consumers_and_providers():
    path = Path(__file__).parents[1] / "pipeline_base.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden_fragments = {
        "providers", "phases", "adversarial_spec", "adversarial_plan",
        "adversarial_loop", "adversarial_review",
    }
    assert not {
        name for name in imported
        if any(fragment in name for fragment in forbidden_fragments)
    }


def _assert_p22_consumers(consumers):
    """Assert each existing consumer path avoids ``pipeline_base``.

    A missing consumer is skipped individually (``pytest.skip`` for that path,
    caught locally) rather than aborting the whole scan — the remaining
    consumers must still be checked.
    """
    for path in consumers:
        if not path.is_file():
            try:
                pytest.skip(
                    f"sibling skill repo not found: {path} — "
                    "this gate only runs in a multi-repo dev workspace"
                )
            except pytest.skip.Exception:
                continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "adversarial_common.pipeline_base"
            for node in ast.walk(tree)
        ), f"P22 must not migrate {path}"


def test_p22_does_not_import_pipeline_base_in_consumers():
    skills = Path(__file__).parents[3]
    consumers = [
        skills / "adversarial-spec/scripts/adversarial_spec.py",
        skills / "adversarial-plan/scripts/adversarial_plan.py",
        skills / "adversarial-code-loop/scripts/adversarial_loop_v4.py",
        skills / "adversarial-code-review/scripts/adversarial_review.py",
    ]
    # ponytail: skip-only gate — siblings absent means this is a standalone
    # checkout; the gate can't run there, so skip rather than assert.
    if not skills.is_dir():
        pytest.skip(
            f"sibling skill repo root not found: {skills} — "
            "this gate only runs in a multi-repo dev workspace"
        )
    _assert_p22_consumers(consumers)


def test_p22_skips_only_missing_consumer_and_still_checks_the_rest(tmp_path):
    clean_a = tmp_path / "clean_a.py"
    clean_a.write_text("import os\n")
    clean_b = tmp_path / "clean_b.py"
    clean_b.write_text("import sys\n")
    violating = tmp_path / "violating.py"
    violating.write_text(
        "from adversarial_common.pipeline_base import setup_git\n"
    )
    missing = tmp_path / "does_not_exist.py"

    # The missing sibling must not abort the scan: the violating file (which
    # comes after it in the list) is still reached and still fails the gate.
    with pytest.raises(AssertionError, match="P22 must not migrate"):
        _assert_p22_consumers([clean_a, missing, clean_b, violating])


# -- Review2 fixes: M1-M4 majors, m2-m3 minors -------------------------------


def _raw_git(workdir, *args):
    proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=workdir)
    return proc.stdout.strip(), proc.stderr.strip(), proc.returncode


def test_stash_identity_is_sha_not_positional(tmp_path):
    # M1: stash_dirty's returned SHA must stay bound to its own stash even
    # after a second stash is pushed on top and shifts stash@{0}.
    gitops.auto_init(tmp_path)
    (tmp_path / "f.txt").write_text("v1")
    gitops.commit_all(tmp_path, "base")

    (tmp_path / "f.txt").write_text("v2-first")
    first_sha = gitops.stash_dirty(tmp_path)

    (tmp_path / "f.txt").write_text("v3-second")
    second_sha = gitops.stash_dirty(tmp_path)
    assert first_sha != second_sha

    gitops.unstash(tmp_path, first_sha)

    assert (tmp_path / "f.txt").read_text() == "v2-first"
    remaining, _, _ = _raw_git(tmp_path, "stash", "list", "--format=%H")
    assert remaining.splitlines() == [second_sha]


def test_truncate_input_with_large_persona_stays_under_cap(tmp_path, monkeypatch):
    # M2: the persona must never be truncated to make room — the BODY is
    # capped against the budget left over after the persona + delimiter
    # overhead, so the final stdin (persona + fences + body) stays <= limit.
    persona_file = tmp_path / "persona.md"
    persona_text = "PERSONA " + ("x" * 2000)
    persona_file.write_text(persona_text)
    body = "y" * 5000

    def fake_execute(argv, stdin_text, timeout, cwd):
        return stdin_text or "", "", 0, True, False

    monkeypatch.setattr(runner, "_execute_attempt", fake_execute)

    _, framed_empty = providers.inject_persona(
        ["fake-cmd"], str(persona_file), "", delimiter=True
    )
    overhead = len(framed_empty)
    limit = overhead + 200  # room for only part of the oversized body

    result = runner.run_cli(
        "fake-cmd",
        stdin_text=body,
        persona_file=str(persona_file),
        max_input_chars=limit,
        max_output_chars=limit + 1000,
        truncate_input=True,
        max_retries=0,
    )
    stdout, _stderr, code = result[0], result[1], result[2]
    assert code == 0
    assert len(stdout) <= limit
    assert persona_text in stdout
    assert providers._UNTRUSTED_BODY_END in stdout


def test_default_wrapper_cmd_no_raise_without_wrapper(monkeypatch, tmp_path):
    # M3: no wrapper on PATH and no fallback file must not raise — module
    # import and --help must keep working without a configured wrapper.
    monkeypatch.setattr(providers.shutil, "which", lambda executable: None)
    fake_home = tmp_path / "empty-home"

    command = providers.default_wrapper_cmd(environ={"HOME": str(fake_home)})

    assert shlex.split(command) == [providers._CLAUDE_TMUX_EXECUTABLE]


class _FakeGitAdapter:
    """Minimal git_adapter double for setup_git's happy path."""

    def __init__(self):
        self.resolved_stash = "resolved-stash-sha"

    def detect_enclosing_repo(self, workdir):
        return "/repo"

    def ensure_git_identity(self, workdir):
        pass

    def get_current_branch(self, workdir):
        return "main"

    def stash_dirty(self, workdir, *, on_pushed=None):
        if on_pushed is not None:
            on_pushed("push-marker")
        return self.resolved_stash

    def create_loop_branch(self, workdir, feature, parent, prefix="loop"):
        return f"{prefix}/{feature}/1"

    def checkout(self, workdir, branch):
        pass

    def record_branch_point(self, workdir, parent):
        return "branch-point-sha"

    def ensure_gitignore(self, workdir, entry):
        pass


def test_setup_git_no_state_records_stash_in_result():
    # M4: setup_git(state=None) must still expose the recorded stash id
    # through its RESULT, not only into a throwaway local the caller can
    # never see.
    fake = _FakeGitAdapter()
    result = pb.setup_git(
        "/safe", "feature", None, policy=pb.GitSetupPolicy(git_adapter=fake),
    )
    assert result["exit_code"] == 0
    assert result["stash_id"] == "resolved-stash-sha"


def test_override_tilde_uses_injected_home():
    # m2: the wrapper-path override must expand ``~`` against the injected
    # environ HOME, not the real process HOME.
    command = providers.default_wrapper_cmd(
        environ={"HOME": "/fake", providers.CLAUDE_TMUX_PATH_ENV: "~/w.py"}
    )
    assert shlex.split(command) == ["/fake/w.py"]


def test_remove_worktree_raises_on_git_failure(tmp_path, monkeypatch):
    # m3: a genuine git failure (not the already-gone case, excluded by the
    # existence check) must raise GitError instead of being swallowed.
    wt_path = tmp_path / "wt"
    wt_path.mkdir()

    def fake_run(workdir, args, timeout=gitops.DEFAULT_GIT_TIMEOUT):
        return "", "fatal: could not remove worktree", 1

    monkeypatch.setattr(gitops, "_run", fake_run)
    with pytest.raises(gitops.GitError):
        gitops.remove_worktree(str(tmp_path), str(wt_path))


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
