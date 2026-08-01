"""Unit tests for stdlib-only context, size, and complexity primitives."""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import adversarial_common.gates as gates
from adversarial_common import (
    check_context,
    enforce_input_cap,
    enforce_output_cap,
    estimate_complexity,
    post_build_gate,
    post_fix_gate,
    pre_build_gate,
)
from adversarial_common.gates import TRUNCATION_MARKER
from adversarial_common.providers import (
    _UNTRUSTED_BODY_BEGIN,
    _UNTRUSTED_BODY_END,
    _delimit_untrusted_body,
)


def test_empty_input_is_blocked_with_named_reason():
    result = check_context("brief", " \n\t", {"min_chars": 0, "min_tokens": 0})

    assert result["ok"] is False
    assert result["reason"] == "empty_input"
    assert result["thresholds"]["min_chars"] == 0


def test_input_below_character_floor_is_blocked():
    result = check_context(
        "input",
        "short",
        {"min_chars": 10, "min_tokens": 0},
    )

    assert result["ok"] is False
    assert result["reason"] == "below_min_chars"


def test_spec_requires_requirements_section():
    text = "# Overview\n" + "A detailed design without the required heading. " * 4

    result = check_context("spec", text)

    assert result["ok"] is False
    assert result["reason"] == "missing_required_section:Requirements"
    assert result["thresholds"]["required_sections"] == ["Requirements"]


def test_spec_with_requirements_section_is_accepted():
    text = (
        "# Overview\n"
        "A detailed design with enough context for the pipeline gate.\n\n"
        "## Requirements\n"
        "- R1: The implementation preserves the documented public contract.\n"
        "- R2: Verification covers the expected behavior in detail.\n"
    )

    result = check_context("spec", text)

    assert result["ok"] is True
    assert result["reason"] == "ok"


def test_diff_with_no_source_lines_is_blocked():
    result = check_context("diff", "diff --git a/a.py b/a.py\n")

    assert result["ok"] is False
    assert result["reason"] == "below_min_source_lines"


@pytest.mark.parametrize("cap", [enforce_input_cap, enforce_output_cap])
def test_cap_truncates_with_marker_and_honors_hard_limit(cap):
    text = "abcdefghijklmnopqrstuvwxyz"
    limited, was_truncated = cap(text, 20)

    assert was_truncated is True
    assert limited.endswith(TRUNCATION_MARKER)
    assert limited.startswith(text[: 20 - len(TRUNCATION_MARKER)])
    assert len(limited) == 20


@pytest.mark.parametrize("cap", [enforce_input_cap, enforce_output_cap])
def test_cap_leaves_text_at_limit_unchanged(cap):
    assert cap("exact", 5) == ("exact", False)


def test_zero_cap_is_safe_and_reported():
    assert enforce_input_cap("content", 0) == ("", True)


@pytest.mark.parametrize("body_len, cap", [(60, 60), (100, 60)])
def test_enforce_input_cap_then_delimit_keeps_closing_fence(body_len, cap):
    # Cap is applied to the raw body BEFORE delimiting, so the closing fence
    # added by delimiting always survives as the last bytes regardless of cap.
    capped, _ = enforce_input_cap("x" * body_len, cap)
    delimited = _delimit_untrusted_body(capped)
    assert delimited.startswith(_UNTRUSTED_BODY_BEGIN)
    assert delimited.endswith(_UNTRUSTED_BODY_END)


def test_complexity_tiers_and_agent_counts_are_strictly_increasing():
    samples = [
        "x" * 100,
        "x" * 1_500,
        "x" * 7_500,
        "x" * 25_000,
    ]

    results = [estimate_complexity(sample) for sample in samples]

    assert [result["tier"] for result in results] == [
        "trivial",
        "low",
        "medium",
        "high",
    ]
    agent_counts = [result["max_agents"] for result in results]
    assert all(left < right for left, right in zip(agent_counts, agent_counts[1:]))


def test_diff_structure_increases_complexity_and_summary_is_auditable():
    plain = estimate_complexity("small change")
    diff = estimate_complexity(
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        + "-old\n+new\n" * 20
    )

    assert diff["max_agents"] > plain["max_agents"]
    assert "1 files" in diff["summary"]
    assert "40 source lines" in diff["summary"]
    json.dumps(diff)


def test_gate_module_imports_only_standard_library_modules():
    path = Path(__file__).parents[1] / "gates.py"
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
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
    )

    assert imported_roots <= sys.stdlib_module_names


