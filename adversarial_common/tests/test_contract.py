"""Contract-locking tests for the shared runtime boundary (R1-R5, R7-R12).

These tests encode the runtime contract that every adversarial skill depends
on. A failure here means a downstream skill's assumptions are broken.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import adversarial_common as ac
from adversarial_common import (
    contract as _contract,
    costs,
    gates,
    gitops,
    pipeline_base as pb,
    providers,
    runner,
)
from adversarial_common.contract import run_directive


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

    def stash_dirty(self, workdir, *, on_pushed=None, state=None):
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


# -- P2: ac-directive parser (adversarial_common.contract) ------------------
#
# The directive parser binds spec.md acceptance criteria to machine-enforced
# checks. These tests cover the happy-path round-trip (AC1), every validation
# error case (AC1), the prose-only AC (AC2), and the location-binding rule
# (AC3).


def _one_directive_spec(info, body):
    """A spec whose AC1 carries a single fenced directive."""
    return (
        "# Spec\n\n## Acceptance criteria\n\n"
        "- AC1: criterion\n"
        f"  ```{info}\n{body}\n  ```\n"
    )


# (name) -> (info string, directive body); each must yield exactly one parse
# error whose cause contains the matching substring in _EXPECTED_CAUSE.
_DIRECTIVE_ERROR_CASES = {
    "unknown kind": (
        "ac-directive", "ac: AC1\nkind: frobnicate\ncommand: grep foo",
    ),
    "missing command": (
        "ac-directive", "ac: AC1\nkind: grep",
    ),
    "missing ac": (
        "ac-directive", "kind: grep\ncommand: grep foo",
    ),
    "unbound ac": (
        "ac-directive", "ac: AC99\nkind: grep\ncommand: grep foo",
    ),
    "bad info string": (
        "ac-directive x", "ac: AC1\nkind: grep\ncommand: grep foo",
    ),
    "malformed YAML": (
        "ac-directive", "ac: AC1\nfoo: [unclosed",
    ),
    "illegal expected": (
        "ac-directive",
        "ac: AC1\nkind: no-diff\nexpected: 0\ncommand: git status",
    ),
}

_EXPECTED_CAUSE = {
    "unknown kind": "unknown kind",
    "missing command": "missing command",
    "missing ac": "missing ac",
    "unbound ac": "unbound ac",
    "bad info string": "bad info string",
    "malformed YAML": "malformed YAML",
    "illegal expected": "illegal expected",
}


def test_parses_directives():
    # AC1: one grep, one shell (block-scalar command + non-default
    # expected/timeout), one no-diff (quoted multi-line command) round-trip
    # verbatim; then every error case is a parse error with a cause.
    happy = (
        "# Spec\n\n"
        "## Requirements\n\n- R1: parse directives\n\n"
        "## Acceptance criteria\n\n"
        "- AC1: `test_parses_directives` — round-trip\n"
        "  ```ac-directive\n"
        "  ac: AC1\n"
        "  kind: grep\n"
        '  command: grep -n "foo" src/*.py\n'
        "  ```\n"
        "  ```ac-directive\n"
        "  ac: AC1\n"
        "  kind: shell\n"
        "  expected: 0\n"
        "  timeout: 120\n"
        "  command: |\n"
        "    set -e\n"
        "    ./build.sh\n"
        "    ./test.sh\n"
        "  ```\n"
        "  ```ac-directive\n"
        "  ac: AC1\n"
        "  kind: no-diff\n"
        '  command: "git diff --quiet\\nexit 0"\n'
        "  ```\n"
        "- AC2: other\n"
    )
    res = _contract.parse_spec(happy)
    assert res.ok, res.errors
    assert len(res.directives) == 3
    by_kind = {d.kind: d for d in res.directives}

    grep = by_kind["grep"]
    assert grep.ac == "AC1"
    assert grep.command == b'grep -n "foo" src/*.py'
    assert grep.timeout == 60            # default
    assert grep.files == ("*",)          # default for grep
    assert grep.expected is None

    shell = by_kind["shell"]
    assert shell.command == b"set -e\n./build.sh\n./test.sh\n"  # block-scalar verbatim
    assert shell.expected == 0           # non-default
    assert shell.timeout == 120          # non-default
    assert shell.files == ()             # files not valid for shell

    nodiff = by_kind["no-diff"]
    assert nodiff.command == b"git diff --quiet\nexit 0"  # quoted multi-line verbatim
    assert nodiff.files == ("*",)        # default for no-diff
    assert nodiff.expected is None

    # every error case surfaces as a parse error with a cause, no directive
    for name, (info, body) in _DIRECTIVE_ERROR_CASES.items():
        err_res = _contract.parse_spec(_one_directive_spec(info, body))
        assert not err_res.ok, f"{name}: expected an error"
        assert err_res.errors, f"{name}: no errors recorded"
        cause = err_res.errors[0].cause
        assert _EXPECTED_CAUSE[name] in cause, (
            f"{name}: expected cause mentioning {_EXPECTED_CAUSE[name]!r}, "
            f"got {cause!r}"
        )
        assert err_res.directives == [], (
            f"{name}: a bad directive must not be produced"
        )

    # duplicate (ac, kind): the first directive is kept, the second is flagged.
    dup = (
        "# Spec\n\n## Acceptance criteria\n\n- AC1: criterion\n"
        "  ```ac-directive\nac: AC1\nkind: grep\ncommand: grep foo\n  ```\n"
        "  ```ac-directive\nac: AC1\nkind: grep\ncommand: grep bar\n  ```\n"
    )
    dup_res = _contract.parse_spec(dup)
    assert not dup_res.ok
    assert any("duplicate" in e.cause for e in dup_res.errors)
    assert len(dup_res.directives) == 1


def test_prose_only_ac_not_enforced():
    # AC2: an AC with no directive yields zero directives and stays clean.
    spec = (
        "# Spec\n\n## Acceptance criteria\n\n"
        "- AC1: prose only, no directive\n"
        "- AC2: also just prose\n"
    )
    res = _contract.parse_spec(spec)
    assert res.ok
    assert res.directives == []


def test_directive_location_binding():
    # AC3: a well-formed block whose declared ac does not match its placement
    # (here declared AC1 but placed under AC2) is a parse error.
    spec = (
        "# Spec\n\n## Acceptance criteria\n\n"
        "- AC1: one\n"
        "- AC2: two\n"
        "  ```ac-directive\n"
        "  ac: AC1\n"
        "  kind: grep\n"
        "  command: grep foo bar\n"
        "  ```\n"
    )
    res = _contract.parse_spec(spec)
    assert not res.ok
    assert any("misplaced" in e.cause for e in res.errors)
    assert res.directives == []


# -- P3: execution engine (adversarial_common.contract.run_directive) ---------
#
# AC1 test_execution_semantics: the fixed execution contract for one parsed
# directive — cwd is repo_root, fixed documented env (no ambient leak, no
# operator rc), per-directive timeout fails instead of hanging, output is
# length-bounded with a marker, a signal-terminated child is a recorded
# interruption (never pass), no-diff compares only the named set and ignores
# untracked noise, and grep present/absent pass/fail is correct.


def _git_init_repo(path):
    """Init a throwaway git repo at *path* with one committed tracked file."""
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co",
    }
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env={**os.environ, **env})
    (path / "tracked.txt").write_text("foo line\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True, env={**os.environ, **env})


def _directive(kind, command, **kwargs):
    """Build a Directive bound to AC1 with sensible parser-equivalent defaults."""
    return _contract.Directive(ac="AC1", kind=kind, command=command.encode(), **kwargs)


def _assert_result_shape(result):
    assert set(result) == {
        "id", "status", "command", "timed_out", "message", "truncated_output",
        "infra", "denials",
    }
    assert result["status"] in ("pass", "fail")
    assert isinstance(result["timed_out"], bool)
    assert isinstance(result["infra"], bool)
    assert isinstance(result["denials"], tuple)
    assert result["id"].startswith("AC1:")


def _passthrough_contain(command, repo_root, _profile, timeout):
    """Containment provider that runs *command* for real via the bare engine.

    The execution-semantics suite's commands do not violate the review
    profile (no network, no out-of-scope write), so a provider that permits
    them and records no denials is the faithful double for a compliant
    directive under a real sandbox. The engine's own default backend would
    infra-fail here on a host without a usable sandbox, so the suite injects
    this seam to exercise execution semantics directly, independent of host
    sandboxing capability.
    """
    out = _contract._run_shell(command, repo_root, timeout)
    stdout, stderr, rc, timed_out, truncated, lines = out
    return _contract.ContainedRun(
        output=_contract._bound_output(stdout, stderr, truncated),
        stdout=stdout, rc=rc, timed_out=timed_out,
        stdout_lines=lines, denials=(),
    )


def test_execution_semantics(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)
    rr = str(repo)
    monkeypatch.setattr(_contract, "_contain", _passthrough_contain)

    # --- shell: writes a file under the fixed env, cwd is repo_root ----------
    out = run_directive(_directive("shell", "echo hello > out.txt"), rr)
    _assert_result_shape(out)
    assert out["status"] == "pass", out
    assert out["id"] == "AC1:shell"
    assert (repo / "out.txt").read_text().strip() == "hello"  # cwd was repo_root

    # fixed env: an ambient var never reaches the child (no leak), and PATH is
    # the engine's explicit constant, not the operator's.
    monkeypatch.setenv("AC_AMBIENT_LEAK", "secret")
    no_leak = run_directive(_directive("shell", 'test -z "$AC_AMBIENT_LEAK"'), rr)
    assert no_leak["status"] == "pass", no_leak
    fixed_path = run_directive(
        _directive("shell", 'test "$PATH" = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"'),
        rr,
    )
    assert fixed_path["status"] == "pass", fixed_path

    # --- timeout: fails fast (never pass, never hang) -----------------------
    start = time.monotonic()
    timed = run_directive(_directive("shell", "sleep 30", timeout=1), rr)
    elapsed = time.monotonic() - start
    assert timed["status"] == "fail", timed
    assert timed["timed_out"] is True
    assert "timeout" in timed["message"]
    assert elapsed < 3, f"timeout did not fire fast enough: {elapsed:.1f}s"

    # --- output past the limit is truncated with the shared marker ----------
    big = run_directive(
        _directive("shell", "awk 'BEGIN{for(i=0;i<20000;i++)printf \"x\"}'"), rr,
    )
    assert len(big["truncated_output"]) <= _contract.MAX_DIRECTIVE_OUTPUT
    assert gates.TRUNCATION_MARKER in big["truncated_output"]

    # --- shell exit-code contract: default 0, non-default expected matches --
    assert run_directive(_directive("shell", "exit 0"), rr)["status"] == "pass"
    assert run_directive(_directive("shell", "exit 1"), rr)["status"] == "fail"
    assert run_directive(_directive("shell", "exit 3", expected=3), rr)["status"] == "pass"
    assert run_directive(_directive("shell", "exit 4", expected=3), rr)["status"] == "fail"

    # --- SIGINT/SIGTERM: status fail, interruption recorded -----------------
    for sig_cmd, name in (("kill -TERM $$", "SIGTERM"), ("kill -INT $$", "SIGINT")):
        sig = run_directive(_directive("shell", sig_cmd), rr)
        assert sig["status"] == "fail", (name, sig)
        assert sig["timed_out"] is False
        assert "interrupted" in sig["message"], (name, sig)
        assert name in sig["message"], (name, sig)

    # --- grep: present passes, absent fails --------------------------------
    present = run_directive(_directive("grep", "grep foo tracked.txt"), rr)
    _assert_result_shape(present)
    assert present["status"] == "pass", present
    assert present["id"] == "AC1:grep"
    absent = run_directive(_directive("grep", "grep not-present tracked.txt"), rr)
    assert absent["status"] == "fail", absent
    assert absent["message"] == "absent"

    # --- no-diff: post-baseline mutation in the named set is flagged --------
    # (pre-existing untracked noise outside the named set is ignored)
    (repo / "noise.txt").write_text("pre-existing untracked noise\n")  # before run

    clean = run_directive(_directive("no-diff", "true"), rr)
    _assert_result_shape(clean)
    assert clean["status"] == "pass", clean          # noise ignored
    assert clean["id"] == "AC1:no-diff"

    mutated = run_directive(_directive("no-diff", "echo appended >> tracked.txt"), rr)
    assert mutated["status"] == "fail", mutated      # tracked mutation flagged
    assert "diff" in mutated["message"]
    assert "tracked.txt" in mutated["message"]
    # the untracked noise is still not part of the reported diff
    assert "noise.txt" not in mutated["message"]


def test_grep_named_file_set_scopes_the_search(tmp_path):
    """A2: directive.files scopes the grep, defaulting to all tracked files.

    A match that lives only in an excluded file must NOT satisfy a directive
    whose named set omits it; the default set (all tracked) does see it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)  # tracked.txt has a single 'foo line'
    (repo / "other.txt").write_text("uniquetoken\n")
    subprocess.run(["git", "add", "other.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "other"],
        cwd=repo, check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                   "GIT_AUTHOR_EMAIL": "t@t.co",
                                   "GIT_COMMITTER_NAME": "t",
                                   "GIT_COMMITTER_EMAIL": "t@t.co"},
    )
    rr = str(repo)

    # named set excludes the file holding the token -> absent (fail)
    scoped = run_directive(_directive("grep", "grep uniquetoken", files=("tracked.txt",)), rr)
    assert scoped["status"] == "fail", scoped
    assert scoped["message"] == "absent"

    # default set (all tracked) -> present (pass)
    default = run_directive(_directive("grep", "grep uniquetoken"), rr)
    assert default["status"] == "pass", default
    assert default["message"] == "present"


