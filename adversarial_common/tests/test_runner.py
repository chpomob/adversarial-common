"""Retry and cap acceptance tests for the canonical CLI runner."""

from collections import deque

from adversarial_common import gates, runner


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