@pytest.mark.parametrize("bad_limit", [-1, True, 1.5])
def test_cap_rejects_invalid_limits(bad_limit):
    with pytest.raises((TypeError, ValueError)):
        enforce_output_cap("text", bad_limit)


def test_pre_build_refuses_unresolvable_command_before_provider_call(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'gate-test'\n")
    provider_calls: list[str] = []

    result = pre_build_gate(tmp_path, ["definitely-not-a-real-gate-command"])
    if result["ok"]:
        provider_calls.append("build")

    assert result["ok"] is False
    assert result["exit_code"] == 127
    assert result["infra"] is True
    assert result["project_markers"] == ["pyproject.toml"]
    assert result["resolved_executable"] == ""
    assert provider_calls == []


def test_pre_build_requires_a_project_marker(tmp_path):
    result = pre_build_gate(tmp_path, [sys.executable, "-c", "pass"])

    assert result["ok"] is False
    assert result["exit_code"] == 2
    assert result["infra"] is False
    assert result["project_markers"] == []


@pytest.mark.parametrize("gate", [post_build_gate, post_fix_gate])
def test_post_gate_success_merges_output_and_does_not_invoke_a_shell(tmp_path, gate):
    literal_argument = "literal; exit 91"
    result = gate(
        tmp_path,
        [
            sys.executable,
            "-c",
            (
                "import sys; print(sys.argv[1], flush=True); "
                "print('diagnostic', file=sys.stderr, flush=True)"
            ),
            literal_argument,
        ],
    )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["infra"] is False
    assert literal_argument in result["log"]
    assert "diagnostic" in result["log"]
    assert result["truncated"] is False
    _assert_gate_result_shape(result)


def test_string_gate_preserves_shell_syntax_in_preflight_and_execution(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'gate-test'\n")
    command = (
        "GATE_VALUE=second; "
        "printf 'first\\n' && printf '%s\\n' \"$GATE_VALUE\""
    )

    preflight = pre_build_gate(tmp_path, command)
    result = post_build_gate(tmp_path, command)

    assert preflight["ok"] is True
    assert Path(preflight["resolved_executable"]).name == "sh"
    assert result["ok"] is True
    assert result["log"] == "first\nsecond\n"
    assert result["command"] == command


def test_post_gate_reports_verification_failure_as_non_infrastructure(tmp_path):
    result = post_fix_gate(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import sys; print('tests failed'); raise SystemExit(7)",
        ],
    )

    assert result["ok"] is False
    assert result["exit_code"] == 7
    assert result["infra"] is False
    assert result["log"] == "tests failed\n"
    _assert_gate_result_shape(result)


def test_post_gate_reports_spawn_failure_with_structured_evidence(tmp_path, monkeypatch):
    popen_options = {}

    def refuse_spawn(*args, **kwargs):
        popen_options.update(kwargs)
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(gates.subprocess, "Popen", refuse_spawn)

    result = post_build_gate(tmp_path, [sys.executable, "-c", "pass"])

    assert result["ok"] is False
    assert result["exit_code"] == 126
    assert result["infra"] is True
    assert "simulated spawn failure" in result["error"]
    assert popen_options["shell"] is False
    assert popen_options["start_new_session"] is True
    _assert_gate_result_shape(result)


def test_post_gate_timeout_is_an_infrastructure_failure(tmp_path):
    result = post_build_gate(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(30)",
        ],
        timeout=0.1,
    )

    assert result["ok"] is False
    assert result["exit_code"] == 124
    assert result["infra"] is True
    assert result["log"] == "started\n"
    assert "timed out" in result["error"]
    _assert_gate_result_shape(result)