def test_grep_int_expected_reads_count_mode(tmp_path):
    """A5: an integer expectation is a match count.

    Default grep prints one line per match (count = match lines); ``grep -c``
    /``--count`` print one count per file, so the count is read off the output.
    Truncation never miscounts the default-mode tally.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)
    (repo / "two.txt").write_text("foo\nfoo\n")
    subprocess.run(["git", "add", "two.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "two"],
        cwd=repo, check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                   "GIT_AUTHOR_EMAIL": "t@t.co",
                                   "GIT_COMMITTER_NAME": "t",
                                   "GIT_COMMITTER_EMAIL": "t@t.co"},
    )
    rr = str(repo)
    d = _directive

    # default mode: 2 match lines
    assert run_directive(d("grep", "grep foo", expected=2, files=("two.txt",)), rr)["status"] == "pass"
    assert run_directive(d("grep", "grep foo", expected=1, files=("two.txt",)), rr)["status"] == "fail"

    # count mode: grep -c / --count print the count, not match lines
    assert run_directive(d("grep", "grep -c foo", expected=2, files=("two.txt",)), rr)["status"] == "pass"
    assert run_directive(d("grep", "grep --count foo", expected=2, files=("two.txt",)), rr)["status"] == "pass"
    assert run_directive(d("grep", "grep -c foo", expected=1, files=("two.txt",)), rr)["status"] == "fail"
    # short-flag group containing c (e.g. -cn) is also count mode
    assert run_directive(d("grep", "grep -cn foo", expected=2, files=("two.txt",)), rr)["status"] == "pass"

    # count reported on failure shows the value, not just 'present'
    miss = run_directive(d("grep", "grep -c foo", expected=9, files=("two.txt",)), rr)
    assert miss["status"] == "fail"
    assert miss["message"] == "count 2", miss


def test_grep_named_set_replaces_baked_in_file_operand(tmp_path):
    """A2 (round 3): the named set REPLACES file operands, it does not union them.

    Reproduces the disputed finding: ``grep TOKEN excluded.md`` with ``files``
    naming a different set must NOT search the excluded file baked into the
    command. The operand is dropped, so a token living only in that excluded
    file is absent (fail); the default set (all tracked) still sees it (pass).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)  # tracked.txt, no token
    (repo / "included.txt").write_text("nothing relevant here\n")
    (repo / "excluded.md").write_text("Literal hardware mis-priming\n")
    _env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co"}
    subprocess.run(["git", "add", "included.txt", "excluded.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "two"], cwd=repo, check=True, env=_env)
    rr = str(repo)

    # named set excludes the file the command bakes in -> token absent (fail)
    scoped = run_directive(
        _directive("grep", 'grep "Literal hardware mis-priming" excluded.md',
                   files=("included.txt",)),
        rr,
    )
    assert scoped["status"] == "fail", scoped
    assert scoped["message"] == "absent", scoped

    # default set (all tracked) searches excluded.md too -> present (pass)
    default = run_directive(
        _directive("grep", 'grep "Literal hardware mis-priming"'), rr,
    )
    assert default["status"] == "pass", default


def test_grep_count_no_double_count_with_baked_operand(tmp_path):
    """A5 (round 3): ``-c`` counts a baked-in file operand once, not twice.

    Reproduces the disputed finding: ``grep -c PATTERN file`` under the default
    named set (all tracked) must count *file* once. The previous append-only
    behavior searched it twice and reported ``count 2`` for one match.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)  # tracked.txt, no match
    (repo / "src.py").write_text("def _legal_expected():\n    pass\n")
    _env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co"}
    subprocess.run(["git", "add", "src.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "src"], cwd=repo, check=True, env=_env)
    rr = str(repo)

    ok = run_directive(
        _directive("grep", 'grep -c "def _legal_expected" src.py', expected=1), rr,
    )
    assert ok["status"] == "pass", ok
    assert ok["message"] == "count 1", ok  # counted once across all tracked

    bad = run_directive(
        _directive("grep", 'grep -c "def _legal_expected" src.py', expected=2), rr,
    )
    assert bad["status"] == "fail", bad
    assert bad["message"] == "count 1", bad  # not "count 2"


# -- P4: trust & containment model (adversarial_common.contract) ------------
#
# AC1 test_directive_containment: shell/no-diff execute under the P1
# constrained profile. With a profile available, a directive attempting an
# outbound connection and an out-of-scope write is observed denied, denial
# events recorded, and the effect confined to the worktree; with the runtime
# unable to provide a profile, the directive is NOT executed at ambient
# privilege -> infra failure (blocks APPROVE).

# Network program tokens / substrings marking a statement as an outbound
# connection attempt under the review profile (no network).
_NET_PROGRAMS = frozenset({
    "curl", "wget", "nc", "netcat", "ssh", "scp", "ftp", "telnet",
})
_NET_HINTS = (
    "/dev/tcp/", "socket.create_connection", "urllib", "requests.",
    "http://", "https://",
)
_REDIRECT_RE = re.compile(r"(?:>>|>)\s*(\S+)")


def _network_statement(stmt):
    """True if *stmt* (one shell statement) attempts an outbound connection."""
    s = stmt.strip()
    head = s.split(None, 1)[0] if s else ""
    return head in _NET_PROGRAMS or any(hint in s for hint in _NET_HINTS)


def _out_of_scope_write(stmt, repo_root, write_roots):
    """Path of an out-of-scope redirect target in *stmt*, or None.

    A redirect (``>``/``>>``) whose target resolves outside every write root
    (worktree + scratch) violates the profile's write confinement.
    """
    for m in _REDIRECT_RE.finditer(stmt):
        tok = m.group(1).strip("'\"")
        path = os.path.normpath(tok if os.path.isabs(tok) else os.path.join(repo_root, tok))
        if not any(path == r or path.startswith(r + os.sep) for r in write_roots):
            return path
    return None


def _policy_contain(command, repo_root, profile, timeout):
    """Reference policy containment provider (test double).

    Enforces the profile at the command-policy layer: each newline-separated
    statement is classified; a network operation or a write redirect to a path
    outside the profile's ``write_roots`` is DENIED (recorded, not executed);
    permitted statements run via the engine. This mirrors what the real kernel
    sandbox (bwrap ``--unshare-net`` + read-only mounts, strace-audited) would
    enforce, deterministically, on a host without usable user namespaces — so
    denial recording and worktree confinement stay testable everywhere.
    """
    write_roots = tuple(os.path.abspath(r) for r in profile.write_roots)
    denials = []
    permitted = []
    for stmt in command.decode("utf-8", "replace").split("\n"):
        s = stmt.strip()
        if not s:
            continue
        if _network_statement(s):
            denials.append({"event": "network_denied", "detail": s})
            continue
        oos = _out_of_scope_write(s, repo_root, write_roots)
        if oos:
            denials.append({"event": "write_denied", "path": oos, "detail": s})
            continue
        permitted.append(s)
    run_cmd = "\n".join(permitted).encode() if permitted else b"true"
    out = _contract._run_shell(run_cmd, repo_root, timeout)
    stdout, stderr, rc, timed_out, truncated, lines = out
    return _contract.ContainedRun(
        output=_contract._bound_output(stdout, stderr, truncated),
        stdout=stdout, rc=rc, timed_out=timed_out,
        stdout_lines=lines, denials=tuple(denials),
    )


def test_directive_containment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)
    rr = str(repo)
    oos_path = str(tmp_path / "oos_leak.txt")  # sibling of repo -> out of scope

    # --- profile available: containment enforces + records denials ----------
    monkeypatch.setattr(_contract, "_contain", _policy_contain)
    cmd = "\n".join([
        "echo wt-ok > inside.txt",         # worktree write -> permitted
        f"echo leak > {oos_path}",         # out-of-scope write -> denied
        "curl -sS https://example.com",    # outbound connection -> denied
    ])
    out = run_directive(_directive("shell", cmd), rr)
    _assert_result_shape(out)
    assert out["status"] == "fail", out            # denials taint the result
    assert out["infra"] is False, out              # a real denial, not infra
    assert len(out["denials"]) == 2, out["denials"]
    events = {d["event"] for d in out["denials"]}
    assert {"network_denied", "write_denied"} <= events, out["denials"]
    assert any(d.get("path") == oos_path for d in out["denials"]), out["denials"]
    # effect confined to the worktree: the permitted write landed, the
    # violations did not.
    assert (repo / "inside.txt").read_text().strip() == "wt-ok"
    assert not os.path.exists(oos_path)

    # --- profile unavailable: NOT executed at ambient -> infra failure -------
    # The default profile resolver always resolves the review role, so simulate
    # a runtime that cannot provide a constrained profile by returning None.
    monkeypatch.setattr(_contract, "_resolve_profile", lambda *_a, **_k: None)
    ambient_marker = repo / "ambient_marker.txt"
    assert not ambient_marker.exists()
    infra = run_directive(
        _directive("shell", "echo ran > ambient_marker.txt\ncurl https://x.io"), rr,
    )
    _assert_result_shape(infra)
    assert infra["status"] == "fail", infra
    assert infra["infra"] is True, infra           # blocks APPROVE (R2/F4)
    assert infra["denials"] == (), infra
    assert not ambient_marker.exists()   # never ran at ambient privilege


def test_directive_default_infra_fails_without_sandbox(tmp_path, monkeypatch):
    """R2/F4: with no containment backend, the engine default does not run a
    shell directive at ambient privilege — it fails as infrastructure.

    The engine default probes for a real sandbox backend (bubblewrap +
    strace with user namespaces). Where none is usable this host returns
    None, so a shell directive settles ``infra=True`` rather than pass/fail
    at ambient. We force the probe to 'unavailable' so the assertion holds on
    every host.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)
    rr = str(repo)
    monkeypatch.setattr(_contract, "_containment_backend", lambda: None)
    # leave _resolve_profile and _contain at their real defaults
    out = run_directive(_directive("shell", "echo hi"), rr)
    assert out["status"] == "fail", out
    assert out["infra"] is True, out
    assert "containment backend unavailable" in out["message"], out


def test_directive_unknown_kind_does_not_route_through_containment(tmp_path):
    """A directly-constructed Directive with an unrecognized kind (the parser
    never produces one) is rejected before profile resolution, not silently
    treated as shell."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)
    out = run_directive(_directive("frobnicate", "echo hi"), str(repo))
    assert out["status"] == "fail", out
    assert out["infra"] is False, out
    assert "unknown kind" in out["message"], out


def test_directive_no_diff_denial_short_circuits_diff_check(tmp_path, monkeypatch):
    """A denied no-diff directive fails on the denial, not a spurious diff."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)
    rr = str(repo)
    monkeypatch.setattr(_contract, "_contain", _policy_contain)
    out = run_directive(_directive("no-diff", "curl -sS https://example.com"), rr)
    assert out["status"] == "fail", out
    assert out["infra"] is False, out
    assert len(out["denials"]) == 1, out["denials"]
    assert "containment denied" in out["message"], out


# -- P4: syscall-level denial detection (not text-scraped from output) ------
#
# A2 (review finding on the prior attempt at this spec): detecting denials by
# searching the child's own stdout/stderr is both suppressible (the child can
# redirect its own error output, e.g. ``2>/dev/null``) and fabricate-able (a
# compliant command that merely prints unrelated text like "permission
# denied" gets falsely flagged). ``_parse_strace_denials`` instead classifies
# a strace syscall log the traced child cannot see or edit.


def test_parse_strace_denials_detects_network_and_write_denials():
    write_roots = ("/repo",)
    trace = b"\n".join([
        b'123 connect(3, {sa_family=AF_INET, sin_port=htons(443), '
        b'sin_addr=inet_addr("1.2.3.4")}, 16) = -1 ENETUNREACH (Network is unreachable)',
        b'123 openat(AT_FDCWD, "/etc/escaped", O_WRONLY|O_CREAT|O_TRUNC, 0666)'
        b' = -1 EROFS (Read-only file system)',
        b'123 openat(AT_FDCWD, "/repo/ok.txt", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3',
        b'123 openat(AT_FDCWD, "/repo/missing.txt", O_RDONLY) = -1 ENOENT'
        b' (No such file or directory)',
    ])
    events = _contract._parse_strace_denials(trace, write_roots, "/repo")
    assert any(e["event"] == "network_denied" for e in events)
    assert any(
        e["event"] == "write_denied" and e["path"] == "/etc/escaped" for e in events
    )
    # a successful write inside the write root, and a read-only-mode failure
    # (ENOENT on O_RDONLY, not a write attempt) must NOT be recorded.
    assert len(events) == 2, events


def test_parse_strace_denials_not_fooled_by_suppressed_or_fabricated_text():
    # The child redirecting its own stderr to /dev/null cannot hide a denial
    # from the syscall-level detector (unlike scraping captured output).
    write_roots = ("/repo",)
    suppressed_trace = (
        b'99 openat(AT_FDCWD, "/etc/escaped", O_WRONLY|O_CREAT, 0644)'
        b' = -1 EACCES (Permission denied)\n'
    )
    events = _contract._parse_strace_denials(suppressed_trace, write_roots, "/repo")
    assert events == [{"event": "write_denied", "path": "/etc/escaped", "errno": "EACCES"}]

    # a compliant command that merely PRINTS "permission denied" as ordinary
    # program output never reaches the syscall log at all, so it is never
    # misclassified as a denial (there is nothing here to even parse).
    benign_trace = b""
    assert _contract._parse_strace_denials(benign_trace, write_roots, "/repo") == []


def test_parse_strace_denials_resolves_relative_path_against_cwd():
    write_roots = ("/repo",)
    trace = (
        b'1 openat(AT_FDCWD, "../../escape.txt", O_WRONLY|O_CREAT, 0644)'
        b' = -1 EROFS (Read-only file system)\n'
    )
    events = _contract._parse_strace_denials(trace, write_roots, "/repo/sub")
    assert events == [{"event": "write_denied", "path": "/escape.txt", "errno": "EROFS"}]


@pytest.mark.skipif(
    _contract._containment_backend() != "bwrap",
    reason="no usable bwrap+strace sandbox backend on this host",
)
def test_directive_containment_real_backend(tmp_path):
    """Integration check for the real bwrap+strace backend, where available.

    Skipped on hosts (this dev sandbox included — unprivileged user
    namespaces are blocked here) that cannot enter a real bwrap sandbox.
    AC1's containment semantics are fully covered by
    test_directive_containment above via an injected policy double; this
    additionally proves the real kernel-enforced backend where the host
    allows it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_repo(repo)
    rr = str(repo)
    oos_path = str(tmp_path / "real_oos_leak.txt")

    cmd = "\n".join([
        "echo wt-ok > inside.txt",
        f"echo leak > {oos_path}",
        "curl -sS --max-time 2 http://example.com || true",
    ])
    out = run_directive(_directive("shell", cmd, timeout=15), rr)
    assert out["infra"] is False, out
    assert (repo / "inside.txt").read_text().strip() == "wt-ok"
    assert not os.path.exists(oos_path)

