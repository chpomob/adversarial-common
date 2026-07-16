"""Quota-aware provider selection.

The resolver deliberately executes the quota checker without a shell.  Each
provider's ``quota_check`` value is tokenized independently and appended to one
combined invocation, so a snapshot can be reused by every role in the process.
"""

from __future__ import annotations

import copy
import inspect
import json
import math
import os
import shlex
import subprocess
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Final, Mapping

from .providers import ProviderConfig, ProviderConfigError, ProviderEntry


OK: Final = "OK"
DRAINING: Final = "DRAINING"
RATE_LIMITED: Final = "RATE-LIMITED"
KEY_INVALID: Final = "KEY_INVALID"
UNKNOWN: Final = "UNKNOWN"
PROVIDER_STATES: Final = frozenset(
    {OK, DRAINING, RATE_LIMITED, KEY_INVALID, UNKNOWN}
)

_CHECKER_TIMEOUT_SECONDS: Final = 30.0
_AUTH_STATUS_CODES: Final = frozenset({401, 403})
_AUTH_ERROR_KEYS: Final = frozenset({"auth_error", "authentication_error"})
_STATUS_KEYS: Final = frozenset(
    {"code", "http_code", "http_status", "status", "status_code"}
)
_AUTH_MARKERS: Final = (
    "api key invalid",
    "authentication error",
    "authentication failed",
    "authentication_error",
    "auth error",
    "auth_error",
    "invalid api key",
    "invalid_api_key",
    "key invalid",
    "key_invalid",
    "unauthenticated",
    "unauthorized",
)
_RATE_LIMIT_MARKERS: Final = (
    "http 429",
    "rate limit",
    "rate-limit",
    "rate_limit",
    "status 429",
    "too many requests",
)
_ERROR_CONTEXT_KEYS: Final = frozenset(
    {
        "detail",
        "error",
        "error_description",
        "error_message",
        "errors",
        "message",
        "reason",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderDecision:
    """The immutable result of one provider selection attempt."""

    alias: str | None
    command: str | None
    quota_state: str
    fallback: bool
    reason: str
    raw_snapshot: dict[str, object]
    forced: bool
    error: str | None

    def __post_init__(self) -> None:
        if self.quota_state not in PROVIDER_STATES:
            raise ValueError(f"Unsupported quota state: {self.quota_state}")


class NoProviderAvailable(RuntimeError):
    """Raised when every provider in a role's chain is ineligible."""

    def __init__(
        self,
        role: str,
        snapshots: Mapping[str, object],
        reasons: Mapping[str, str],
    ) -> None:
        self.role = role
        self.snapshots = copy.deepcopy(dict(snapshots))
        self.reasons = dict(reasons)
        detail = "; ".join(
            f"{alias}: {reason}" for alias, reason in self.reasons.items()
        )
        message = f"No provider available for role '{role}'"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _CacheRecord:
    created_at: float
    snapshot: dict[str, object]
    error: str | None


@dataclass(frozen=True, slots=True)
class _QuotaReading:
    state: str
    kind: str | None
    metric: int | float | None
    warning: str | None = None


# Quota data is process-scoped rather than resolver-scoped.  The key includes
# the executable and alias/argument mapping so unrelated registries cannot
# consume one another's snapshots.
_SNAPSHOT_CACHE: dict[tuple[object, ...], _CacheRecord] = {}
_CACHE_LOCK = threading.RLock()
_CACHE_KEY_LOCKS: dict[tuple[object, ...], threading.Lock] = {}


class QuotaResolver:
    """Resolve ordered provider chains against a cached quota snapshot."""

    def __init__(
        self,
        config: ProviderConfig,
        checker_path: str | os.PathLike[str],
    ) -> None:
        if not isinstance(config, ProviderConfig):
            raise TypeError("config must be a ProviderConfig")
        try:
            checker = os.fspath(checker_path)
        except TypeError as exc:
            raise TypeError("checker_path must be a path string") from exc
        if not isinstance(checker, str) or not checker.strip():
            raise ValueError("checker_path must be a non-empty path string")

        self._config = config
        self._checker_path = os.path.expanduser(checker)
        self._providers = _configured_providers(config)
        self._cache_key = (
            os.path.abspath(os.path.expanduser(checker)),
            self._providers,
        )
        self.history: list[ProviderDecision] = []
        self._history_lock = threading.Lock()

    def resolve(
        self,
        role: str,
        *,
        workdir: str,
        force: bool = False,
        force_provider: str | None = None,
    ) -> ProviderDecision:
        """Select a command for *role* and record the attempt in history."""
        chain = self._chain_for(role)
        normalized_workdir = _normalize_workdir(workdir)

        if force_provider is not None:
            return self._resolve_forced_provider(
                role, chain, force_provider, normalized_workdir
            )
        if force:
            return self._record(
                _decision_for_entry(
                    chain[0],
                    normalized_workdir,
                    quota_state=UNKNOWN,
                    fallback=False,
                    reason="forced primary provider",
                    raw_snapshot={},
                    forced=True,
                )
            )

        record = self._quota_snapshot()
        if record.error is not None:
            return self._record(
                _decision_for_entry(
                    chain[0],
                    normalized_workdir,
                    quota_state=UNKNOWN,
                    fallback=True,
                    reason="quota checker failed; selected primary provider",
                    raw_snapshot=record.snapshot,
                    forced=False,
                    error=record.error,
                )
            )

        reasons: dict[str, str] = {}
        for index, entry in enumerate(chain):
            raw = record.snapshot.get(entry.alias, {})
            reading = _normalize_quota(
                raw,
                entry.alias,
                warn_on_missing=entry.quota_check is not None,
            )
            if reading.warning is not None:
                warnings.warn(reading.warning, RuntimeWarning, stacklevel=2)

            rejection = _rejection_reason(entry, reading)
            if rejection is not None:
                reasons[entry.alias] = rejection
                continue

            if reading.state == UNKNOWN:
                reason = "selected provider with unknown quota state"
            elif index:
                reason = "selected fallback provider after earlier rejection"
            else:
                reason = "selected first eligible provider"
            return self._record(
                _decision_for_entry(
                    entry,
                    normalized_workdir,
                    quota_state=reading.state,
                    fallback=index > 0,
                    reason=reason,
                    raw_snapshot=record.snapshot,
                    forced=False,
                )
            )

        error = NoProviderAvailable(role, record.snapshot, reasons)
        self._record(
            ProviderDecision(
                alias=None,
                command=None,
                quota_state=UNKNOWN,
                fallback=False,
                reason="no provider available",
                raw_snapshot=copy.deepcopy(record.snapshot),
                forced=False,
                error=str(error),
            )
        )
        raise error

    def _chain_for(self, role: str) -> tuple[ProviderEntry, ...]:
        try:
            return self._config.chain_for(role)
        except KeyError as exc:
            raise ProviderConfigError(
                "PROVIDER_ROLE_NOT_CONFIGURED",
                f"role '{role}' is not configured",
            ) from exc

    def _resolve_forced_provider(
        self,
        role: str,
        chain: tuple[ProviderEntry, ...],
        alias: str,
        workdir: str,
    ) -> ProviderDecision:
        if not isinstance(alias, str) or not alias.strip():
            detail = "force_provider must be a non-empty alias"
            self._record_failed_force(detail)
            raise ProviderConfigError("PROVIDER_FORCE_INVALID_ALIAS", detail)
        for entry in chain:
            if entry.alias == alias:
                return self._record(
                    _decision_for_entry(
                        entry,
                        workdir,
                        quota_state=UNKNOWN,
                        fallback=False,
                        reason="forced requested provider",
                        raw_snapshot={},
                        forced=True,
                    )
                )
        detail = f"provider '{alias}' is not configured for role '{role}'"
        self._record_failed_force(detail)
        raise ProviderConfigError("PROVIDER_FORCE_NOT_IN_ROLE", detail)

    def _record_failed_force(self, detail: str) -> None:
        self._record(
            ProviderDecision(
                alias=None,
                command=None,
                quota_state=UNKNOWN,
                fallback=False,
                reason="forced provider selection failed",
                raw_snapshot={},
                forced=True,
                error=detail,
            )
        )

    def _quota_snapshot(self) -> _CacheRecord:
        ttl = float(self._config.quota_cache_ttl)
        with _CACHE_LOCK:
            now = time.monotonic()
            cached = _SNAPSHOT_CACHE.get(self._cache_key)
            if cached is not None and 0 <= now - cached.created_at < ttl:
                return cached
            key_lock = _CACHE_KEY_LOCKS.setdefault(
                self._cache_key, threading.Lock()
            )

        # Checker execution is serialized only with other resolvers using the
        # same cache key.  A slow checker must not block unrelated registries.
        with key_lock:
            with _CACHE_LOCK:
                now = time.monotonic()
                cached = _SNAPSHOT_CACHE.get(self._cache_key)
                if cached is not None and 0 <= now - cached.created_at < ttl:
                    return cached

            if not any(check is not None for _alias, check in self._providers):
                record = _CacheRecord(
                    now,
                    {alias: {} for alias, _check in self._providers},
                    None,
                )
            else:
                record = self._run_checker()
            with _CACHE_LOCK:
                _SNAPSHOT_CACHE[self._cache_key] = record
            return record

    def _run_checker(self) -> _CacheRecord:
        command = [self._checker_path]
        try:
            for _alias, quota_check in self._providers:
                if quota_check is None:
                    continue
                command.extend(shlex.split(quota_check))
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_CHECKER_TIMEOUT_SECONDS,
                check=False,
            )
            try:
                payload = json.loads(
                    completed.stdout,
                    parse_constant=_reject_non_json_number,
                )
            except (json.JSONDecodeError, TypeError) as exc:
                if completed.returncode:
                    detail = (
                        "quota checker exited with status "
                        f"{completed.returncode} and returned invalid JSON"
                    )
                else:
                    detail = "quota checker returned invalid JSON"
                raise _CheckerFailure(detail) from exc
            snapshot = _snapshot_from_payload(payload, self._providers)
            return _CacheRecord(time.monotonic(), snapshot, None)
        except FileNotFoundError:
            return self._checker_failure("quota checker executable not found")
        except subprocess.TimeoutExpired:
            return self._checker_failure("quota checker timed out")
        except PermissionError:
            return self._checker_failure("quota checker is not executable")
        except (OSError, ValueError, _CheckerFailure) as exc:
            detail = str(exc) or "quota checker execution failed"
            return self._checker_failure(detail)

    def _checker_failure(self, detail: str) -> _CacheRecord:
        _warn_external_caller(detail)
        snapshot = {alias: {} for alias, _check in self._providers}
        return _CacheRecord(time.monotonic(), snapshot, detail)

    def _record(self, decision: ProviderDecision) -> ProviderDecision:
        with self._history_lock:
            self.history.append(decision)
        return decision


class _CheckerFailure(RuntimeError):
    pass


def _configured_providers(
    config: ProviderConfig,
) -> tuple[tuple[str, str | None], ...]:
    checks: dict[str, str | None] = {}
    for chain in config.roles.values():
        for entry in chain:
            previous = checks.get(entry.alias)
            if (
                previous is not None
                and entry.quota_check is not None
                and previous != entry.quota_check
            ):
                raise ProviderConfigError(
                    "PROVIDER_CONFIG_CONFLICTING_QUOTA_CHECK",
                    f"provider '{entry.alias}' has conflicting quota_check values",
                )
            if entry.alias not in checks or previous is None:
                checks[entry.alias] = entry.quota_check
    return tuple(checks.items())


def _snapshot_from_payload(
    payload: object,
    providers: tuple[tuple[str, str | None], ...],
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise _CheckerFailure("quota checker JSON root must be an object")

    results = payload.get("results", payload)
    by_alias: Mapping[str, object]
    if isinstance(results, dict):
        if "results" not in payload and not any(
            alias in results for alias, _check in providers
        ) and _looks_like_quota_reading(results):
            checked_aliases = [
                alias for alias, check in providers if check is not None
            ]
            if len(checked_aliases) != 1:
                raise _CheckerFailure(
                    "flat quota checker results are ambiguous for multiple "
                    "providers; results must be keyed by provider alias"
                )
            by_alias = {checked_aliases[0]: results}
        else:
            by_alias = results
    elif isinstance(results, list):
        collected: dict[str, object] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            alias = item.get("alias", item.get("provider"))
            if isinstance(alias, str):
                collected[alias] = item
        by_alias = collected
    else:
        raise _CheckerFailure("quota checker results must be an object or array")

    return {
        alias: copy.deepcopy(by_alias.get(alias, {}))
        for alias, _check in providers
    }


def _reject_non_json_number(value: str) -> object:
    raise ValueError(f"invalid JSON number: {value}")


def _looks_like_quota_reading(value: Mapping[str, object]) -> bool:
    reading_keys = {
        "auth_error",
        "authentication_error",
        "balance",
        "error",
        "http_code",
        "http_status",
        "session",
        "status",
        "status_code",
        "used_pct",
    }
    return bool(reading_keys.intersection(value))


def _normalize_quota(
    raw: object,
    alias: str,
    *,
    warn_on_missing: bool = True,
) -> _QuotaReading:
    if not isinstance(raw, dict) or not raw:
        return _QuotaReading(
            UNKNOWN,
            None,
            None,
            (
                f"quota data for provider '{alias}' is missing or unrecognized"
                if warn_on_missing
                else None
            ),
        )

    signal = _error_signal(raw)
    if signal is not None:
        return _QuotaReading(signal, None, None)

    session = raw.get("session")
    if isinstance(session, dict) and "used_pct" in session:
        metric = _runtime_number(session.get("used_pct"))
        if metric is None or metric < 0:
            return _QuotaReading(
                UNKNOWN,
                "percentage",
                None,
                f"quota used_pct for provider '{alias}' is missing or non-numeric",
            )
        if metric >= 100:
            state = RATE_LIMITED
        elif metric >= 50:
            state = DRAINING
        else:
            state = OK
        return _QuotaReading(state, "percentage", metric)

    if "used_pct" in raw:
        metric = _runtime_number(raw.get("used_pct"))
        if metric is None or metric < 0:
            return _QuotaReading(
                UNKNOWN,
                "percentage",
                None,
                f"quota used_pct for provider '{alias}' is missing or non-numeric",
            )
        if metric >= 100:
            state = RATE_LIMITED
        elif metric >= 50:
            state = DRAINING
        else:
            state = OK
        return _QuotaReading(state, "percentage", metric)

    if "balance" in raw:
        metric = _runtime_number(raw.get("balance"))
        if metric is None:
            return _QuotaReading(
                UNKNOWN,
                "balance",
                None,
                f"quota balance for provider '{alias}' is missing or non-numeric",
            )
        # A bare balance schema has no unit-independent definition of "low".
        # Positive values therefore remain OK unless the configured stop
        # threshold blocks them; zero and negative values are exhausted.
        state = RATE_LIMITED if metric <= 0 else OK
        return _QuotaReading(state, "balance", metric)

    return _QuotaReading(
        UNKNOWN,
        None,
        None,
        f"quota data for provider '{alias}' is missing or unrecognized",
    )


def _error_signal(value: object) -> str | None:
    """Return an error state from explicit status and error fields only."""
    if not isinstance(value, dict):
        return None

    auth_found = False
    rate_found = False
    stack: list[tuple[object, bool]] = [(value, False)]
    while stack:
        current, in_error_context = stack.pop()
        if isinstance(current, dict):
            for key, nested in current.items():
                normalized_key = str(key).lower()
                inspect_field = in_error_context or normalized_key in (
                    _STATUS_KEYS | _AUTH_ERROR_KEYS | _ERROR_CONTEXT_KEYS
                )
                if inspect_field and normalized_key in _STATUS_KEYS:
                    status = _runtime_number(nested)
                    if isinstance(nested, str) and nested.strip() in {
                        "401",
                        "403",
                        "429",
                    }:
                        status = int(nested.strip())
                    if status in _AUTH_STATUS_CODES:
                        auth_found = True
                    elif status == 429:
                        rate_found = True
                if inspect_field and normalized_key in _AUTH_ERROR_KEYS:
                    if nested is True:
                        auth_found = True
                if inspect_field:
                    stack.append((nested, True))
        elif isinstance(current, (list, tuple)):
            if in_error_context:
                stack.extend((item, True) for item in current)
        elif in_error_context and isinstance(current, str):
            lowered = current.lower()
            if any(marker in lowered for marker in _AUTH_MARKERS):
                auth_found = True
            if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
                rate_found = True
            if lowered.strip() == "429":
                rate_found = True
    if auth_found:
        return KEY_INVALID
    if rate_found:
        return RATE_LIMITED
    return None


def _warn_external_caller(detail: str) -> None:
    """Warn at the first frame outside this module, regardless of call depth."""
    frame = inspect.currentframe()
    stacklevel = 1
    try:
        while frame is not None and frame.f_globals.get("__name__") == __name__:
            stacklevel += 1
            frame = frame.f_back
    finally:
        del frame
    warnings.warn(detail, RuntimeWarning, stacklevel=stacklevel)


def _rejection_reason(
    entry: ProviderEntry, reading: _QuotaReading
) -> str | None:
    if reading.state == RATE_LIMITED:
        return "quota state RATE-LIMITED"
    if reading.state == KEY_INVALID:
        return "quota state KEY_INVALID"
    threshold = entry.stop_threshold
    if threshold is None or reading.metric is None:
        return None
    if reading.kind == "percentage" and reading.metric > threshold:
        return (
            f"used_pct {_format_number(reading.metric)} exceeds stop threshold "
            f"{_format_number(threshold)}"
        )
    if reading.kind == "balance" and reading.metric < threshold:
        return (
            f"balance {_format_number(reading.metric)} is below stop threshold "
            f"{_format_number(threshold)}"
        )
    return None


def _runtime_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return None
    if not finite:
        return None
    return value


def _format_number(value: int | float) -> str:
    return format(value, "g")


def _normalize_workdir(workdir: str) -> str:
    try:
        value = os.fspath(workdir)
    except TypeError as exc:
        raise TypeError("workdir must be a path string") from exc
    if not isinstance(value, str) or not value:
        raise ValueError("workdir must be a non-empty path string")
    return os.path.abspath(os.path.normpath(os.path.expanduser(value)))


def _decision_for_entry(
    entry: ProviderEntry,
    workdir: str,
    *,
    quota_state: str,
    fallback: bool,
    reason: str,
    raw_snapshot: Mapping[str, object],
    forced: bool,
    error: str | None = None,
) -> ProviderDecision:
    return ProviderDecision(
        alias=entry.alias,
        command=entry.command.replace("{workdir}", workdir),
        quota_state=quota_state,
        fallback=fallback,
        reason=reason,
        raw_snapshot=copy.deepcopy(dict(raw_snapshot)),
        forced=forced,
        error=error,
    )


__all__ = [
    "DRAINING",
    "KEY_INVALID",
    "NoProviderAvailable",
    "OK",
    "PROVIDER_STATES",
    "ProviderDecision",
    "QuotaResolver",
    "RATE_LIMITED",
    "UNKNOWN",
]
