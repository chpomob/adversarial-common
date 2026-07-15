"""Retry and cap acceptance tests for the canonical CLI runner."""

import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from adversarial_common import gates, runner
from adversarial_common.costs import CostLedger


class FakeClock:
    def __init__(self, values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


def _attempts(monkeypatch, results):
    pending = deque(results)
    monkeypatch.setattr(runner, "_execute_attempt", lambda *args: pending.popleft())


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
