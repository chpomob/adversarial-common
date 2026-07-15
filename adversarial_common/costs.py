"""Thread-safe token and estimated-cost accounting for adversarial runs."""

from __future__ import annotations

import json
import math
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, TextIO


_MILLION: Final = Decimal(1_000_000)
_COST_PRECISION: Final = Decimal("0.0000000001")

# USD per million tokens. Dated model ids inherit the longest matching family
# prefix. Unknown models remain trackable at a zero price.
MODEL_PRICES: Final[dict[str, dict[str, float]]] = {
    "claude-opus-4": {"prompt": 15.0, "completion": 75.0},
    "claude-sonnet-4": {"prompt": 3.0, "completion": 15.0},
    "claude-haiku-3.5": {"prompt": 0.8, "completion": 4.0},
    "claude-3-5-haiku": {"prompt": 0.8, "completion": 4.0},
    "gpt-5": {"prompt": 1.25, "completion": 10.0},
    "gpt-5-mini": {"prompt": 0.25, "completion": 2.0},
}

# Provider commands intentionally omit a model when the provider's own default
# should be used. Attribute those calls to a documented priced family instead
# of silently treating the provider executable name as an unknown free model.
PROVIDER_PRICE_ALIASES: Final[dict[str, str]] = {
    "codex": "gpt-5",
    "claude": "claude-sonnet-4",
    "claude-tmux": "claude-sonnet-4",
}


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Immutable usage attributable to one provider attempt."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    est_cost_usd: float
    estimated: bool
    phase: str = ""
    persona: str = ""

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")
        for name in ("prompt_tokens", "completion_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not math.isfinite(self.est_cost_usd) or self.est_cost_usd < 0:
            raise ValueError("est_cost_usd must be a finite non-negative number")


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Opaque reservation for one provider attempt's projected cost."""

    reservation_id: int
    model: str
    reserved_cost_usd: float


def estimate_tokens(text: str | None) -> int:
    """Return the deterministic, ceiling-rounded char/4 fallback count."""
    if text is not None and not isinstance(text, str):
        raise TypeError("text must be a string or None")
    return (len(text or "") + 3) // 4


class CostLedger:
    """Accumulate usage by model, phase, and persona across a whole run.

    prices and ADVERSARIAL_MODEL_PRICES accept mappings whose values contain
    prompt/completion USD-per-million prices. Constructor overrides (the
    CLI/flag layer) take precedence over environment values. All mutation and
    budget reads are protected because parallel personas share one ledger.
    """

    def __init__(
        self,
        prices: Mapping[str, Mapping[str, float]] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._records: list[UsageRecord] = []
        self._reservations: dict[int, Decimal] = {}
        self._next_reservation_id = 1
        self._prices: dict[str, dict[str, Decimal]] = {
            model: {
                "prompt": Decimal(str(price["prompt"])),
                "completion": Decimal(str(price["completion"])),
            }
            for model, price in MODEL_PRICES.items()
        }
        environment = os.environ if env is None else env
        raw_overrides = environment.get("ADVERSARIAL_MODEL_PRICES", "").strip()
        if raw_overrides:
            try:
                parsed = json.loads(raw_overrides)
            except json.JSONDecodeError as exc:
                raise ValueError("ADVERSARIAL_MODEL_PRICES must be valid JSON") from exc
            self._merge_prices(parsed)
        if prices is not None:
            self._merge_prices(prices)

    @property
    def records(self) -> tuple[UsageRecord, ...]:
        """Return an immutable snapshot of records in insertion order."""
        with self._lock:
            return tuple(self._records)

    @property
    def total_cost_usd(self) -> float:
        """Return the reconciled cost of all records."""
        with self._lock:
            return _decimal_to_float(self._total_cost_locked())

    def _merge_prices(self, overrides: Mapping[str, Mapping[str, float]]) -> None:
        if not isinstance(overrides, Mapping):
            raise TypeError("price overrides must be a mapping")
        for raw_model, price in overrides.items():
            if not isinstance(raw_model, str) or not raw_model.strip():
                raise ValueError("model ids in price overrides must be non-empty strings")
            model = raw_model.strip()
            if not isinstance(price, Mapping):
                raise TypeError(f"price override for {model!r} must be a mapping")
            if not price or any(key not in {"prompt", "completion"} for key in price):
                raise ValueError(
                    f"price override for {model!r} must contain prompt and/or completion"
                )
            previous = self._prices.get(
                model, {"prompt": Decimal(0), "completion": Decimal(0)}
            )
            self._prices[model] = {
                "prompt": _price_value(
                    model, "prompt", price.get("prompt", previous["prompt"])
                ),
                "completion": _price_value(
                    model,
                    "completion",
                    price.get("completion", previous["completion"]),
                ),
            }

    def price_for(self, model: str | None) -> dict[str, float]:
        """Return effective prompt/completion prices for a model id."""
        model_id = _model_id(model)
        with self._lock:
            price = self._price_for_locked(model_id)
            return {kind: float(value) for kind, value in price.items()}

    def estimate_cost(
        self,
        model: str | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> float:
        """Estimate a call cost without mutating the ledger."""
        _validate_token_count("prompt_tokens", prompt_tokens)
        _validate_token_count("completion_tokens", completion_tokens)
        model_id = _model_id(model)
        with self._lock:
            cost = self._calculate_cost_locked(
                model_id, prompt_tokens, completion_tokens
            )
        return _decimal_to_float(cost)

    def budget_status(
        self,
        budget_usd: float,
        *,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> dict[str, float | bool]:
        """Return an atomic snapshot used to decide whether to start a call."""
        budget = _money_value("budget_usd", budget_usd)
        _validate_token_count("prompt_tokens", prompt_tokens)
        _validate_token_count("completion_tokens", completion_tokens)
        model_id = _model_id(model)
        with self._lock:
            spent = self._total_cost_locked()
            reserved = self._reserved_cost_locked()
            projected_call = self._calculate_cost_locked(
                model_id, prompt_tokens, completion_tokens
            )
            return self._budget_status_locked(
                budget, spent, reserved, projected_call
            )

    def reserve_budget(
        self,
        budget_usd: float,
        *,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> tuple[dict[str, float | bool], BudgetReservation | None]:
        """Atomically admit and reserve projected spend for one attempt."""
        budget = _money_value("budget_usd", budget_usd)
        _validate_token_count("prompt_tokens", prompt_tokens)
        _validate_token_count("completion_tokens", completion_tokens)
        model_id = _model_id(model)
        with self._lock:
            spent = self._total_cost_locked()
            reserved = self._reserved_cost_locked()
            projected_call = self._calculate_cost_locked(
                model_id, prompt_tokens, completion_tokens
            )
            status = self._budget_status_locked(
                budget, spent, reserved, projected_call
            )
            if status["refused"]:
                return status, None
            reservation_id = self._next_reservation_id
            self._next_reservation_id += 1
            self._reservations[reservation_id] = projected_call
            return status, BudgetReservation(
                reservation_id=reservation_id,
                model=model_id,
                reserved_cost_usd=_decimal_to_float(projected_call),
            )

    def release_budget(self, reservation: BudgetReservation) -> None:
        """Release an admitted attempt that did not reach the provider."""
        with self._lock:
            self._consume_reservation_locked(reservation)

    def would_exceed(
        self,
        budget_usd: float,
        *,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> bool:
        """Return whether current plus projected usage exceeds budget_usd."""
        status = self.budget_status(
            budget_usd,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return bool(status["refused"])

    def record(
        self,
        model: str | None,
        *,
        prompt_text: str | None = None,
        completion_text: str | None = None,
        usage: Mapping[str, Any] | None = None,
        phase: str = "",
        persona: str = "",
        reservation: BudgetReservation | None = None,
    ) -> UsageRecord:
        """Record native usage when complete, otherwise use char/4 estimates."""
        if prompt_text is not None and not isinstance(prompt_text, str):
            raise TypeError("prompt_text must be a string or None")
        if completion_text is not None and not isinstance(completion_text, str):
            raise TypeError("completion_text must be a string or None")
        if not isinstance(phase, str) or not isinstance(persona, str):
            raise TypeError("phase and persona must be strings")
        model_id = _model_id(model)
        prompt_native = _usage_count(usage, "prompt_tokens", "input_tokens")
        completion_native = _usage_count(
            usage, "completion_tokens", "output_tokens"
        )
        estimated = prompt_native is None or completion_native is None
        prompt_tokens = (
            estimate_tokens(prompt_text) if prompt_native is None else prompt_native
        )
        completion_tokens = (
            estimate_tokens(completion_text)
            if completion_native is None
            else completion_native
        )
        with self._lock:
            if reservation is not None:
                if reservation.model != model_id:
                    raise ValueError("budget reservation model does not match record")
                self._consume_reservation_locked(reservation)
            cost = self._calculate_cost_locked(
                model_id, prompt_tokens, completion_tokens
            )
            record = UsageRecord(
                model=model_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                est_cost_usd=_decimal_to_float(cost),
                estimated=estimated,
                phase=phase,
                persona=persona,
            )
            self._records.append(record)
        return record

    add = record

    def summary(self) -> dict[str, Any]:
        """Return serializable per-model/dimension breakdowns and one total."""
        records = self.records
        models = _aggregate(records, lambda record: record.model)
        phases = _aggregate(records, lambda record: record.phase, omit_empty=True)
        personas = _aggregate(records, lambda record: record.persona, omit_empty=True)
        total = _usage_totals(records)
        # Preserve the original total contract: estimation provenance belongs
        # to records and breakdowns, while the total has only numeric fields.
        total.pop("estimated")
        return {
            "models": models,
            "phases": phases,
            "personas": personas,
            "total": total,
            "records": [asdict(record) for record in records],
        }

    def format_summary(self) -> str:
        """Format model, phase, and persona breakdowns from summary()."""
        summary = self.summary()
        lines = ["Estimated model costs:"]
        self._append_breakdown(lines, summary["models"])
        if summary["phases"]:
            lines.append("By phase:")
            self._append_breakdown(lines, summary["phases"])
        if summary["personas"]:
            lines.append("By persona:")
            self._append_breakdown(lines, summary["personas"])
        lines.append(f"  total: ${summary['total']['est_cost_usd']:.6f}")
        return "\n".join(lines)

    def print_summary(self, file: TextIO | None = None) -> None:
        """Print all cost breakdowns to stderr (or an explicit text stream)."""
        print(self.format_summary(), file=file or sys.stderr)

    @staticmethod
    def _append_breakdown(lines, breakdown) -> None:
        for label, usage in sorted(breakdown.items()):
            marker = " estimated" if usage["estimated"] else ""
            lines.append(
                f"  {label}: {usage['prompt_tokens']} prompt + "
                f"{usage['completion_tokens']} completion tokens, "
                f"${usage['est_cost_usd']:.6f}{marker}"
            )

    def _budget_status_locked(
        self,
        budget: Decimal,
        spent: Decimal,
        reserved: Decimal,
        projected_call: Decimal,
    ) -> dict[str, float | bool]:
        projected_total = spent + reserved + projected_call
        return {
            "limit_usd": _decimal_to_float(budget),
            "spent_usd": _decimal_to_float(spent),
            "reserved_usd": _decimal_to_float(reserved),
            "projected_call_usd": _decimal_to_float(projected_call),
            "projected_total_usd": _decimal_to_float(projected_total),
            "refused": projected_total > budget,
        }

    def _consume_reservation_locked(self, reservation: BudgetReservation) -> None:
        if not isinstance(reservation, BudgetReservation):
            raise TypeError("reservation must be a BudgetReservation")
        expected = self._reservations.get(reservation.reservation_id)
        if expected is None:
            raise ValueError("budget reservation is unknown or already consumed")
        if _decimal_to_float(expected) != reservation.reserved_cost_usd:
            raise ValueError("budget reservation does not match ledger state")
        del self._reservations[reservation.reservation_id]

    def _price_for_locked(self, model: str) -> dict[str, Decimal]:
        exact = self._prices.get(model)
        if exact is not None:
            return exact
        alias = PROVIDER_PRICE_ALIASES.get(model)
        if alias is not None and alias in self._prices:
            return self._prices[alias]
        matches = [
            family for family in self._prices if model.startswith(f"{family}-")
        ]
        if not matches:
            return {"prompt": Decimal(0), "completion": Decimal(0)}
        return self._prices[max(matches, key=len)]

    def _calculate_cost_locked(
        self, model: str, prompt_tokens: int, completion_tokens: int
    ) -> Decimal:
        price = self._price_for_locked(model)
        cost = (
            Decimal(prompt_tokens) * price["prompt"]
            + Decimal(completion_tokens) * price["completion"]
        ) / _MILLION
        return cost.quantize(_COST_PRECISION)

    def _total_cost_locked(self) -> Decimal:
        return sum(
            (Decimal(str(record.est_cost_usd)) for record in self._records),
            start=Decimal(0),
        ).quantize(_COST_PRECISION)

    def _reserved_cost_locked(self) -> Decimal:
        return sum(
            self._reservations.values(), start=Decimal(0)
        ).quantize(_COST_PRECISION)


def _aggregate(records, key_function, *, omit_empty=False):
    grouped: dict[str, list[UsageRecord]] = {}
    for record in records:
        key = key_function(record)
        if omit_empty and not key:
            continue
        grouped.setdefault(key, []).append(record)
    return {key: _usage_totals(items) for key, items in grouped.items()}


def _usage_totals(records) -> dict[str, Any]:
    prompt_tokens = sum(record.prompt_tokens for record in records)
    completion_tokens = sum(record.completion_tokens for record in records)
    cost = sum(
        (Decimal(str(record.est_cost_usd)) for record in records),
        start=Decimal(0),
    ).quantize(_COST_PRECISION)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "est_cost_usd": _decimal_to_float(cost),
        "estimated": any(record.estimated for record in records),
    }


def _usage_count(
    usage: Mapping[str, Any] | None, primary: str, alternate: str
) -> int | None:
    if not isinstance(usage, Mapping):
        return None
    value = usage.get(primary, usage.get(alternate))
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _model_id(model: str | None) -> str:
    if model is not None and not isinstance(model, str):
        raise TypeError("model must be a string or None")
    return (model or "unknown").strip() or "unknown"


def _validate_token_count(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _price_value(model: str, kind: str, value: Any) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{kind} price for {model!r} must be numeric") from exc
    if isinstance(value, bool) or not price.is_finite() or price < 0:
        raise ValueError(f"{kind} price for {model!r} must be finite and non-negative")
    return price


def _money_value(name: str, value: Any) -> Decimal:
    try:
        money = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if isinstance(value, bool) or not money.is_finite() or money < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return money


def _decimal_to_float(value: Decimal) -> float:
    return float(value.quantize(_COST_PRECISION))


__all__ = [
    "BudgetReservation", "CostLedger", "MODEL_PRICES", "PROVIDER_PRICE_ALIASES",
    "UsageRecord",
    "estimate_tokens",
]
