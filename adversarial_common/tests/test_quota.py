"""Tests for quota-aware provider resolution."""

import threading
import warnings
from pathlib import Path

import pytest

import adversarial_common.quota as quota_module

from adversarial_common import (
    DRAINING,
    KEY_INVALID,
    OK,
    RATE_LIMITED,
    UNKNOWN,
    NoProviderAvailable,
    ProviderConfig,
    ProviderConfigError,
    ProviderDecision,
    ProviderEntry,
    QuotaResolver,
)


def _checker(tmp_path: Path, payload: object, counter: Path | None = None) -> Path:
    path = tmp_path / "quota-check"
    counter_line = ""
    if counter is not None:
        counter_line = (
            "from pathlib import Path\n"
            f"p = Path({str(counter)!r})\n"
            "p.write_text(p.read_text() + '1' if p.exists() else '1')\n"
        )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"{counter_line}"
        f"print(json.dumps({payload!r}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _config(
    *entries: ProviderEntry,
    ttl: int | float = 30,
    role: str = "dev",
) -> ProviderConfig:
    return ProviderConfig(roles={role: entries}, quota_cache_ttl=ttl)


@pytest.mark.parametrize(
    ("raw", "state"),
    [
        ({"session": {"used_pct": 45}}, OK),
        ({"session": {"used_pct": 50}}, DRAINING),
        ({"session": {"used_pct": 105}}, RATE_LIMITED),
        ({"balance": 1.5}, OK),
        ({"balance": 0}, RATE_LIMITED),
        ({"status_code": 429}, RATE_LIMITED),
        ({"error": "invalid API key"}, KEY_INVALID),
        ({}, UNKNOWN),
    ],
)
def test_normalizes_supported_quota_shapes(tmp_path, raw, state):
    checker = _checker(tmp_path, {"results": {"codex": raw}})
    config = _config(ProviderEntry("codex", "echo build", "--codex"))
    resolver = QuotaResolver(config, checker)

    if state in {RATE_LIMITED, KEY_INVALID}:
        with pytest.raises(NoProviderAvailable):
            resolver.resolve("dev", workdir=str(tmp_path))
        decision = resolver.history[-1]
    elif state == UNKNOWN:
        with pytest.warns(RuntimeWarning):
            decision = resolver.resolve("dev", workdir=str(tmp_path))
    else:
        decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision.quota_state == (UNKNOWN if decision.alias is None else state)


def test_snapshot_is_shared_between_roles_until_ttl_expires(
    tmp_path, monkeypatch
):
    counter = tmp_path / "counter"
    checker = _checker(
        tmp_path,
        {
            "results": {
                "codex": {"session": {"used_pct": 10}},
                "claude": {"session": {"used_pct": 20}},
            }
        },
        counter,
    )
    config = ProviderConfig(
        roles={
            "dev": (ProviderEntry("codex", "dev", "--codex"),),
            "review": (ProviderEntry("claude", "review", "--claude"),),
        },
        quota_cache_ttl=30,
    )
    resolver = QuotaResolver(config, checker)
    clock = [100.0]
    monkeypatch.setattr(quota_module.time, "monotonic", lambda: clock[0])

    first = resolver.resolve("dev", workdir=str(tmp_path))
    second = resolver.resolve("review", workdir=str(tmp_path))

    assert counter.read_text() == "1"
    assert first.raw_snapshot == second.raw_snapshot

    clock[0] += 31
    resolver.resolve("dev", workdir=str(tmp_path))
    assert counter.read_text() == "11"


@pytest.mark.parametrize(
    ("raw", "threshold", "blocked"),
    [
        ({"session": {"used_pct": 85}}, 80, True),
        ({"session": {"used_pct": 80}}, 80, False),
        ({"balance": 1.5}, 2.0, True),
        ({"balance": 2.0}, 2.0, False),
    ],
)
def test_stop_threshold_boundaries(tmp_path, raw, threshold, blocked):
    checker = _checker(tmp_path, {"results": {"primary": raw}})
    config = _config(
        ProviderEntry("primary", "echo primary", "--primary", threshold),
        ProviderEntry("fallback", "echo fallback"),
    )
    resolver = QuotaResolver(config, checker)

    with _does_not_warn():
        decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision.alias == ("fallback" if blocked else "primary")
    assert decision.fallback is blocked


def test_no_provider_records_failed_attempt_and_exposes_reasons(tmp_path):
    checker = _checker(
        tmp_path,
        {
            "results": {
                "codex": {"status": 429},
                "claude": {"error": "unauthorized"},
            }
        },
    )
    config = _config(
        ProviderEntry("codex", "codex", "--codex"),
        ProviderEntry("claude", "claude", "--claude"),
    )
    resolver = QuotaResolver(config, checker)

    with pytest.raises(NoProviderAvailable) as caught:
        resolver.resolve("dev", workdir=str(tmp_path))

    assert caught.value.role == "dev"
    assert caught.value.reasons == {
        "codex": "quota state RATE-LIMITED",
        "claude": "quota state KEY_INVALID",
    }
    assert caught.value.snapshots["codex"] == {"status": 429}
    assert resolver.history[-1].alias is None


def test_force_modes_skip_checker_and_expand_workdir(tmp_path):
    missing = tmp_path / "does-not-exist"
    config = _config(
        ProviderEntry("codex", "run --cwd {workdir}", "--codex"),
        ProviderEntry("claude", "other {unknown} {workdir}", "--claude"),
    )
    resolver = QuotaResolver(config, missing)

    primary = resolver.resolve(
        "dev", workdir=str(tmp_path) + "///", force=True
    )
    selected = resolver.resolve(
        "dev", workdir=str(tmp_path), force_provider="claude"
    )

    assert primary.command == f"run --cwd {tmp_path}"
    assert selected.command == f"other {{unknown}} {tmp_path}"
    assert primary.forced and selected.forced
    assert len(resolver.history) == 2


