"""Acceptance tests for phase- and persona-aware cost accounting."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from adversarial_common.costs import CostLedger


def test_multi_phase_and_per_model_aggregation_reconciles_exactly():
    ledger = CostLedger(
        prices={
            "model-a": {"prompt": 1.0, "completion": 2.0},
            "model-b": {"prompt": 4.0, "completion": 8.0},
        },
        env={},
    )
    first = ledger.record(
        "model-a",
        usage={"input_tokens": 100, "output_tokens": 50},
        phase="build",
        persona="builder",
    )
    second = ledger.record(
        "model-b",
        usage={"prompt_tokens": 200, "completion_tokens": 25},
        phase="review",
        persona="critic",
    )
    third = ledger.record(
        "model-a",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        phase="review",
        persona="critic",
    )

    summary = ledger.summary()

    assert summary["models"]["model-a"] == {
        "prompt_tokens": 110,
        "completion_tokens": 55,
        "est_cost_usd": 0.00022,
        "estimated": False,
    }
    assert summary["phases"]["review"]["prompt_tokens"] == 210
    assert summary["personas"]["critic"]["completion_tokens"] == 30
    assert summary["total"]["est_cost_usd"] == sum(
        record.est_cost_usd for record in (first, second, third)
    )
    assert summary["total"]["est_cost_usd"] == sum(
        item["est_cost_usd"] for item in summary["models"].values()
    )


def test_native_counts_take_precedence_and_partial_usage_is_estimated():
    ledger = CostLedger(prices={"model": {"prompt": 1, "completion": 1}}, env={})

    native = ledger.record(
        "model",
        prompt_text="x" * 400,
        completion_text="y" * 400,
        usage={"input_tokens": 7, "output_tokens": 3},
    )
    partial = ledger.record(
        "model",
        prompt_text="x" * 8,
        completion_text="y" * 12,
        usage={"input_tokens": 5},
    )

    assert (native.prompt_tokens, native.completion_tokens) == (7, 3)
    assert native.estimated is False
    assert (partial.prompt_tokens, partial.completion_tokens) == (5, 3)
    assert partial.estimated is True


def test_environment_then_constructor_price_override_precedence():
    env = {
        "ADVERSARIAL_MODEL_PRICES": (
            '{"model":{"prompt":3,"completion":4},'
            '"env-only":{"prompt":8,"completion":9}}'
        )
    }
    ledger = CostLedger(
        prices={"model": {"prompt": 1}},
        env=env,
    )

    assert ledger.price_for("model") == {"prompt": 1.0, "completion": 4.0}
    assert ledger.price_for("env-only") == {"prompt": 8.0, "completion": 9.0}


def test_dated_model_uses_longest_family_price_and_unknown_is_free():
    ledger = CostLedger(env={})

    assert ledger.price_for("gpt-5-mini-2026-01-01") == {
        "prompt": 0.25,
        "completion": 2.0,
    }
    unknown = ledger.record(
        "not-priced", usage={"input_tokens": 50, "output_tokens": 20}
    )
    assert unknown.est_cost_usd == 0.0


def test_provider_only_model_ids_use_priced_default_families():
    ledger = CostLedger(env={})

    assert ledger.price_for("codex") == ledger.price_for("gpt-5")
    assert ledger.price_for("claude") == ledger.price_for("claude-sonnet-4")


def test_usage_records_and_record_snapshots_are_immutable():
    ledger = CostLedger(env={})
    record = ledger.record(
        "unknown", usage={"input_tokens": 1, "output_tokens": 1}
    )

    with pytest.raises(FrozenInstanceError):
        record.prompt_tokens = 2
    assert isinstance(ledger.records, tuple)


def test_concurrent_updates_do_not_drop_records():
    ledger = CostLedger(
        prices={"model": {"prompt": 1, "completion": 1}},
        env={},
    )

    def add_record(_):
        ledger.record(
            "model",
            usage={"input_tokens": 1, "output_tokens": 1},
            phase="review",
            persona="critic",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_record, range(250)))

    summary = ledger.summary()
    assert len(ledger.records) == 250
    assert summary["total"]["prompt_tokens"] == 250
    assert summary["total"]["completion_tokens"] == 250
    assert summary["total"]["est_cost_usd"] == 0.0005


def test_budget_status_and_stderr_format_use_reconciled_totals(capsys):
    ledger = CostLedger(
        prices={"model": {"prompt": 1, "completion": 2}},
        env={},
    )
    ledger.record(
        "model",
        usage={"input_tokens": 100, "output_tokens": 50},
    )

    allowed = ledger.budget_status(0.0003, model="model", prompt_tokens=50)
    refused = ledger.budget_status(0.0002, model="model", prompt_tokens=1)
    assert allowed["refused"] is False
    assert refused["refused"] is True

    ledger.print_summary()
    output = capsys.readouterr().err
    assert "model: 100 prompt + 50 completion tokens, $0.000200" in output
    assert "total: $0.000200" in output


def test_budget_reservations_are_atomic_and_reconciled():
    ledger = CostLedger(
        prices={"model": {"prompt": 1, "completion": 1}},
        env={},
    )

    allowed, reservation = ledger.reserve_budget(
        0.0001, model="model", prompt_tokens=50, completion_tokens=50
    )
    refused, refused_reservation = ledger.reserve_budget(
        0.0001, model="model", prompt_tokens=1
    )

    assert allowed["refused"] is False
    assert refused["reserved_usd"] == 0.0001
    assert refused["refused"] is True
    assert refused_reservation is None
    ledger.record(
        "model",
        usage={"input_tokens": 50, "output_tokens": 50},
        reservation=reservation,
    )
    assert ledger.total_cost_usd == 0.0001


def test_high_precision_budget_reservation_round_trips_without_rejection():
    ledger = CostLedger(
        prices={
            "model": {"prompt": "123456789.0123456789", "completion": 0}
        },
        env={},
    )
    status, reservation = ledger.reserve_budget(
        200_000_000, model="model", prompt_tokens=1_000_000
    )

    assert status["refused"] is False
    record = ledger.record(
        "model", usage={"input_tokens": 1_000_000, "output_tokens": 0},
        reservation=reservation,
    )
    assert record.est_cost_usd == reservation.reserved_cost_usd


def test_stderr_summary_includes_phase_and_persona_breakdowns():
    ledger = CostLedger(
        prices={"model": {"prompt": 1, "completion": 2}},
        env={},
    )
    ledger.record(
        "model",
        usage={"input_tokens": 100, "output_tokens": 50},
        phase="review",
        persona="critic",
    )

    output = ledger.format_summary()

    assert "By phase:\n  review: 100 prompt + 50 completion tokens" in output
    assert "By persona:\n  critic: 100 prompt + 50 completion tokens" in output
