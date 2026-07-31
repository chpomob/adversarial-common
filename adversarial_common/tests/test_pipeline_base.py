"""Unit tests for the shared pipeline lifecycle surface."""

from __future__ import annotations

import argparse
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from adversarial_common import pipeline_base as pb


class FakeLedger:
    def summary(self):
        return {"total": {"est_cost_usd": 1.25}}


class FakeGit:
    def __init__(self, *, repo=True, branch="main"):
        self.repo = repo
        self.branch = branch
        self.calls = []
        self.stash = "stash-sha"
        self.fail = set()

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    def detect_enclosing_repo(self, workdir):
        self._call("detect", workdir)
        return "/repo" if self.repo else None

    def ensure_git_identity(self, workdir):
        self._call("identity", workdir)

    def get_current_branch(self, workdir):
        self._call("current", workdir)
        return self.branch

    def auto_init(self, workdir):
        self._call("init", workdir)
        self.repo = True
        self.branch = "main"

    def stash_dirty(self, workdir):
        self._call("stash", workdir)
        return self.stash

    def create_loop_branch(self, workdir, feature, parent, prefix="loop"):
        self._call("create", workdir, feature, parent, prefix)
        return f"{prefix}/{feature}/1"

    def checkout(self, workdir, branch):
        self._call("checkout", workdir, branch)
        self.branch = branch

    def record_branch_point(self, workdir, parent):
        self._call("branch_point", workdir, parent)
        return "abc123"

    def ensure_gitignore(self, workdir, entry):
        self._call("gitignore", workdir, entry)

    def unstash(self, workdir, stash_id):
        self._call("unstash", workdir, stash_id)

    def squash_merge(self, workdir, branch, parent, message):
        self._call("squash", workdir, branch, parent, message)

    def reject_marker(self, workdir, message):
        self._call("reject", workdir, message)


def test_banner_shape_and_ci_suppression():
    stream = io.StringIO()
    pb.banner("BUILD", stream=stream)
    assert stream.getvalue() == f"\n{'=' * 60}\n  BUILD\n{'=' * 60}\n"
    pb.banner("HIDDEN", ci=True, stream=stream)
    assert "HIDDEN" not in stream.getvalue()


def test_write_json_is_pretty_atomic_and_creates_parents(tmp_path):
    path = pb.write_json(tmp_path, "nested/state.json", {"a": [1]})
    assert path == tmp_path / "nested/state.json"
    assert path.read_text() == '{\n  "a": [\n    1\n  ]\n}\n'
    assert not list(path.parent.glob(".state.json.*.tmp"))


def test_write_json_serialization_error_preserves_existing_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("old")
    with pytest.raises(TypeError):
        pb.write_json(tmp_path, "state.json", {"bad": object()})
    assert path.read_text() == "old"