def test_invalid_forced_alias_is_recorded(tmp_path):
    resolver = QuotaResolver(
        _config(ProviderEntry("codex", "run")), tmp_path / "missing"
    )

    with pytest.raises(ProviderConfigError, match="PROVIDER_FORCE_NOT_IN_ROLE"):
        resolver.resolve("dev", workdir=str(tmp_path), force_provider="other")

    assert resolver.history[-1].forced is True
    assert resolver.history[-1].alias is None


def test_checker_failure_falls_back_and_is_cached(tmp_path):
    config = _config(
        ProviderEntry("codex", "echo build", "--codex"), ttl=30
    )
    resolver = QuotaResolver(config, tmp_path / "missing")

    with pytest.warns(
        RuntimeWarning, match="executable not found"
    ) as caught_warnings:
        first = resolver.resolve("dev", workdir=str(tmp_path))
    second = resolver.resolve("dev", workdir=str(tmp_path))

    assert Path(caught_warnings[0].filename) == Path(__file__)
    assert first.alias == second.alias == "codex"
    assert first.fallback and second.fallback
    assert first.quota_state == second.quota_state == UNKNOWN
    assert first.error == second.error == "quota checker executable not found"
    assert first.raw_snapshot == second.raw_snapshot == {"codex": {}}


def test_invalid_json_falls_back_to_primary(tmp_path):
    checker = tmp_path / "quota-check"
    checker.write_text("#!/bin/sh\nprintf 'not json'\n", encoding="utf-8")
    checker.chmod(0o755)
    resolver = QuotaResolver(
        _config(ProviderEntry("codex", "echo build", "--codex")), checker
    )

    with pytest.warns(RuntimeWarning, match="invalid JSON"):
        decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision == ProviderDecision(
        alias="codex",
        command="echo build",
        quota_state=UNKNOWN,
        fallback=True,
        reason="quota checker failed; selected primary provider",
        raw_snapshot={"codex": {}},
        forced=False,
        error="quota checker returned invalid JSON",
    )


def test_unrelated_checker_cache_keys_do_not_block_each_other(
    tmp_path, monkeypatch
):
    slow_started = threading.Event()
    release_slow = threading.Event()
    fast_finished = threading.Event()
    slow_checker = tmp_path / "slow-checker"
    fast_checker = tmp_path / "fast-checker"
    slow = QuotaResolver(
        _config(ProviderEntry("slow", "slow", "--slow")), slow_checker
    )
    fast = QuotaResolver(
        _config(ProviderEntry("fast", "fast", "--fast")), fast_checker
    )

    def run_checker(self):
        if self._checker_path == str(slow_checker):
            slow_started.set()
            assert release_slow.wait(timeout=2)
            alias = "slow"
        else:
            alias = "fast"
        return quota_module._CacheRecord(
            quota_module.time.monotonic(),
            {alias: {"used_pct": 1}},
            None,
        )

    monkeypatch.setattr(QuotaResolver, "_run_checker", run_checker)
    slow_thread = threading.Thread(
        target=slow.resolve, kwargs={"role": "dev", "workdir": str(tmp_path)}
    )
    fast_thread = threading.Thread(
        target=lambda: (
            fast.resolve("dev", workdir=str(tmp_path)), fast_finished.set()
        )
    )

    slow_thread.start()
    assert slow_started.wait(timeout=2)
    fast_thread.start()
    assert fast_finished.wait(timeout=1)
    release_slow.set()
    slow_thread.join(timeout=2)
    fast_thread.join(timeout=2)
    assert not slow_thread.is_alive()
    assert not fast_thread.is_alive()


def test_error_detection_ignores_unrelated_nested_fields(tmp_path):
    checker = _checker(
        tmp_path,
        {
            "results": {
                "codex": {
                    "session": {"used_pct": 5, "status": 429},
                    "notes": "no rate limit issues this week",
                }
            }
        },
    )
    resolver = QuotaResolver(
        _config(ProviderEntry("codex", "codex", "--codex")), checker
    )

    decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision.alias == "codex"
    assert decision.quota_state == OK


def test_flat_result_is_not_broadcast_to_multiple_checked_providers(tmp_path):
    checker = _checker(tmp_path, {"session": {"used_pct": 5}})
    resolver = QuotaResolver(
        _config(
            ProviderEntry("codex", "codex", "--codex"),
            ProviderEntry("claude", "claude", "--claude"),
        ),
        checker,
    )

    with pytest.warns(RuntimeWarning, match="ambiguous"):
        decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision.quota_state == UNKNOWN
    assert decision.error is not None


def test_flat_result_applies_only_to_the_single_checked_provider(tmp_path):
    checker = _checker(tmp_path, {"session": {"used_pct": 5}})
    resolver = QuotaResolver(
        _config(
            ProviderEntry("codex", "codex", "--codex"),
            ProviderEntry("manual", "manual"),
        ),
        checker,
    )

    decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision.alias == "codex"
    assert decision.quota_state == OK


def test_provider_without_quota_check_does_not_warn(tmp_path):
    resolver = QuotaResolver(
        _config(ProviderEntry("manual", "manual")), tmp_path / "missing"
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision.alias == "manual"
    assert decision.quota_state == UNKNOWN


class _does_not_warn:
    def __enter__(self):
        self._catcher = warnings.catch_warnings()
        self._catcher.__enter__()
        warnings.simplefilter("error")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._catcher.__exit__(exc_type, exc_value, traceback)
