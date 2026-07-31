"""Retry and cap acceptance tests for the canonical CLI runner."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from adversarial_common import gates, runner
from adversarial_common.costs import CostLedger
from adversarial_common.quota import (
    NoProviderAvailable,
    OK,
    ProviderDecision,
)


class FakeClock:
    def __init__(self, values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


def _attempts(monkeypatch, results):
    pending = deque(results)
    monkeypatch.setattr(runner, "_execute_attempt", lambda *args: pending.popleft())


def _decision(command="echo build", **overrides):
    values = {
        "alias": "codex",
        "command": command,
        "quota_state": OK,
        "fallback": False,
        "reason": "selected first eligible provider",
        "raw_snapshot": {},
        "forced": False,
        "error": None,
    }
    values.update(overrides)
    return ProviderDecision(**values)


def _active_processes():
    with runner._ACTIVE_PROCESSES_LOCK:
        return tuple(runner._ACTIVE_PROCESSES)


def _wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _pid_is_running(pid):
    try:
        stat = os.path.join("/proc", str(pid), "stat")
        with open(stat, encoding="utf-8") as handle:
            return handle.read().split()[2] != "Z"
    except FileNotFoundError:
        return False


def test_run_phase_cmd_resolves_command_and_records_decision(monkeypatch):
    calls = []

    class Resolver:
        def resolve(self, role, **kwargs):
            calls.append((role, kwargs))
            return _decision()

    def fake_run_cli(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return runner.RunResult(("done", "", 0), {"existing": True})

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)

    result = runner.run_phase_cmd(
        "build",
        "dev",
        "/tmp/project",
        Resolver(),
        persona_file="/tmp/test_persona.md",
    )

    assert calls[0] == (
        "dev",
        {
            "workdir": "/tmp/project",
            "force": False,
            "force_provider": None,
        },
    )
    assert calls[1] == (
        "echo build",
        {
            "cwd": "/tmp/project",
            "phase": "build",
            "persona_file": "/tmp/test_persona.md",
        },
    )
    assert result.metadata == {
        "existing": True,
        "provider_decision": {
            "phase": "build",
            "alias": "codex",
            "quota_state": "OK",
            "fallback": False,
            "forced": False,
            "reason": "selected first eligible provider",
            "raw_snapshot": {},
        },
    }


def test_run_phase_cmd_explicit_command_skips_resolution(monkeypatch):
    class Resolver:
        def resolve(self, *args, **kwargs):
            raise AssertionError("explicit commands must bypass the resolver")

    received = []

    def fake_run_cli(cmd, **kwargs):
        received.append((cmd, kwargs))
        return runner.RunResult(("manual", "", 0))

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)

    result = runner.run_phase_cmd(
        "build", "dev", "/tmp/project", Resolver(), explicit_cmd="echo manual"
    )

    assert received[0][0] == "echo manual"
    assert "provider_decision" not in result.metadata


def test_run_phase_cmd_without_resolver_preserves_legacy_command(monkeypatch):
    received = []

    def fake_run_cli(cmd, **kwargs):
        received.append((cmd, kwargs))
        return runner.RunResult(("legacy", "", 0))

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)

    result = runner.run_phase_cmd(
        "review",
        "critic",
        "/tmp/project",
        None,
        explicit_cmd="echo legacy",
    )

    assert received[0][0] == "echo legacy"
    assert "provider_decision" not in result.metadata


def test_run_phase_cmd_without_resolver_accepts_cmd_keyword(monkeypatch):
    received = []

    def fake_run_cli(cmd, **kwargs):
        received.append((cmd, kwargs))
        return runner.RunResult(("legacy", "", 0))

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)

    runner.run_phase_cmd(
        "review",
        "critic",
        "/tmp/project",
        None,
        cmd="echo legacy",
    )

    assert received[0][0] == "echo legacy"


def test_run_phase_cmd_returns_reject_when_no_provider_is_available(monkeypatch):
    class Resolver:
        def resolve(self, *args, **kwargs):
            raise NoProviderAvailable(
                "dev",
                {"codex": {"status": 429}},
                {"codex": "quota state RATE-LIMITED"},
            )

    monkeypatch.setattr(
        runner,
        "run_cli",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("run_cli must not run without an eligible provider")
        ),
    )

    result = runner.run_phase_cmd(
        "build", "dev", "/tmp/project", Resolver()
    )

    assert result[0] == ""
    assert result[2] == 4
    assert result[2] != runner.COST_BUDGET_EXIT_CODE
    assert result.metadata["error"] == (
        "No provider available for role 'dev': "
        "codex: quota state RATE-LIMITED"
    )
    assert result.metadata["rejection_reasons"] == {
        "codex": "quota state RATE-LIMITED"
    }
    assert result.metadata["raw_snapshots"] == {
        "codex": {"status": 429}
    }
    assert result.metadata["provider_decision"] == {
        "phase": "build",
        "alias": None,
        "quota_state": "UNKNOWN",
        "fallback": False,
        "forced": False,
        "reason": "no provider available",
        "raw_snapshot": {"codex": {"status": 429}},
    }


def test_run_phase_cmd_does_not_read_resolver_history_on_failure(monkeypatch):
    class Resolver:
        @property
        def history(self):
            raise AssertionError("shared resolver history must not be read")

        def resolve(self, *args, **kwargs):
            raise NoProviderAvailable("dev", {}, {})

    monkeypatch.setattr(
        runner,
        "run_cli",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("run_cli must not run without an eligible provider")
        ),
    )

    result = runner.run_phase_cmd(
        "build", "dev", "/tmp/project", Resolver()
    )

    assert result[2] == 4


def test_run_phase_cmd_rejects_ambiguous_cmd_arguments():
    class Resolver:
        def resolve(self, *args, **kwargs):
            raise AssertionError("ambiguous arguments must fail first")

    for resolver, explicit_cmd in (
        (None, "echo explicit"),
        (Resolver(), None),
    ):
        try:
            runner.run_phase_cmd(
                "build",
                "dev",
                "/tmp/project",
                resolver,
                explicit_cmd=explicit_cmd,
                cmd="echo legacy",
            )
        except TypeError as exc:
            assert str(exc) == (
                "cmd may only be supplied when explicit_cmd is None and "
                "resolver is None"
            )
        else:
            raise AssertionError("ambiguous cmd arguments must raise TypeError")


def test_run_phase_cmd_forwards_force_mode_and_expanded_command(monkeypatch):
    received = []

    class Resolver:
        def resolve(self, role, *, workdir, force, force_provider):
            assert (role, force, force_provider) == ("dev", False, "claude")
            return _decision(
                f"claude-tmux --cwd {workdir}",
                alias="claude",
                forced=True,
                reason="forced requested provider",
            )

    def fake_run_cli(cmd, **kwargs):
        received.append((cmd, kwargs))
        return runner.RunResult(("", "", 0))

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)

    result = runner.run_phase_cmd(
        "build",
        "dev",
        "/tmp/test",
        Resolver(),
        force_provider="claude",
    )

    assert received[0][0] == "claude-tmux --cwd /tmp/test"
    assert result.metadata["provider_decision"]["forced"] is True


def test_fast_124_retries_once_then_succeeds(monkeypatch):
    _attempts(monkeypatch, [
        ("", "provider timeout", 124, True, False),
        ("done", "", 0, True, False),
    ])
    sleeps = []
    attempts = []

    result = runner.run_cli(
        ["codex"],
        timeout=10,
        max_retries=3,
        base=2,
        jitter=1,
        clock=FakeClock([0.0, 0.1, 0.1, 0.2]),
        sleeper=sleeps.append,
        rng=lambda low, high: 0.5,
        attempt_log=attempts,
    )

    assert tuple(result) == ("done", "", 0)
    assert len(attempts) == 2
    assert attempts == result.metadata["attempts"]
    assert sleeps == [2.5]


def test_missing_binary_is_recorded_once_without_sleep():
    def unexpected_sleep(delay):
        raise AssertionError(f"unexpected retry delay: {delay}")

    result = runner.run_cli(
        ["definitely-not-a-real-command"],
        max_retries=3,
        sleeper=unexpected_sleep,
    )

    assert result[2] == 127
    assert len(result.metadata["attempts"]) == 1
    assert result.metadata["attempts"][0]["reason"] == "permanent"


def test_registry_is_empty_after_normal_and_timeout_execution():
    normal = runner.run_cli(
        [sys.executable, "-c", "print('done')"], max_retries=0
    )
    timed_out = runner.run_cli(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.05,
        max_retries=0,
    )

    assert tuple(normal) == ("done", "", 0)
    assert timed_out[2] == 124
    assert _active_processes() == ()


def test_started_process_is_cleaned_up_when_communicate_is_interrupted(
    monkeypatch,
):
    signals = []

    class InterruptedProcess:
        pid = 999_999_999
        returncode = None

        def communicate(self, **kwargs):
            raise KeyboardInterrupt

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    process = InterruptedProcess()

    def signal_process_group(target, sig):
        assert target is process
        signals.append(sig)
        if sig == signal.SIGKILL:
            target.returncode = -sig

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runner, "_signal_process_group", signal_process_group)
    monkeypatch.setattr(runner, "_TERMINATION_GRACE_SECONDS", 0)

    try:
        runner._execute_attempt(["provider"], None, 30, None)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("the provider interrupt must propagate")

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert _active_processes() == ()


def test_reaped_process_group_is_not_probed(monkeypatch):
    probes = []

    class ReapedProcess:
        pid = 1234

        def poll(self):
            return 0

    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sig: probes.append((pid, sig)),
    )

    assert not runner._process_group_is_active(ReapedProcess())
    assert probes == []


def test_process_creation_and_registration_are_atomic_with_cleanup(monkeypatch):
    popen_entered = threading.Event()
    release_popen = threading.Event()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    terminated = threading.Event()
    signals = []

    class BlockingProcess:
        pid = 999_999_998
        returncode = None

        def communicate(self, **kwargs):
            assert terminated.wait(3)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    process = BlockingProcess()

    def create_process(*args, **kwargs):
        popen_entered.set()
        assert release_popen.wait(3)
        return process

    def signal_process_group(target, sig):
        assert target is process
        signals.append(sig)
        target.returncode = -sig
        terminated.set()

    def cleanup():
        cleanup_started.set()
        runner.terminate_active_processes()
        cleanup_finished.set()

    monkeypatch.setattr(runner.subprocess, "Popen", create_process)
    monkeypatch.setattr(runner, "_signal_process_group", signal_process_group)

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempt = pool.submit(
            runner._execute_attempt, ["provider"], None, 30, None
        )
        assert popen_entered.wait(3)
        cleanup_call = pool.submit(cleanup)
        assert cleanup_started.wait(3)
        assert not cleanup_finished.wait(0.05)

        release_popen.set()

        cleanup_call.result(timeout=3)
        assert attempt.result(timeout=3)[2] == -signal.SIGTERM

    assert signals == [signal.SIGTERM]
    assert _active_processes() == ()


def test_terminate_active_processes_handles_concurrent_repeated_cleanup():
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            runner.run_cli,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            max_retries=0,
        )
        assert _wait_until(lambda: len(_active_processes()) == 1)

        with ThreadPoolExecutor(max_workers=4) as terminators:
            calls = [
                terminators.submit(runner.terminate_active_processes)
                for _ in range(4)
            ]
            for call in calls:
                call.result(timeout=3)

        assert result.result(timeout=3)[2] != 0

    runner.terminate_active_processes()
    assert _active_processes() == ()


def test_terminate_active_processes_allows_graceful_sigterm_exit(tmp_path):
    ready_path = tmp_path / "ready"
    script = (
        "import signal, sys, time\n"
        "def stop(*args):\n"
        "    time.sleep(0.2)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"open({str(ready_path)!r}, 'w').close()\n"
        "time.sleep(30)\n"
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            runner.run_cli,
            [sys.executable, "-c", script],
            max_retries=0,
        )
        assert _wait_until(ready_path.exists)

        runner.terminate_active_processes()

        assert result.result(timeout=3)[2] == 0

    assert _active_processes() == ()


def test_terminate_active_processes_ignores_already_exited_child():
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"], start_new_session=True
    )
    process.wait(timeout=3)
    runner._register_process(process)

    runner.terminate_active_processes()
    runner.terminate_active_processes()

    assert _active_processes() == ()


def test_terminate_active_processes_kills_provider_process_tree(tmp_path):
    grandchild_pid_path = tmp_path / "grandchild.pid"
    script = (
        "import signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)']); "
        f"open({str(grandchild_pid_path)!r}, 'w').write(str(child.pid)); "
        "time.sleep(30)"
    )
    provider_pid = None
    grandchild_pid = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                runner.run_cli,
                [sys.executable, "-c", script],
                max_retries=0,
            )
            assert _wait_until(grandchild_pid_path.exists)
            provider_pid = _active_processes()[0].pid
            grandchild_pid = int(grandchild_pid_path.read_text())

            runner.terminate_active_processes()

            assert result.result(timeout=3)[2] != 0

        assert _wait_until(lambda: not _pid_is_running(provider_pid))
        assert _wait_until(lambda: not _pid_is_running(grandchild_pid))
        assert _active_processes() == ()
    finally:
        if provider_pid is not None:
            try:
                os.killpg(provider_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_missing_wrapper_diagnostic_names_command_and_configuration(monkeypatch):
    def missing_wrapper(*args, **kwargs):
        raise FileNotFoundError("missing wrapper")

    monkeypatch.setattr(runner.subprocess, "Popen", missing_wrapper)
    result = runner.run_cli(["claude-tmux.py"], max_retries=0)

    assert result[2] == 127
    assert "Claude wrapper command not found: claude-tmux.py" in result[1]
    assert "ADVERSARIAL_CLAUDE_TMUX_PATH" in result[1]
    assert "PATH" in result[1]


def test_hard_4xx_is_not_retried(monkeypatch):
    _attempts(monkeypatch, [("", "HTTP 401 Unauthorized", 1, True, False)])
    result = runner.run_cli(
        ["claude"],
        max_retries=3,
        clock=FakeClock([0.0, 0.1]),
        sleeper=lambda delay: (_ for _ in ()).throw(AssertionError(delay)),
    )
    assert result[2] == 1
    assert len(result.metadata["attempts"]) == 1


def test_retry_bounds_and_delays_are_exact(monkeypatch):
    _attempts(monkeypatch, [
        ("", "connection reset by peer", 1, True, False),
        ("", "connection reset by peer", 1, True, False),
        ("", "connection reset by peer", 1, True, False),
        ("", "connection reset by peer", 1, True, False),
    ])
    sleeps = []
    result = runner.run_cli(
        ["codex"],
        max_retries=3,
        base=2,
        jitter=1,
        clock=FakeClock([0.0, 0.1] * 4),
        sleeper=sleeps.append,
        rng=lambda low, high: 0.25,
    )

    assert result[2] == 1
    assert len(result.metadata["attempts"]) == 4
    assert sleeps == [2.25, 4.25, 8.25]
    assert all(
        lower <= delay <= lower + 1
        for delay, lower in zip(sleeps, (2, 4, 8), strict=True)
    )


def test_input_cap_rejects_before_subprocess_start(monkeypatch):
    def unexpected_popen(*args, **kwargs):
        raise AssertionError("subprocess must not start")

    monkeypatch.setattr(runner.subprocess, "Popen", unexpected_popen)
    result = runner.run_cli(["codex"], stdin_text="x" * 11, max_input_chars=10)

    assert result[2] != 0
    assert result.metadata["input_rejected"] is True
    assert result.metadata["attempts"] == []


def test_input_cap_can_head_truncate_with_marker(monkeypatch):
    transmitted = []

    def execute(argv, stdin_text, timeout, cwd):
        transmitted.append(stdin_text)
        return "ok", "", 0, True, False

    monkeypatch.setattr(runner, "_execute_attempt", execute)
    result = runner.run_cli(
        ["codex"],
        stdin_text="x" * 50,
        max_input_chars=30,
        truncate_input=True,
        clock=FakeClock([0.0, 0.1]),
    )

    assert result[2] == 0
    assert len(transmitted[0]) == 30
    assert transmitted[0].endswith(gates.TRUNCATION_MARKER)
    assert result.metadata["cap_events"][0]["kind"] == "input"


def test_run_cli_delimits_persona_from_untrusted_input(monkeypatch, tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("TRUSTED PERSONA")
    transmitted = []

    def execute(argv, stdin_text, timeout, cwd):
        transmitted.append(stdin_text)
        return "ok", "", 0, True, False

    monkeypatch.setattr(runner, "_execute_attempt", execute)
    result = runner.run_cli(
        ["codex"],
        stdin_text="UNTRUSTED BODY",
        persona_file=persona,
        clock=FakeClock([0.0, 0.1]),
    )

    assert result[2] == 0
    assert transmitted == [
        "TRUSTED PERSONA\n\n"
        "--- END TRUSTED PERSONA ---\n"
        "--- BEGIN UNTRUSTED CONTENT ---\n"
        "UNTRUSTED BODY\n"
        "--- END UNTRUSTED CONTENT ---"
    ]


def test_output_cap_is_marked_and_recorded(monkeypatch):
    _attempts(monkeypatch, [("y" * 50, "", 0, True, False)])
    result = runner.run_cli(
        ["codex"],
        max_output_chars=30,
        clock=FakeClock([0.0, 0.1]),
    )

    assert len(result[0]) == 30
    assert result[0].endswith(gates.TRUNCATION_MARKER)
    assert result.metadata["cap_events"] == [{
        "kind": "output",
        "stream": "stdout",
        "attempt": 1,
        "limit": 30,
        "original_chars": 50,
        "truncated": True,
    }]


def test_genuine_runner_timeout_is_never_retried(monkeypatch):
    _attempts(monkeypatch, [("", "TIMEOUT after 10s", 124, True, True)])
    result = runner.run_cli(
        ["codex"],
        timeout=10,
        clock=FakeClock([0.0, 0.1]),
        sleeper=lambda delay: (_ for _ in ()).throw(AssertionError(delay)),
    )
    assert result[2] == 124
    assert len(result.metadata["attempts"]) == 1


def test_native_usage_precedes_char_estimate_and_reaches_metadata(monkeypatch):
    native_output = (
        '{"type":"turn.completed","usage":'
        '{"input_tokens":17,"output_tokens":6}}'
    )
    _attempts(monkeypatch, [(native_output, "", 0, True, False)])
    ledger = CostLedger(
        prices={"codex-model": {"prompt": 1, "completion": 2}},
        env={},
    )

    result = runner.run_cli(
        ["codex"],
        stdin_text="x" * 400,
        ledger=ledger,
        model="codex-model",
        phase="review",
        persona="critic",
        include_usage=True,
        clock=FakeClock([0.0, 0.1]),
    )

    assert tuple(result)[3]["prompt_tokens"] == 17
    assert tuple(result)[3]["completion_tokens"] == 6
    assert tuple(result)[3]["estimated"] is False
    assert result.metadata["usage"] == tuple(result)[3]


def test_retry_accounts_for_each_started_attempt_once(monkeypatch):
    first = (
        '{"usage":{"input_tokens":3,"output_tokens":2}}',
        "connection reset by peer",
        1,
        True,
        False,
    )
    second = (
        '{"usage":{"input_tokens":5,"output_tokens":4}}',
        "",
        0,
        True,
        False,
    )
    _attempts(monkeypatch, [first, second])
    ledger = CostLedger(
        prices={"codex-model": {"prompt": 1, "completion": 1}},
        env={},
    )

    result = runner.run_cli(
        ["codex"],
        ledger=ledger,
        model="codex-model",
        phase="review",
        persona="critic",
        include_usage=True,
        max_retries=1,
        base=0,
        jitter=0,
        clock=FakeClock([0.0, 0.1, 0.1, 0.2]),
        sleeper=lambda delay: None,
    )

    assert result[2] == 0
    assert [
        (item.prompt_tokens, item.completion_tokens)
        for item in ledger.records
    ] == [(3, 2), (5, 4)]
    assert all("usage" in attempt for attempt in result.metadata["attempts"])
    assert result[3]["prompt_tokens"] == 8
    assert result[3]["completion_tokens"] == 6
    assert result.metadata["usage"] == result[3]


def test_budget_refuses_before_starting_provider(monkeypatch):
    ledger = CostLedger(
        prices={"model": {"prompt": 1, "completion": 1}},
        env={},
    )
    ledger.record(
        "model",
        usage={"input_tokens": 1_000_000, "output_tokens": 0},
    )

    def unexpected_attempt(*args):
        raise AssertionError("provider must not start above budget")

    monkeypatch.setattr(runner, "_execute_attempt", unexpected_attempt)
    result = runner.run_cli(
        ["codex"],
        stdin_text="abcd",
        ledger=ledger,
        model="model",
        budget=1.0,
        max_completion_tokens=0,
    )

    assert result[2] == runner.COST_BUDGET_EXIT_CODE
    assert result.metadata["budget_exceeded"] is True
    assert result.metadata["attempts"] == []
    assert len(ledger.records) == 1


def test_budget_projects_completion_cost_before_start(monkeypatch):
    ledger = CostLedger(
        prices={"model": {"prompt": 0, "completion": 1}},
        env={},
    )

    def unexpected_attempt(*args):
        raise AssertionError("provider must not start above budget")

    monkeypatch.setattr(runner, "_execute_attempt", unexpected_attempt)
    result = runner.run_cli(
        ["codex"],
        ledger=ledger,
        model="model",
        budget=0.5,
        max_completion_tokens=500_001,
    )

    assert result[2] == runner.COST_BUDGET_EXIT_CODE
    assert result.metadata["budget"]["projected_call_usd"] == 0.500001


def test_parallel_budget_reservation_admits_only_affordable_calls(monkeypatch):
    ledger = CostLedger(
        prices={"model": {"prompt": 0, "completion": 1}},
        env={},
    )
    first_started = threading.Event()
    finish_first = threading.Event()
    start_count = 0
    start_lock = threading.Lock()

    def execute(*args):
        nonlocal start_count
        with start_lock:
            start_count += 1
        first_started.set()
        assert finish_first.wait(timeout=2)
        return "done", "", 0, True, False

    monkeypatch.setattr(runner, "_execute_attempt", execute)
    kwargs = {
        "cmd": ["codex"],
        "ledger": ledger,
        "model": "model",
        "budget": 1.0,
        "max_completion_tokens": 750_000,
        "usage": {"input_tokens": 0, "output_tokens": 750_000},
        "clock": lambda: 0.0,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.run_cli, **kwargs)
        assert first_started.wait(timeout=2)
        second = pool.submit(runner.run_cli, **kwargs)
        second_result = second.result(timeout=2)
        finish_first.set()
        first_result = first.result(timeout=2)

    assert first_result[2] == 0
    assert second_result[2] == runner.COST_BUDGET_EXIT_CODE
    assert start_count == 1
    assert len(ledger.records) == 1


def test_default_codex_command_is_costed_without_explicit_model(monkeypatch):
    _attempts(monkeypatch, [("done", "", 0, True, False)])
    ledger = CostLedger(env={})

    result = runner.run_cli(
        ["codex"],
        ledger=ledger,
        usage={"input_tokens": 100, "output_tokens": 50},
        clock=FakeClock([0.0, 0.1]),
    )

    assert result[2] == 0
    record = ledger.records[0]
    assert record.model == "codex"
    assert record.est_cost_usd == 0.000625


def test_show_costs_prints_model_breakdown_to_stderr(monkeypatch, capsys):
    _attempts(monkeypatch, [("done", "", 0, True, False)])
    ledger = CostLedger(
        prices={"model": {"prompt": 1, "completion": 2}},
        env={},
    )

    result = runner.run_cli(
        ["codex"],
        ledger=ledger,
        model="model",
        usage={"input_tokens": 100, "output_tokens": 50},
        show_costs=True,
        clock=FakeClock([0.0, 0.1]),
    )

    assert result[2] == 0
    diagnostic = capsys.readouterr().err
    assert "model: 100 prompt + 50 completion tokens, $0.000200" in diagnostic
    assert "total: $0.000200" in diagnostic


def test_run_parallel_bounds_workers_and_preserves_input_order(monkeypatch):
    lock = threading.Lock()
    active = 0
    peak_active = 0
    delays = {"first": 0.08, "second": 0.01, "third": 0.06, "fourth": 0.01}

    def fake_run_cli(cmd, **kwargs):
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(delays[cmd])
            return f"done:{cmd}", "", 0
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)
    started = time.monotonic()
    results = runner.run_parallel(
        [(label, {"cmd": label}) for label in delays],
        concurrency=2,
    )
    elapsed = time.monotonic() - started

    assert peak_active == 2
    assert elapsed < 0.21
    assert [result["label"] for result in results] == list(delays)
    assert [result["stdout"] for result in results] == [
        f"done:{label}" for label in delays
    ]


def test_run_parallel_contains_exception_to_failing_sibling(monkeypatch):
    def fake_run_cli(cmd, **kwargs):
        if cmd == "bad":
            raise RuntimeError("worker exploded")
        return cmd.upper(), "", 0

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)
    results = runner.run_parallel(
        [
            ("left", {"cmd": "left"}),
            ("bad", {"cmd": "bad"}),
            ("right", {"cmd": "right"}),
        ],
        concurrency=3,
    )

    assert [result["ok"] for result in results] == [True, False, True]
    assert results[0]["stdout"] == "LEFT"
    assert results[1]["error"] == "RuntimeError: worker exploded"
    assert results[2]["stdout"] == "RIGHT"


def test_run_delegated_low_complexity_uses_direct_fallback(monkeypatch):
    calls = []

    def fake_run_cli(cmd, stdin_text=None, **kwargs):
        calls.append((cmd, stdin_text))
        return "direct result", "", 0

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)
    complexity_checks = []
    monkeypatch.setattr(
        runner.gates,
        "estimate_complexity",
        lambda text, max_agents: complexity_checks.append((text, max_agents))
        or {"level": "low", "recommended_agents": 2},
    )
    result = runner.run_delegated(
        "small input",
        {"cmd": "decompose"},
        {"cmd": "worker"},
        {"cmd": "synthesize"},
        fallback_call={"cmd": "direct"},
    )

    assert result["delegated"] is False
    assert result["mode"] == "direct"
    assert result["status"] == "fallback"
    assert "below required level 'high'" in result["reason"]
    assert calls == [("direct", "small input")]
    assert result["result"] == ("direct result", "", 0)
    assert complexity_checks == [("small input", 6)]


def test_run_delegated_synthesizes_survivors_with_worker_origins(monkeypatch):
    synthesis_inputs = []

    def fake_run_cli(cmd, stdin_text=None, **kwargs):
        if cmd == "decompose":
            return json.dumps({
                "tasks": [
                    {"id": "alpha", "scope": "a.py"},
                    {"id": "broken", "scope": "b.py"},
                    {"id": "omega", "scope": "c.py"},
                ]
            }), "", 0
        if cmd == "worker-broken":
            return "", "provider unavailable", 9
        if cmd.startswith("worker-"):
            finding_id = cmd.removeprefix("worker-")
            return json.dumps({
                "findings": [{"id": finding_id, "confidence": "high", "basis": "code"}]
            }), "", 0
        if cmd == "synthesize":
            synthesis_inputs.append(json.loads(stdin_text))
            return json.dumps({
                "findings": [{"id": "combined", "confidence": "high", "basis": "code"}]
            }), "", 0
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)
    result = runner.run_delegated(
        "large input",
        {"cmd": "decompose"},
        lambda task: {"cmd": f"worker-{task['id']}"},
        {"cmd": "synthesize"},
        concurrency=2,
        complexity={"level": "high", "recommended_agents": 3},
    )

    assert result["delegated"] is True
    assert result["status"] == "synthesized"
    assert result["partial"] is True
    assert [worker["ok"] for worker in result["workers"]] == [True, False, True]
    assert [worker["origin"] for worker in result["workers"]] == [
        "worker", "worker", "worker"
    ]
    assert [worker["label"] for worker in result["survivors"]] == ["alpha", "omega"]
    assert len(synthesis_inputs) == 1
    assert [item["label"] for item in synthesis_inputs[0]] == ["alpha", "omega"]
    assert all(
        item["payload"]["findings"][0]["origin"] == "worker"
        for item in synthesis_inputs[0]
    )
    assert (
        result["synthesis"]["payload"]["findings"][0]["origin"]
        == "worker"
    )


def test_worker_origin_only_marks_items_below_findings_key():
    payload = [
        {"id": "metadata", "status": "complete"},
        {"result": {"findings": [{"id": "actual-finding"}]}},
    ]

    runner._mark_worker_findings(payload)

    assert "origin" not in payload[0]
    assert "origin" not in payload[1]
    assert payload[1]["result"]["findings"][0]["origin"] == "worker"