def test_post_gate_timeout_bounds_output_drain_when_cleanup_fails(
    tmp_path, monkeypatch
):
    class UnkillableProcess:
        pid = 12345
        stdout = None
        returncode = None

        def __init__(self):
            self.timeouts = []

        def communicate(self, timeout=None):
            self.timeouts.append(timeout)
            raise subprocess.TimeoutExpired(
                "gate", timeout, output=b"partial output\n"
            )

    proc = UnkillableProcess()
    monkeypatch.setattr(gates.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(
        gates,
        "_kill_process_group",
        lambda _proc: "could not kill gate process",
    )

    result = post_build_gate(
        tmp_path, [sys.executable, "-c", "pass"], timeout=0.1
    )

    assert result["exit_code"] == 124
    assert result["log"] == "partial output\n"
    assert result["cleanup_error"] == "could not kill gate process"
    assert proc.timeouts == [0.1, gates._POST_KILL_DRAIN_TIMEOUT]


def test_post_gate_timeout_kills_descendant_process_group(tmp_path):
    heartbeat = tmp_path / "child-heartbeat"
    child_code = (
        "import pathlib, sys, time\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "with path.open('a', encoding='utf-8') as stream:\n"
        "    while True:\n"
        "        stream.write('x')\n"
        "        stream.flush()\n"
        "        time.sleep(0.01)\n"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        f"child_code = {child_code!r}\n"
        f"heartbeat = {str(heartbeat)!r}\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', child_code, heartbeat],\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        ")\n"
        "deadline = time.monotonic() + 5\n"
        "while not pathlib.Path(heartbeat).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    child_pid = None

    try:
        result = post_fix_gate(
            tmp_path,
            [sys.executable, "-c", parent_code],
            timeout=0.5,
        )
        child_pid = int(result["log"].strip())
        time.sleep(0.05)
        size_after_cleanup = heartbeat.stat().st_size
        time.sleep(0.15)

        assert result["exit_code"] == 124
        assert heartbeat.stat().st_size == size_after_cleanup
        assert "cleanup_error" not in result
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_post_gate_log_truncation_is_bounded_and_deterministic(tmp_path):
    payload = "0123456789" * 10
    result = post_build_gate(
        tmp_path,
        [sys.executable, "-c", f"print({payload!r}, end='')"],
        max_log_chars=32,
    )

    marker = "[...truncated...]\n"
    assert result["truncated"] is True
    assert result["log"] == marker + payload[-(32 - len(marker)):]
    assert len(result["log"]) == 32


@pytest.mark.parametrize("gate", [pre_build_gate, post_build_gate, post_fix_gate])
def test_invalid_command_sequence_returns_structured_failure(tmp_path, gate):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'gate-test'\n")
    result = gate(tmp_path, [sys.executable, 1])  # type: ignore[list-item]

    assert result["exit_code"] == 127
    assert result["infra"] is True
    assert "sequence of strings" in result["error"]
    _assert_gate_result_shape(result)


# -- P5: shared executable-contract settle gate (gates.run_contract_gate) --
#
# [R2, R3; AC3, AC3a, AC4]. AC1/AC2 drive the settle decision itself; AC3/
# AC3a prove that decision is identical no matter which of the four
# pipelines' import path reaches it (the shared entry point for F1
# enforcement, R2) — both for a spec that settles APPROVE and one that
# settles REJECT. AC4 is the full suite, exercised by CI rather than a
# single test here.


def _env_with_identity():
    return {
        **os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co",
    }


def _init_contract_repo(root, files):
    """Init a throwaway git repo at *root* with *files* ({relpath: text}) committed."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=_env_with_identity())
    for relpath, text in files.items():
        (root / relpath).write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "base"], cwd=root, check=True, env=_env_with_identity(),
    )


def _write_two_ac_spec(spec_path, *, ac2_pattern):
    """A spec with grep directives bound to AC1 (always matches) and AC2
    (matches *ac2_pattern* in tracked.txt) — grep needs no containment (R1),
    so these settle deterministically on any host, sandboxed or not."""
    spec_path.write_text(
        "# Spec\n\n"
        "## Acceptance criteria\n\n"
        "- AC1: has foo\n"
        "  ```ac-directive\n"
        "  ac: AC1\n"
        "  kind: grep\n"
        "  command: grep foo tracked.txt\n"
        "  ```\n"
        "- AC2: has the target token\n"
        "  ```ac-directive\n"
        "  ac: AC2\n"
        "  kind: grep\n"
        f"  command: grep {ac2_pattern} tracked.txt\n"
        "  ```\n"
    )


def test_contract_gate_approves_when_all_pass(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_contract_repo(repo, {"tracked.txt": "foo line\nbar line\n"})
    spec_path = tmp_path / "spec.md"
    _write_two_ac_spec(spec_path, ac2_pattern="bar")

    result = gates.run_contract_gate(str(spec_path), str(repo))

    assert result["settle"] == "APPROVE", result
    assert result["ac_status"] == {"AC1": "pass", "AC2": "pass"}
    assert result["failures"] == []
    assert len(result["directives"]) == 2
    assert all(d["status"] == "pass" for d in result["directives"])


def test_contract_gate_rejects_on_single_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # AC2's pattern is absent from tracked.txt -> that one directive fails.
    _init_contract_repo(repo, {"tracked.txt": "foo line\nno match here\n"})
    spec_path = tmp_path / "spec.md"
    _write_two_ac_spec(spec_path, ac2_pattern="missingtoken")

    result = gates.run_contract_gate(str(spec_path), str(repo))

    assert result["settle"] == "REJECT", result
    assert result["ac_status"] == {"AC1": "pass", "AC2": "fail"}
    assert len(result["failures"]) == 1
    failure = result["failures"][0]
    assert failure["ac"] == "AC2"
    assert failure["reason"] == "absent"


def test_contract_gate_runs_valid_directives_despite_a_sibling_parse_error(tmp_path):
    # AC1 is well-formed and would pass; AC2's directive has an unknown
    # `kind`, so it's a parse error rather than a directive. The parse
    # error must not suppress AC1 from running and appearing in the result.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_contract_repo(repo, {"tracked.txt": "foo line\n"})
    spec_path = tmp_path / "spec.md"
    spec_path.write_text(
        "# Spec\n\n"
        "## Acceptance criteria\n\n"
        "- AC1: has foo\n"
        "  ```ac-directive\n"
        "  ac: AC1\n"
        "  kind: grep\n"
        "  command: grep foo tracked.txt\n"
        "  ```\n"
        "- AC2: malformed\n"
        "  ```ac-directive\n"
        "  ac: AC2\n"
        "  kind: not-a-real-kind\n"
        "  command: grep foo tracked.txt\n"
        "  ```\n"
    )

    result = gates.run_contract_gate(str(spec_path), str(repo))

    assert result["settle"] == "REJECT", result
    assert result["ac_status"]["AC1"] == "pass"
    assert result["ac_status"]["AC2"] == "fail"
    assert len(result["directives"]) == 1
    assert result["directives"][0]["status"] == "pass"
    assert any(
        f["ac"] == "AC2" and f["id"] is None and "parse error" in f["reason"]
        for f in result["failures"]
    )


# Each pipeline's own bootstrap (see the top of e.g. adversarial_spec.py)
# does exactly this: put the pipeline's own script dir and the
# adversarial-common skill root on sys.path, then import adversarial_common.
_PIPELINE_ENTRY_SCRIPTS = (
    "adversarial-spec/scripts/adversarial_spec.py",
    "adversarial-plan/scripts/adversarial_plan.py",
    "adversarial-code-loop/scripts/adversarial_loop_v4.py",
    "adversarial-code-review/scripts/adversarial_review.py",
)

_SUBPROCESS_GATE_SCRIPT = """
import json
import sys

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])

