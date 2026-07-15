"""Thread-safe token and estimated-cost accounting for adversarial runs."""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Final, TextIO


# USD per million tokens. Unknown models remain trackable at a zero price.
MODEL_PRICES: Final[dict[str, dict[str, float]]] = {
    "claude-opus-4": {"prompt": 15.0, "completion": 75.0},
    "claude-sonnet-4": {"prompt": 3.0, "completion": 15.0},
    "claude-haiku-3.5": {"prompt": 0.8, "completion": 4.0},
    "gpt-5": {"prompt": 1.25, "completion": 10.0},
    "gpt-5-mini": {"prompt": 0.25, "completion": 2.0},
}


@dataclass(frozen=True)
class UsageRecord:
    """Usage attributable to one provider attempt or completed phase."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    est_cost_usd: float
    estimated: bool
    phase: str = ""
    persona: str = ""


def estimate_tokens(text: str | None) -> int:
    """Return the deterministic char/4 fallback token count."""
    return (len(text or "") + 3) // 4


class CostLedger:
    """Accumulate usage by model, phase, and persona across a whole run.

    ``prices`` and ``ADVERSARIAL_MODEL_PRICES`` accept mappings whose values
    contain ``prompt``/``completion`` USD-per-million prices. Explicit
    constructor overrides win over the environment.
    """

    def __init__(
        self,
        prices: Mapping[str, Mapping[str, float]] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._records: list[UsageRecord] = []
        self._prices = {model: dict(price) for model, price in MODEL_PRICES.items()}
        environment = os.environ if env is None else env
        raw_overrides = environment.get("ADVERSARIAL_MODEL_PRICES", "").strip()
        if raw_overrides:
            try:
                parsed = json.loads(raw_overrides)
            except json.JSONDecodeError as exc:
                raise ValueError("ADVERSARIAL_MODEL_PRICES must be valid JSON") from exc
            self._merge_prices(parsed)
        if prices:
            self._merge_prices(prices)

    def _merge_prices(self, overrides: Mapping[str, Mapping[str, float]]) -> None:
        if not isinstance(overrides, Mapping):
            raise TypeError("price overrides must be a mapping")
        for model, price in overrides.items():
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model ids in price overrides must be non-empty strings")
            if not isinstance(price, Mapping):
                raise TypeError(f"price override for {model!r} must be a mapping")
            prompt = _price_value(model, "prompt", price.get("prompt", 0.0))
            completion = _price_value(
                model, "completion", price.get("completion", 0.0)
            )
            self._prices[model] = {"prompt": prompt, "completion": completion}

    def record(
        self,
        model: str,
        *,
        prompt_text: str | None = None,
        completion_text: str | None = None,
        usage: Mapping[str, Any] | None = None,
        phase: str = "",
        persona: str = "",
    ) -> UsageRecord:
        """Record native usage when complete, otherwise use char/4 estimates."""
        model_id = (model or "unknown").strip() or "unknown"
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
        price = self._prices.get(model_id, {"prompt": 0.0, "completion": 0.0})
        cost = (
            prompt_tokens * price["prompt"]
            + completion_tokens * price["completion"]
        ) / 1_000_000
        record = UsageRecord(
            model=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            est_cost_usd=round(cost, 10),
            estimated=estimated,
            phase=phase,
            persona=persona,
        )
        with self._lock:
            self._records.append(record)
        return record

    add = record

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serializable per-model breakdown and reconciled total."""
        with self._lock:
            records = list(self._records)
        models: dict[str, dict[str, Any]] = {}
        for record in records:
            item = models.setdefault(
                record.model,
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "est_cost_usd": 0.0,
                    "estimated": False,
                },
            )
            item["prompt_tokens"] += record.prompt_tokens
            item["completion_tokens"] += record.completion_tokens
            item["est_cost_usd"] += record.est_cost_usd
            item["estimated"] = item["estimated"] or record.estimated
        for item in models.values():
            item["est_cost_usd"] = round(item["est_cost_usd"], 10)
        total = {
            "prompt_tokens": sum(item["prompt_tokens"] for item in models.values()),
            "completion_tokens": sum(
                item["completion_tokens"] for item in models.values()
            ),
            "est_cost_usd": round(
                sum(item["est_cost_usd"] for item in models.values()), 10
            ),
        }
        return {
            "models": models,
            "total": total,
            "records": [asdict(record) for record in records],
        }

    def format_summary(self) -> str:
        """Format the same per-model data used by ``summary`` for stderr."""
        summary = self.summary()
        lines = ["Estimated model costs:"]
        for model, usage in sorted(summary["models"].items()):
            marker = " estimated" if usage["estimated"] else ""
            lines.append(
                f"  {model}: {usage['prompt_tokens']} prompt + "
                f"{usage['completion_tokens']} completion tokens, "
                f"${usage['est_cost_usd']:.6f}{marker}"
            )
        lines.append(f"  total: ${summary['total']['est_cost_usd']:.6f}")
        return "\n".join(lines)

    def print_summary(self, file: TextIO | None = None) -> None:
        print(self.format_summary(), file=file or sys.stderr)


def _usage_count(
    usage: Mapping[str, Any] | None, primary: str, alternate: str
) -> int | None:
    if not isinstance(usage, Mapping):
        return None
    value = usage.get(primary, usage.get(alternate))
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _price_value(model: str, kind: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{kind} price for {model!r} must be non-negative")
    return float(value)


__all__ = ["CostLedger", "MODEL_PRICES", "UsageRecord", "estimate_tokens"]