def test_write_json_replace_error_preserves_existing_and_cleans_temp(
    tmp_path, monkeypatch,
):
    path = tmp_path / "state.json"
    path.write_text("old")

    def fail_replace(source, destination):
        raise OSError("replace denied")

    monkeypatch.setattr(pb.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace denied"):
        pb.write_json(tmp_path, "state.json", {"new": True})
    assert path.read_text() == "old"
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_ensure_finding_ids_handles_blanks_and_duplicate_chains():
    findings = [
        {}, {"id": "  "}, {"id": "X"}, {"id": "X"}, {"id": "X-4"},
    ]
    assert pb.ensure_finding_ids(findings) is findings
    assert [item["id"] for item in findings] == [
        "finding-1", "finding-2", "X", "X-4", "X-4-5",
    ]


def test_ensure_finding_ids_rejects_non_mapping():
    with pytest.raises(TypeError, match="finding 1"):
        pb.ensure_finding_ids([None])


def test_settled_and_unresolved_contracts():
    findings = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
    results = [
        {"id": "A", "status": "resolved"},
        {"id": "B", "status": "open"},
        {"id": None, "status": "resolved"},
    ]
    assert pb.SETTLED_STATUSES == frozenset({"resolved", "rejected"})
    assert pb.is_settled_status("rejected")
    assert pb.unresolved_findings(findings, results) == findings[1:]
    assert pb.unresolved_findings(
        findings, [{"id": "B", "status": "waived"}],
        settled_statuses=("waived",),
    ) == [findings[0], findings[2]]


def test_threshold_overrides_precedence_and_environment_order():
    args = SimpleNamespace(min_chars=7, min_tokens=None, min_lines=None)
    result = pb.threshold_overrides(
        args,
        {
            "min_chars": ("LOCAL_CHARS", "SHARED_CHARS"),
            "min_tokens": ("LOCAL_TOKENS", "SHARED_TOKENS"),
            "min_lines": ("MISSING",),
        },
        environ={
            "LOCAL_CHARS": "99", "SHARED_CHARS": "100",
            "LOCAL_TOKENS": "8", "SHARED_TOKENS": "9",
        },
    )
    assert result == {"min_chars": 7, "min_tokens": 8}


@pytest.mark.parametrize("value", ["bad", "-1", True])
def test_threshold_overrides_uses_consumer_error(value):
    class ConsumerError(Exception):
        pass

    with pytest.raises(ConsumerError, match="non-negative integer"):
        pb.threshold_overrides(
            SimpleNamespace(limit=None), {"limit": ("LIMIT",)},
            environ={"LIMIT": value}, error_type=ConsumerError,
        )


def _preflight_policy(captured, *, context_ok=True, enforce_input_cap=True):
    def check(kind, text, overrides):
        captured["check"] = (kind, text, overrides)
        return {
            "ok": context_ok,
            "reason": "below_min_chars" if not context_ok else "ok",
            "thresholds": overrides,
        }

    def complexity(text, max_agents):
        captured["complexity"] = (text, max_agents)
        return {"level": "low"}

    def writer(out_dir, verdict, payload):
        captured["blocked"] = (Path(out_dir), verdict, dict(payload))

    return pb.PreflightPolicy(
        context_kind=lambda args, text: args.kind,
        threshold_env={"min_chars": ("MIN_CHARS",)},
        environ={"MIN_CHARS": "2"},
        enforce_input_cap=enforce_input_cap,
        check_context=check,
        estimate_complexity=complexity,
        enforce_cap=lambda text, limit: (text[:limit], len(text) > limit),
        execution_settings=lambda args: {"custom": True},
        blocked_writer=writer,
        blocked_extra=lambda args, result: {"consumer": args.kind},
        reporter=lambda message: captured.setdefault("reports", []).append(message),
    )


def test_preflight_passes_and_attaches_metadata(tmp_path):
    captured = {}
    args = SimpleNamespace(
        kind="brief", min_chars=None, max_input_chars=10,
        truncate_input=False, max_agents=4,
    )
    result = pb.preflight(args, "short", tmp_path, policy=_preflight_policy(captured))
    assert result.ok is True
    assert result.effective_text == "short"
    assert captured["check"] == ("brief", "short", {"min_chars": 2})
    assert args._context is result.context
    assert args._complexity is result.complexity
    assert args._preflight_cap_events == []
    assert "blocked" not in captured


def test_preflight_truncates_when_enabled(tmp_path):
    captured = {}
    args = SimpleNamespace(
        kind="input", min_chars=None, max_input_chars=4,
        truncate_input=True, max_agents=2,
    )
    result = pb.preflight(args, "abcdef", tmp_path, policy=_preflight_policy(captured))
    assert result.ok
    assert result.effective_text == "abcd"
    assert result.cap_events == [{
        "kind": "input", "phase": "preflight", "limit": 4,
        "original_chars": 6, "truncated": True,
    }]


def test_preflight_blocks_unaccepted_cap_and_writes_consumer_payload(tmp_path):
    captured = {}
    args = SimpleNamespace(
        kind="spec", min_chars=None, max_input_chars=4,
        truncate_input=False, max_agents=2,
    )
    result = pb.preflight(args, "abcdef", tmp_path, policy=_preflight_policy(captured))
    assert not result.ok
    assert result.context["reason"] == "input_exceeds_max_chars"
    out_dir, verdict, payload = captured["blocked"]
    assert out_dir == tmp_path
    assert verdict == "CONTEXT_BLOCKED"
    assert payload["consumer"] == "spec"
    assert payload["execution"] == {"custom": True}
    assert payload["costs"]["records"] == []
    assert captured["reports"] == ["X context blocked: input_exceeds_max_chars"]


def test_preflight_context_block_and_no_cap_policy(tmp_path):
    captured = {}
    args = SimpleNamespace(
        kind="diff", min_chars=None, max_input_chars=1,
        truncate_input=False, max_agents=1,
    )
    result = pb.preflight(
        args, "abcdef", tmp_path,
        policy=_preflight_policy(
            captured, context_ok=False, enforce_input_cap=False,
        ),
    )
    assert not result.ok
    assert result.effective_text == "abcdef"
    assert result.cap_events == []
    assert captured["blocked"][2]["reason"] == "below_min_chars"


def test_preflight_propagates_blocked_writer_error(tmp_path):
    policy = pb.PreflightPolicy(
        check_context=lambda *args: {"ok": False, "reason": "no", "thresholds": {}},
        estimate_complexity=lambda *args, **kwargs: {},
        blocked_writer=lambda *args: (_ for _ in ()).throw(OSError("disk full")),
    )
    args = SimpleNamespace(max_input_chars=10, truncate_input=False, max_agents=1)
    with pytest.raises(OSError, match="disk full"):
        pb.preflight(args, "x", tmp_path, policy=policy)


def test_preflight_default_policy_writes_complete_blocked_artifact(tmp_path):
    args = SimpleNamespace(
        max_input_chars=None, max_output_chars=100, truncate_input=False,
        max_retries=1, max_agents=2, show_costs=False,
    )
    result = pb.preflight(args, "x", tmp_path)
    assert not result.ok
    payload = json.loads((tmp_path / "final.json").read_text())
    assert payload["verdict"] == "CONTEXT_BLOCKED"
    assert payload["context_blocked"] is True
    assert payload["attempts"] == []


def test_record_phase_aggregates_and_deduplicates():
    state = {"warnings": [{"code": "old"}]}
    result = {
        "exit_code": 0,
        "execution": {
            "attempts": [{"number": 1}, "bad"],
            "cap_events": [{"kind": "output"}],
        },
        "provider_history": [{"provider": "p"}, "bad"],
        "warnings": [{"code": "old"}, {"code": "new"}],
        "epistemic_distribution": {"confidence": {"high": 1}},
    }
    call = pb.record_phase(
        state, "review", result, FakeLedger(), include_epistemic=True,
    )
    assert call["ok"] is True
    assert state["attempts"] == [{"phase": "review", "number": 1}]
    assert state["cap_events"] == [{"phase": "review", "kind": "output"}]
    assert state["provider_history"] == [{"provider": "p"}]
    assert state["warnings"] == [{"code": "old"}, {"code": "new"}]
    assert state["epistemic_labels"]["confidence"]["high"] == 1
    assert state["costs"]["total"]["est_cost_usd"] == 1.25


def test_record_phase_handles_malformed_result_and_epistemic_opt_out():
    state = {}
    call = pb.record_phase(state, "bad", None, FakeLedger())
    assert call == {
        "label": "bad", "ok": False, "attempts": [], "cap_events": [],
    }
    assert "epistemic_labels" not in state


def test_setup_git_existing_repo_updates_recovery_state():
    fake = FakeGit()
    state = {}
    result = pb.setup_git(
        "/safe", "feature", state,
        policy=pb.GitSetupPolicy(
            prefix="spec", gitignore_entry=".adversarial-spec/", git_adapter=fake,
        ),
    )
    assert result == {
        "exit_code": 0, "parent_branch": "main", "branch": "spec/feature/1",
        "branch_point": "abc123", "stash_id": "stash-sha",
    }
    assert state == {
        "stash_id": "stash-sha", "parent_branch": "main",
        "branch": "spec/feature/1", "branch_point": "abc123",
    }
    assert ("identity", "/safe") in fake.calls
    assert ("gitignore", "/safe", ".adversarial-spec/") in fake.calls


def test_setup_git_initializes_non_repo_and_maps_errors():
    fake = FakeGit(repo=False)
    result = pb.setup_git("/safe", "f", policy=pb.GitSetupPolicy(git_adapter=fake))
    assert result["parent_branch"] == "main"
    assert ("init", "/safe") in fake.calls
    fake = FakeGit()
    fake.fail.add("stash")
    state = {}
    result = pb.setup_git("/safe", "f", state, policy=pb.GitSetupPolicy(git_adapter=fake))
    assert result["exit_code"] == 1
    assert result["error"] == "stash failed"
    assert state["stash_id"] == ""


def test_setup_git_callback_is_policy_owned_and_syncs_state():
    state = {}
    seen = {}

    def callback(context):
        seen.update(context)
        return {
            "exit_code": 0, "parent_branch": "trunk", "branch": "custom",
            "branch_point": "point", "stash_id": "s",
        }

    result = pb.setup_git(
        "/safe", "f", state,
        policy=pb.GitSetupPolicy(setup_callback=callback),
    )
    assert result["branch"] == "custom"
    assert state["parent_branch"] == "trunk"
    assert seen["state"] is state


def test_setup_git_callback_error_is_mapped():
    result = pb.setup_git(
        "/safe", "f",
        policy=pb.GitSetupPolicy(
            setup_callback=lambda context: (_ for _ in ()).throw(OSError("callback")),
        ),
    )
    assert result == {"exit_code": 1, "error": "callback"}


def test_restore_git_checks_out_then_unstashes_and_atomically_persists(tmp_path):
    fake = FakeGit(branch="loop/f/1")
    state = {"parent_branch": "main", "stash_id": "stash-sha", "other": 1}
    result = pb.restore_git(
        "/safe", state, tmp_path,
        policy=pb.RestorePolicy(git_adapter=fake),
    )
    assert result == pb.RestoreResult(True, True, True)
    assert state["stash_id"] == ""
    assert json.loads((tmp_path / "state.json").read_text())["stash_id"] == ""
    assert [call[0] for call in fake.calls].index("checkout") < [
        call[0] for call in fake.calls
    ].index("unstash")


def test_restore_git_never_unstashes_after_checkout_failure():
    fake = FakeGit(branch="loop/f/1")
    fake.fail.add("checkout")
    reports = []
    state = {"parent_branch": "main", "stash_id": "stash-sha"}
    result = pb.restore_git(
        "/safe", state,
        policy=pb.RestorePolicy(git_adapter=fake, reporter=reports.append),
    )
    assert not result.ok
    assert not result.branch_restored
    assert not any(call[0] == "unstash" for call in fake.calls)
    assert "git stash pop stash-sha" in reports[-1]


def test_restore_git_pop_and_persistence_errors_are_structured(tmp_path):
    fake = FakeGit()
    fake.fail.add("unstash")
    state = {"parent_branch": "main", "stash_id": "s"}
    result = pb.restore_git(
        "/safe", state, tmp_path,
        policy=pb.RestorePolicy(git_adapter=fake, reporter=lambda message: None),
    )
    assert result.branch_restored and not result.stash_restored
    assert state["stash_id"] == "s"

    fake.fail.clear()
    result = pb.restore_git(
        "/safe", state, tmp_path,
        policy=pb.RestorePolicy(
            git_adapter=fake,
            state_writer=lambda *args: (_ for _ in ()).throw(OSError("disk")),
            reporter=lambda message: None,
        ),
    )
    assert result.stash_restored and not result.state_persisted and not result.ok
    assert state["stash_id"] == ""


def test_restore_git_cleanup_error_does_not_mask_restore():
    fake = FakeGit()
    result = pb.restore_git(
        "/safe", {"parent_branch": "main", "stash_id": ""},
        policy=pb.RestorePolicy(
            git_adapter=fake,
            pre_restore=lambda context: (_ for _ in ()).throw(RuntimeError("cleanup")),
            reporter=lambda message: None,
        ),
    )
    assert result.ok
    assert result.cleanup_error == "cleanup"


def test_restore_git_no_stash_needs_no_state_write():
    fake = FakeGit()
    result = pb.restore_git(
        "/safe", {"parent_branch": "main", "stash_id": ""}, "/unused",
        policy=pb.RestorePolicy(
            git_adapter=fake,
            state_writer=lambda *args: (_ for _ in ()).throw(AssertionError()),
        ),
    )
    assert result == pb.RestoreResult(True, True, False)


def test_log_retrospective_uses_utc_and_bounded_stdout(tmp_path):
    policy = pb.RetrospectivePolicy(
        stdout_chars=4,
        clock=lambda: datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
    )
    path = pb.log_retrospective(
        "build", {"error": "boom", "stdout": "abcdef"},
        "feat", "loop/feat/1", tmp_path, policy=policy,
    )
    text = path.read_text()
    assert "### 2026-07-31 — build failed for feat" in text
    assert "**Branch:** loop/feat/1" in text
    assert "**Error:** boom" in text
    assert "last 4 chars):** 'cdef'" in text


def test_log_retrospective_handles_non_mapping_result(tmp_path):
    path = pb.log_retrospective(
        "build", None, "feat", "loop/feat/1", tmp_path,
    )
    text = path.read_text()
    assert "**Error:** unknown error" in text
    assert "last 200 chars):** ''" in text


def test_phase_failure_logs_uniformly_and_records_resumable_state(tmp_path):
    reports = []
    recorded = []
    state = {"feature": "feat", "branch": "b"}
    code = pb.phase_failure(
        "verify", {"error": "bad"}, state, tmp_path,
        policy=pb.RetrospectivePolicy(
            infrastructure_exit=9, record_state_failure=True,
            state_recorder=lambda state, out, phase: recorded.append(phase),
            reporter=reports.append,
        ),
    )
    assert code == 9
    assert state["error"] == "verify: bad"
    assert recorded == ["verify_failed"]
    assert (tmp_path / "ISSUES.md").is_file()
    assert reports[0] == "X verify failed: bad"


def test_phase_failure_handles_non_mapping_result(tmp_path):
    reports = []
    code = pb.phase_failure(
        "verify", None, {"feature": "feat", "branch": "b"}, tmp_path,
        policy=pb.RetrospectivePolicy(infrastructure_exit=9, reporter=reports.append),
    )
    assert code == 9
    assert reports == ["X verify failed: unknown error"]
    assert "**Error:** unknown error" in (tmp_path / "ISSUES.md").read_text()


def test_phase_failure_logging_and_state_errors_are_best_effort(tmp_path):
    reports = []
    bad_out = tmp_path / "not-a-directory"
    bad_out.write_text("x")
    code = pb.phase_failure(
        "build", {"error": "bad"}, {}, bad_out,
        policy=pb.RetrospectivePolicy(
            record_state_failure=True,
            state_recorder=lambda *args: (_ for _ in ()).throw(OSError("state")),
            reporter=reports.append,
        ),
    )
    assert code == 1
    assert any("failure state" in message for message in reports)
    assert any("retrospective" in message for message in reports)


def _finish_args(**kwargs):
    return SimpleNamespace(
        no_merge=kwargs.get("no_merge", False),
        ci=kwargs.get("ci", False),
        fail_on=kwargs.get("fail_on", None),
    )


def test_finish_pipeline_default_approved_and_rejected_paths(tmp_path):
    fake = FakeGit()
    state = {"branch": "spec/f/1", "parent_branch": "main"}
    policy = pb.FinishPolicy(
        pipeline_name="Adversarial Spec", approval_label="spec",
        loop_label="Revise/verify loops", git_adapter=fake,
    )
    code = pb.finish_pipeline(
        _finish_args(), "/safe", "f", tmp_path, state, "APPROVED",
        loops=2, policy=policy,
    )
    assert code == 0
    assert any(call[0] == "squash" for call in fake.calls)
    final = json.loads((tmp_path / "final.json").read_text())
    assert final["verdict"] == "APPROVED"
    assert final["merged"] is True
    assert "Revise/verify loops: 2" in (tmp_path / "final.md").read_text()

    other = tmp_path / "reject"
    code = pb.finish_pipeline(
        _finish_args(), "/safe", "f", other, state, "REJECT",
        reason="open", policy=policy,
    )
    assert code == 3
    assert any(call[0] == "reject" for call in fake.calls)


def test_finish_pipeline_no_merge_and_custom_callbacks(tmp_path):
    fake = FakeGit()
    events = []
    state = {"branch": "b", "parent_branch": "main"}

    def finalize(context):
        events.append("finalize")
        return {"exit_code": 0, "merged": False, "custom": True}

    policy = pb.FinishPolicy(
        approved_verdicts=frozenset({"APPROVED", "ARBITRATED"}),
        exit_by_verdict={"APPROVED": 0, "ARBITRATED": 4},
        git_adapter=fake,
        finalize_callback=finalize,
        payload_builder=lambda context: {"consumer": "loop"},
        post_write=lambda context: events.append(context["final_payload"]["consumer"]),
        state_complete=lambda context: events.append("complete"),
    )
    code = pb.finish_pipeline(
        _finish_args(no_merge=True), "/safe", "f", tmp_path, state,
        "ARBITRATED", conditions=["document"], policy=policy,
    )
    assert code == 4
    assert events == ["finalize", "loop", "complete"]
    assert not any(call[0] == "squash" for call in fake.calls)
    assert json.loads((tmp_path / "final.json").read_text())["consumer"] == "loop"


def test_finish_pipeline_default_no_merge_skips_squash(tmp_path):
    fake = FakeGit()
    code = pb.finish_pipeline(
        _finish_args(no_merge=True), "/safe", "f", tmp_path,
        {"branch": "b", "parent_branch": "main"}, "APPROVED",
        policy=pb.FinishPolicy(git_adapter=fake),
    )
    assert code == 0
    assert not any(call[0] == "squash" for call in fake.calls)


def test_finish_pipeline_finalize_and_callback_errors_map_to_infrastructure(tmp_path):
    reports = []
    policy = pb.FinishPolicy(
        finalize_callback=lambda context: {"exit_code": 1, "error": "conflict"},
        reporter=reports.append,
    )
    code = pb.finish_pipeline(
        _finish_args(), "/safe", "f", tmp_path,
        {"branch": "b", "parent_branch": "main"}, "APPROVED", policy=policy,
    )
    assert code == 1
    final = json.loads((tmp_path / "final.json").read_text())
    assert final["infrastructure"] is True
    assert "conflict" in final["error"]
    assert any("git finalize failed" in message for message in reports)

    bad = pb.FinishPolicy(
        finalize_callback=lambda context: {"exit_code": 0},
        payload_builder=lambda context: (_ for _ in ()).throw(ValueError("payload")),
        reporter=lambda message: None,
    )
    assert pb.finish_pipeline(
        _finish_args(), "/safe", "f", tmp_path / "bad",
        {"branch": "b", "parent_branch": "main"}, "APPROVED", policy=bad,
    ) == 1


def test_finish_pipeline_post_write_fatal_and_state_completion_errors(tmp_path):
    base = dict(finalize_callback=lambda context: {"exit_code": 0})
    fatal = pb.FinishPolicy(
        **base,
        post_write=lambda context: (_ for _ in ()).throw(OSError("html")),
        post_write_fatal=True, reporter=lambda message: None,
    )
    state = {"branch": "b", "parent_branch": "main"}
    assert pb.finish_pipeline(
        _finish_args(), "/safe", "f", tmp_path / "post", state,
        "APPROVED", policy=fatal,
    ) == 1

    completion = pb.FinishPolicy(
        **base,
        state_complete=lambda context: (_ for _ in ()).throw(OSError("state")),
        reporter=lambda message: None,
    )
    assert pb.finish_pipeline(
        _finish_args(), "/safe", "f", tmp_path / "state", state,
        "APPROVED", policy=completion,
    ) == 1


def test_finish_pipeline_artifact_callback_errors_map_to_infrastructure(tmp_path):
    state = {"branch": "b", "parent_branch": "main"}
    bad_md = pb.FinishPolicy(
        final_md_builder=lambda context: (_ for _ in ()).throw(OSError("markdown")),
        reporter=lambda message: None,
    )
    assert pb.finish_pipeline(
        _finish_args(), "/safe", "f", tmp_path / "md", state,
        "APPROVED", policy=bad_md,
    ) == 1

    bad_writer = pb.FinishPolicy(
        finalize_callback=lambda context: {"exit_code": 0},
        final_writer=lambda *args: (_ for _ in ()).throw(OSError("json")),
        reporter=lambda message: None,
    )
    assert pb.finish_pipeline(
        _finish_args(), "/safe", "f", tmp_path / "json", state,
        "APPROVED", policy=bad_writer,
    ) == 1


def test_finish_pipeline_nonfatal_post_write_does_not_change_exit(tmp_path):
    policy = pb.FinishPolicy(
        finalize_callback=lambda context: {"exit_code": 0},
        post_write=lambda context: (_ for _ in ()).throw(OSError("html")),
        reporter=lambda message: None,
    )
    assert pb.finish_pipeline(
        _finish_args(), "/safe", "f", tmp_path,
        {"branch": "b", "parent_branch": "main"}, "APPROVED", policy=policy,
    ) == 0


def test_finish_pipeline_uses_ci_callback(tmp_path):
    seen = []
    policy = pb.FinishPolicy(
        finalize_callback=lambda context: {"exit_code": 0},
        ci_exit_mapper=lambda context: seen.append(context["verdict"]) or 17,
    )
    code = pb.finish_pipeline(
        _finish_args(ci=True), "/safe", "f", tmp_path,
        {"branch": "b", "parent_branch": "main"}, "APPROVED", policy=policy,
    )
    assert code == 17
    assert seen == ["APPROVED"]


def test_finish_pipeline_ci_callback_error_maps_to_infrastructure(tmp_path):
    policy = pb.FinishPolicy(
        finalize_callback=lambda context: {"exit_code": 0},
        ci_exit_mapper=lambda context: (_ for _ in ()).throw(ValueError("ci")),
        reporter=lambda message: None,
    )
    assert pb.finish_pipeline(
        _finish_args(ci=True), "/safe", "f", tmp_path,
        {"branch": "b", "parent_branch": "main"}, "APPROVED", policy=policy,
    ) == 1


def test_ci_exit_from_final_prefers_recorded_integer(tmp_path):
    pb.write_json(tmp_path, "final.json", {
        "verdict": "REJECT", "ci": {"exit_code": 42},
    })
    assert pb.ci_exit_from_final(tmp_path, 3) == 42


def test_ci_exit_from_final_rejects_bool_and_passes_finding_schema(tmp_path):
    pb.write_json(tmp_path, "final.json", {
        "verdict": "WARN", "ci": {"exit_code": True},
        "finding_details": [{"id": "A"}], "infrastructure": True,
    })
    seen = {}

    def mapper(verdict, **kwargs):
        seen.update(verdict=verdict, **kwargs)
        return 8

    assert pb.ci_exit_from_final(
        tmp_path, 9, fail_on_selector="findings", mapper=mapper,
    ) == 8
    assert seen["findings"] == [{"id": "A"}]
    assert seen["infrastructure"] is True


def test_ci_exit_from_final_error_and_mapper_fallback(tmp_path):
    errors = []
    assert pb.ci_exit_from_final(tmp_path, 7, error_writer=errors.append) == 1
    assert isinstance(errors[0], OSError)
    pb.write_json(tmp_path, "final.json", {"verdict": "CUSTOM"})
    assert pb.ci_exit_from_final(
        tmp_path, 7,
        mapper=lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("policy")),
    ) == 7


def test_ci_exit_from_final_error_writer_is_best_effort(tmp_path):
    assert pb.ci_exit_from_final(
        tmp_path, 7,
        error_writer=lambda error: (_ for _ in ()).throw(OSError("writer")),
    ) == 1


@pytest.mark.parametrize("value,expected", [("1", 1), (3, 3), ("+4", 4)])
def test_positive_int_accepts_positive_values(value, expected):
    assert pb.positive_int(value) == expected


@pytest.mark.parametrize("value,expected", [("0", 0), (3, 3), ("+4", 4)])
def test_non_negative_int_accepts_domain(value, expected):
    assert pb.non_negative_int(value) == expected


@pytest.mark.parametrize("function,value", [
    (pb.positive_int, 0), (pb.positive_int, -1),
    (pb.non_negative_int, -1), (pb.non_negative_int, "bad"),
    (pb.positive_int, True),
])
def test_integer_validators_raise_argparse_errors(function, value):
    with pytest.raises(argparse.ArgumentTypeError):
        function(value)