from adversarial_common.gates import run_contract_gate

result = run_contract_gate(sys.argv[3], sys.argv[4])
print(json.dumps({"settle": result["settle"], "ac_status": result["ac_status"]}))
"""


def _settle_via_pipeline_import_path(pipeline_root, common_root, spec_path, repo_root):
    """Run run_contract_gate in a fresh subprocess using one pipeline's own
    sys.path bootstrap, so the import genuinely happens by that pipeline's
    path rather than reusing this test process's already-cached module."""
    proc = subprocess.run(
        [
            sys.executable, "-c", _SUBPROCESS_GATE_SCRIPT,
            str(pipeline_root), str(common_root), str(spec_path), str(repo_root),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("expect_approve", [True, False])
def test_contract_gate_identical_across_pipelines(tmp_path, expect_approve):
    skills = Path(__file__).resolve().parents[3]
    common_root = skills / "adversarial-common"
    scripts = [skills / rel for rel in _PIPELINE_ENTRY_SCRIPTS]
    missing = [str(p) for p in scripts if not p.is_file()]
    if missing:
        pytest.skip(
            f"sibling skill repo(s) not found: {missing} — "
            "this gate only runs in a multi-repo dev workspace"
        )

    repo = tmp_path / "repo"
    repo.mkdir()
    text = "foo line\nbar line\n" if expect_approve else "foo line\nno match here\n"
    _init_contract_repo(repo, {"tracked.txt": text})
    spec_path = tmp_path / "spec.md"
    _write_two_ac_spec(spec_path, ac2_pattern=("bar" if expect_approve else "missingtoken"))

    settled = [
        _settle_via_pipeline_import_path(
            script.parent.parent, common_root, spec_path, repo,
        )
        for script in scripts
    ]

    expected_settle = "APPROVE" if expect_approve else "REJECT"
    assert all(s["settle"] == expected_settle for s in settled), settled
    assert all(s == settled[0] for s in settled), settled


def _assert_gate_result_shape(result):
    assert {
        "gate",
        "command",
        "ok",
        "exit_code",
        "infra",
        "log",
        "truncated",
    } <= result.keys()
    json.dumps(result)
