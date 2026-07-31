"""Hardened subprocess execution and bounded parallel dispatch."""

from __future__ import annotations

import copy
import json
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict
from typing import Any, Protocol

from . import costs, gates, providers, quota


DEFAULT_MAX_INPUT_CHARS = 256 * 1024
DEFAULT_MAX_OUTPUT_CHARS = 128 * 1024
# Stable process exit codes for opt-in CI mode.  These values are a public
# contract shared by all pipeline entry points; do not derive them from a
# provider's return code.
CI_EXIT_CLEAN = 0
CI_EXIT_INFRASTRUCTURE = 1
CI_EXIT_BLOCKING = 2
CI_EXIT_NON_BLOCKING = 3
CI_EXIT_CONTEXT_BLOCKED = 5

DEFAULT_RESEARCH_MAX_QUERIES = 5
DEFAULT_RESEARCH_MAX_RESULTS = 5
DEFAULT_RESEARCH_TIMEOUT = 60
DEFAULT_RESEARCH_MAX_INPUT_CHARS = 16 * 1024
DEFAULT_RESEARCH_MAX_OUTPUT_CHARS = 32 * 1024

_ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1B\[[0-?]*[ -/]*[@-~]|\x1B\][^\x07]*(?:\x07|\x1B\\))"
)
_CI_PASS_VERDICTS = frozenset({
    "accept", "accepted", "approve", "approved", "clean", "ok", "pass",
    "passed", "resolved", "success", "succeeded",
})
_CI_BLOCKING_VERDICTS = frozenset({
    "block", "blocked", "blocking", "fail", "failed", "reject", "rejected",
    "request_changes", "requested_changes",
})
_CI_INFRA_VERDICTS = frozenset({
    "error", "infra", "infra_error", "infrastructure",
    "infrastructure_error", "unavailable",
})
_CI_CONTEXT_VERDICTS = frozenset({
    "context_block", "context_blocked", "insufficient_context",
})
_CI_NON_BLOCKING_VERDICTS = frozenset({
    "approve_with_findings", "findings", "non_blocking", "warn", "warning",
})
_SEVERITIES = frozenset({"blocker", "major", "minor", "nit"})
_BLOCKING_SEVERITIES = frozenset({"blocker", "major"})
_CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})
_BASIS_TYPES = frozenset({"spec", "code", "inference", "external"})
_FAIL_ON_KINDS = {
    "verdict": (
        _CI_PASS_VERDICTS | _CI_BLOCKING_VERDICTS | _CI_INFRA_VERDICTS
        | _CI_CONTEXT_VERDICTS | _CI_NON_BLOCKING_VERDICTS
    ),
    "severity": _SEVERITIES,
    "confidence": _CONFIDENCE_LEVELS,
    "basis": _BASIS_TYPES,
}

COST_BUDGET_EXIT_CODE = 3
_NO_PROVIDER_AVAILABLE_EXIT_CODE = 4
_UNSET = object()


class RunResult(tuple):
    """Tuple-compatible CLI result with execution metadata."""

    metadata: dict[str, Any]

    def __new__(cls, values, metadata=None):
        instance = super().__new__(cls, values)
        instance.metadata = metadata if metadata is not None else {}
        return instance


class _ProviderResolver(Protocol):
    def resolve(
        self,
        role: str,
        *,
        workdir: str,
        force: bool = False,
        force_provider: str | None = None,
    ) -> quota.ProviderDecision: ...


def run_phase_cmd(
    phase_name: str,
    role: str,
    workdir: str,
    resolver: _ProviderResolver | None,
    *,
    explicit_cmd: str | list[str] | tuple[str, ...] | None = None,
    force: bool = False,
    force_provider: str | None = None,
    **run_cli_kwargs: Any,
) -> RunResult:
    """Resolve and run the provider command for one pipeline phase.

    An explicitly selected command retains the legacy execution path and must
    not consult quota state.  The same is true when no resolver was created;
    in that case callers may supply the already-selected legacy command as
    either ``explicit_cmd`` or ``cmd``, but not both.  Supplying ``cmd`` with
    a resolver is invalid because provider resolution selects the command.
    """
    execution = dict(run_cli_kwargs)
    has_legacy_cmd = "cmd" in execution
    legacy_cmd = execution.pop("cmd", None)
    execution.setdefault("cwd", workdir)
    execution.setdefault("phase", phase_name)

    if has_legacy_cmd and (explicit_cmd is not None or resolver is not None):
        raise TypeError(
            "cmd may only be supplied when explicit_cmd is None and "
            "resolver is None"
        )
    if explicit_cmd is not None:
        return run_cli(explicit_cmd, **execution)
    if resolver is None:
        return run_cli(legacy_cmd, **execution)

    try:
        decision = resolver.resolve(
            role,
            workdir=workdir,
            force=force,
            force_provider=force_provider,
        )
    except quota.NoProviderAvailable as exc:
        # The resolver normally records this decision in its history, but a
        # caller-provided resolver only needs to implement resolve().  Build a
        # complete failure decision here so both forms expose stable metadata.
        decision = _failed_provider_decision(
            exc,
            forced=force or force_provider is not None,
        )
        metadata = {
            "error": str(exc),
            "provider_decision": _provider_decision_dict(
                phase_name, decision
            ),
            "rejection_reasons": dict(exc.reasons),
            "raw_snapshots": dict(exc.snapshots),
        }
        return RunResult(
            ("", str(exc), _NO_PROVIDER_AVAILABLE_EXIT_CODE), metadata
        )

    result = run_cli(decision.command, **execution)
    provider_metadata = _provider_decision_dict(phase_name, decision)
    if isinstance(result, RunResult):
        result.metadata["provider_decision"] = provider_metadata
        return result

    # Test doubles and third-party wrappers may return the historical tuple.
    # Preserve tuple compatibility while still honoring this API's metadata
    # guarantee.
    return RunResult(result, {"provider_decision": provider_metadata})


def _failed_provider_decision(
    error: quota.NoProviderAvailable,
    *,
    forced: bool,
) -> quota.ProviderDecision:
    return quota.ProviderDecision(
        alias=None,
        command=None,
        quota_state=quota.UNKNOWN,
        fallback=False,
        reason="no provider available",
        raw_snapshot=dict(error.snapshots),
        forced=forced,
        error=str(error),
    )


def _provider_decision_dict(
    phase_name: str,
    decision: quota.ProviderDecision,
) -> dict[str, object]:
    """Return the stable, command-free provider audit representation."""
    return {
        "phase": phase_name,
        "alias": decision.alias,
        "quota_state": decision.quota_state,
        "fallback": decision.fallback,
        "forced": decision.forced,
        "reason": decision.reason,
        "raw_snapshot": copy.deepcopy(decision.raw_snapshot),
    }


def collect_provider_history(results: Sequence[RunResult]) -> list[dict]:
    """Collect valid provider decisions in phase execution order.

    Explicit-command and legacy results do not carry a provider decision and
    are intentionally omitted.  Invalid decisions are also omitted so a
    partially upgraded or third-party runner cannot prevent final artifact
    generation.
    """
    if not isinstance(results, Sequence) or isinstance(
        results, (str, bytes, bytearray)
    ):
        raise TypeError("results must be a sequence")

    history: list[dict] = []
    for result in results:
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        decision = metadata.get("provider_decision")
        if decision is None:
            continue
        normalized = _validated_provider_decision(decision)
        if normalized is not None:
            history.append(normalized)
    return history


def _validated_provider_decision(value: object) -> dict | None:
    """Return a defensive copy of a provider decision with a valid schema."""
    if not isinstance(value, Mapping):
        return None

    required_types = {
        "phase": str,
        "quota_state": str,
        "fallback": bool,
        "forced": bool,
        "reason": str,
    }
    if any(
        type(value.get(key)) is not expected
        for key, expected in required_types.items()
    ):
        return None
    alias = value.get("alias")
    if alias is not None and type(alias) is not str:
        return None

    normalized = {
        "phase": value["phase"],
        "alias": alias,
        "quota_state": value["quota_state"],
        "fallback": value["fallback"],
        "forced": value["forced"],
        "reason": value["reason"],
    }
    if "raw_snapshot" in value:
        snapshot = value["raw_snapshot"]
        if not isinstance(snapshot, Mapping):
            return None
        try:
            snapshot_copy = copy.deepcopy(dict(snapshot))
            json.dumps(snapshot_copy)
        except Exception:
            # Provider metadata is an untrusted artifact boundary.  Custom
            # snapshot values may raise arbitrary exceptions from
            # ``__deepcopy__`` as well as the usual serialization errors.
            return None
        normalized["raw_snapshot"] = snapshot_copy
    return normalized


class _AnsiStrippingStream:
    """Minimal text stream that removes terminal controls from CI diagnostics."""

    def __init__(self, target):
        self._target = target

    def write(self, text):
        return self._target.write(_ANSI_ESCAPE_RE.sub("", str(text)))

    def flush(self):
        return self._target.flush()

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._target, "encoding", "utf-8")


@contextmanager
def ci_mode(enabled=True, *, stderr=None):
    """Route human stdout to plain-text stderr while CI mode is active.

    The helper deliberately does not alter global print or logging state.
    It redirects only the current context, making the disabled path a true
    no-op and allowing machine artifacts to remain the source of truth.
    """
    if not enabled:
        yield sys.stdout
        return
    target = sys.stderr if stderr is None else stderr
    stream = _AnsiStrippingStream(target)
    with redirect_stdout(stream):
        yield stream


def ci_print(*values, enabled=True, file=None, **kwargs):
    """Print a human diagnostic using CI stdout discipline."""
    target = (sys.stderr if file is None else file) if enabled else file
    if enabled:
        text = kwargs.pop("sep", " ").join(str(value) for value in values)
        end = kwargs.pop("end", "\n")
        if kwargs:
            print(_ANSI_ESCAPE_RE.sub("", text), end=end, file=target, **kwargs)
        else:
            target.write(_ANSI_ESCAPE_RE.sub("", text + end))
        return
    print(*values, file=target, **kwargs)


def parse_fail_on(selector=None):
    """Parse a strict, deterministic --fail-on selector.

    Selectors are comma-separated and combined with OR semantics. Supported
    shorthands are findings, blocking, non-blocking, confidence names,
    severities, and basis names. Typed forms include verdict:reject,
    severity:major, confidence:high, and basis:external. The none or never
    selector disables findings-based failure.
    """
    if selector is None:
        return frozenset({"findings"})
    if isinstance(selector, str):
        raw_tokens = selector.split(",")
    elif isinstance(selector, (Sequence, set, frozenset)) and not isinstance(
        selector, (bytes, bytearray)
    ):
        raw_tokens = []
        for value in selector:
            if not isinstance(value, str):
                raise TypeError("fail-on selectors must be strings")
            raw_tokens.extend(value.split(","))
    else:
        raise TypeError("fail-on selector must be a string collection")

    parsed = set()
    for raw_token in raw_tokens:
        token = _policy_name(raw_token)
        if token == "nonblocking":
            token = "non_blocking"
        if not token:
            raise ValueError("fail-on selector contains an empty value")
        if token in {"none", "never"}:
            if len(raw_tokens) != 1:
                raise ValueError("'none'/'never' cannot be combined with selectors")
            return frozenset()
        if token in {"all", "any", "findings"}:
            parsed.add("findings")
            continue
        if token in {"blocking", "non_blocking"}:
            parsed.add(token)
            continue
        if token in _CONFIDENCE_LEVELS:
            parsed.add(f"confidence:{token}")
            continue
        if token in _SEVERITIES:
            parsed.add(f"severity:{token}")
            continue
        if token in _BASIS_TYPES:
            parsed.add(f"basis:{token}")
            continue
        kind, separator, value = token.partition(":")
        value = _policy_name(value)
        allowed = _FAIL_ON_KINDS.get(kind)
        if not separator or allowed is None or value not in allowed:
            raise ValueError(f"unsupported fail-on selector: {raw_token!r}")
        parsed.add(f"{kind}:{value}")
    if not parsed:
        raise ValueError("fail-on selector must not be empty")
    return frozenset(parsed)


def ci_exit_code(
    verdict,
    epistemic=None,
    fail_on_selector=None,
    blocked=False,
    *,
    severity=None,
    findings=None,
    infrastructure=False,
    context_blocked=False,
):
    """Return the stable CI exit code for a final outcome.

    Operational failures are never suppressed by --fail-on: infrastructure
    maps to 1 and insufficient context to 5. Findings selected by policy map
    to 2 when blocking (blocker/major or a blocking verdict), otherwise to 3.
    A clean or policy-excluded outcome maps to 0.
    """
    payload = verdict if isinstance(verdict, Mapping) else None
    if payload is not None:
        verdict = payload.get("verdict", payload.get("status", "unknown"))
        if findings is None:
            findings = payload.get("findings")
        if epistemic is None:
            epistemic = payload.get(
                "epistemic", payload.get("label_distribution")
            )
        if severity is None:
            severity = payload.get("severity")
        infrastructure = infrastructure or _truthy_outcome(
            payload.get("infrastructure")
        )
        context_blocked = context_blocked or _truthy_outcome(
            payload.get("context_blocked")
        )
        context = payload.get("context")
        if isinstance(context, Mapping):
            context_blocked = context_blocked or context.get("ok") is False
        status = _policy_name(payload.get("status", ""))
        infrastructure = infrastructure or status in _CI_INFRA_VERDICTS
        context_blocked = context_blocked or status in _CI_CONTEXT_VERDICTS

    normalized_verdict = _policy_name(verdict)
    if context_blocked or blocked or normalized_verdict in _CI_CONTEXT_VERDICTS:
        return CI_EXIT_CONTEXT_BLOCKED
    if infrastructure or normalized_verdict in _CI_INFRA_VERDICTS:
        return CI_EXIT_INFRASTRUCTURE

    policy = parse_fail_on(fail_on_selector)
    if not policy:
        return CI_EXIT_CLEAN
    facts = _ci_finding_facts(findings)
    facts.extend(_ci_finding_facts(epistemic))
    facts.extend(_severity_facts(severity))

    matched = [
        fact for fact in facts
        if _finding_matches_policy(fact, policy, normalized_verdict)
    ]
    verdict_selected = f"verdict:{normalized_verdict}" in policy
    class_selected = (
        "blocking" in policy
        and normalized_verdict in _CI_BLOCKING_VERDICTS
    ) or (
        "non_blocking" in policy
        and normalized_verdict in _CI_NON_BLOCKING_VERDICTS
    )
    implicit_verdict = (
        "findings" in policy
        and normalized_verdict in (
            _CI_BLOCKING_VERDICTS | _CI_NON_BLOCKING_VERDICTS
        )
    )
    if (
        not matched
        and not verdict_selected
        and not class_selected
        and not implicit_verdict
    ):
        if normalized_verdict not in (
            _CI_PASS_VERDICTS | _CI_BLOCKING_VERDICTS
            | _CI_NON_BLOCKING_VERDICTS | {""}
        ):
            return CI_EXIT_INFRASTRUCTURE
        return CI_EXIT_CLEAN

    blocking = any(
        fact.get("severity") in _BLOCKING_SEVERITIES for fact in matched
    )
    if (
        normalized_verdict in _CI_BLOCKING_VERDICTS
        and (
            verdict_selected
            or class_selected
            or implicit_verdict
            or matched
        )
    ):
        blocking = True
    return CI_EXIT_BLOCKING if blocking else CI_EXIT_NON_BLOCKING


def ensure_final_payload(
    payload=None,
    *,
    verdict=None,
    findings=None,
    infrastructure=None,
    context=None,
    provider_history=_UNSET,
    **extra,
):
    """Return a complete, JSON-serializable final payload without mutation."""
    if payload is None:
        result = {}
    elif isinstance(payload, Mapping):
        result = dict(payload)
    else:
        raise TypeError("payload must be a mapping or None")
    if verdict is not None:
        result["verdict"] = verdict
    result.setdefault("verdict", "UNKNOWN")

    source_findings = findings if findings is not None else result.get("findings", [])
    if source_findings is None:
        source_findings = []
    if not isinstance(source_findings, Sequence) or isinstance(
        source_findings, (str, bytes, bytearray)
    ):
        raise TypeError("findings must be a sequence")
    result["findings"] = [
        dict(item) if isinstance(item, Mapping) else item
        for item in source_findings
    ]
    if infrastructure is not None:
        result["infrastructure"] = infrastructure
    if context is not None:
        result["context"] = context
    history_source = provider_history
    if history_source is _UNSET and "provider_history" in result:
        history_source = result["provider_history"]
    if history_source is not _UNSET:
        if not isinstance(history_source, Sequence) or isinstance(
            history_source, (str, bytes, bytearray)
        ):
            result["provider_history"] = []
        else:
            result["provider_history"] = [
                normalized
                for item in history_source
                if (normalized := _validated_provider_decision(item))
                is not None
            ]
    result.update(extra)

    normalized = _policy_name(result["verdict"])
    context_value = result.get("context")
    known_verdicts = (
        _CI_PASS_VERDICTS | _CI_BLOCKING_VERDICTS | _CI_INFRA_VERDICTS
        | _CI_CONTEXT_VERDICTS | _CI_NON_BLOCKING_VERDICTS
    )
    is_context_block = (
        normalized in _CI_CONTEXT_VERDICTS
        or _truthy_outcome(result.get("context_blocked"))
        or (
            isinstance(context_value, Mapping)
            and context_value.get("ok") is False
        )
    )
    is_infra = (
        normalized in _CI_INFRA_VERDICTS
        or _truthy_outcome(result.get("infrastructure"))
        or normalized not in known_verdicts
    )
    if is_context_block:
        result["status"] = "context_blocked"
    elif is_infra:
        result["status"] = "infrastructure_error"
    elif result["findings"] or normalized in (
        _CI_BLOCKING_VERDICTS | _CI_NON_BLOCKING_VERDICTS
    ):
        result["status"] = "findings"
    else:
        result["status"] = "clean"
    return result


build_final_payload = ensure_final_payload


def _policy_name(value):
    if value is None:
        return ""
    return re.sub(r"[-\s]+", "_", str(value).strip().casefold())


def _truthy_outcome(value):
    if isinstance(value, Mapping):
        return value.get("ok") is False or bool(value.get("error"))
    return bool(value)


def _ci_finding_facts(value):
    if value is None:
        return []
    if isinstance(value, str):
        normalized = _policy_name(value)
        for kind, allowed in (
            ("severity", _SEVERITIES),
            ("confidence", _CONFIDENCE_LEVELS),
            ("basis", _BASIS_TYPES),
        ):
            if normalized in allowed:
                return [{kind: normalized}]
        return []
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        facts = []
        for item in value:
            facts.extend(_ci_finding_facts(item))
        return facts
    if not isinstance(value, Mapping):
        return []

    nested = value.get("findings")
    if isinstance(nested, Sequence) and not isinstance(
        nested, (str, bytes, bytearray)
    ):
        return _ci_finding_facts(nested)

    fact = {}
    for kind, allowed in (
        ("severity", _SEVERITIES),
        ("confidence", _CONFIDENCE_LEVELS),
        ("basis", _BASIS_TYPES),
    ):
        field = value.get(kind)
        if isinstance(field, str):
            normalized = _policy_name(field)
            if normalized in allowed:
                fact[kind] = normalized
    facts = [fact] if fact else []
    for kind, allowed in (
        ("severity", _SEVERITIES),
        ("confidence", _CONFIDENCE_LEVELS),
        ("basis", _BASIS_TYPES),
    ):
        distribution = value.get(kind)
        if isinstance(distribution, Mapping):
            for label, count in distribution.items():
                normalized = _policy_name(label)
                if normalized in allowed and _positive_count(count):
                    facts.append({kind: normalized})
    if not facts:
        keys = {_policy_name(key) for key in value}
        for kind, allowed in (
            ("severity", _SEVERITIES),
            ("confidence", _CONFIDENCE_LEVELS),
            ("basis", _BASIS_TYPES),
        ):
            for label in keys & allowed:
                if _positive_count(value.get(label)):
                    facts.append({kind: label})
    return facts


def _severity_facts(value):
    if value is None:
        return []
    if isinstance(value, str):
        normalized = _policy_name(value)
        return [{"severity": normalized}] if normalized in _SEVERITIES else []
    if isinstance(value, Mapping):
        return [
            {"severity": normalized}
            for label, count in value.items()
            if (normalized := _policy_name(label)) in _SEVERITIES
            and _positive_count(count)
        ]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            fact
            for item in value
            for fact in _severity_facts(item)
        ]
    return []


def _positive_count(value):
    return value is not None and value is not False and (
        not isinstance(value, (int, float)) or value > 0
    )


def _finding_matches_policy(fact, policy, verdict):
    if "findings" in policy:
        return True
    severity = fact.get("severity")
    if "blocking" in policy and (
        severity in _BLOCKING_SEVERITIES
        or (severity is None and verdict in _CI_BLOCKING_VERDICTS)
    ):
        return True
    if "non_blocking" in policy and (
        severity not in _BLOCKING_SEVERITIES
        and verdict not in _CI_BLOCKING_VERDICTS
    ):
        return True
    return any(
        f"{kind}:{fact[kind]}" in policy
        for kind in ("severity", "confidence", "basis")
        if kind in fact
    )


def run_cli(
    cmd,
    stdin_text=None,
    timeout=600,
    cwd=None,
    persona_file=None,
    *,
    ledger=None,
    usage=None,
    model=None,
    phase="",
    persona="",
    include_usage=False,
    show_costs=False,
    budget=None,
    max_completion_tokens=None,
    max_retries=3,
    base=2.0,
    jitter=1.0,
    max_input_chars=DEFAULT_MAX_INPUT_CHARS,
    max_output_chars=DEFAULT_MAX_OUTPUT_CHARS,
    truncate_input=False,
    clock=None,
    sleeper=None,
    rng=None,
    attempt_log=None,
):
    """Run a CLI command and optionally account for its token usage.

    Existing callers continue to receive ``(stdout, stderr, returncode)``.
    ``include_usage=True`` appends a fourth, JSON-serializable usage record.
    Retry attempts and cap events are exposed on the result's ``metadata``
    attribute without changing tuple unpacking.

    A budgeted call requires ``max_completion_tokens`` to match the hard
    output-token limit configured on the provider command. Its prompt and
    maximum completion costs are reserved atomically before every attempt.
    """
    metadata = {"attempts": [], "cap_events": []}
    if isinstance(cmd, str):
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            return _result(
                "", f"Invalid command syntax: {exc}", -1, None,
                include_usage, metadata,
            )
    elif isinstance(cmd, (list, tuple)):
        argv = list(cmd)
    else:
        return _result(
            "", f"Unsupported command type: {type(cmd).__name__}", -1,
            None, include_usage, metadata,
        )
    if not argv:
        return _result("", "Empty command", -1, None, include_usage, metadata)
    argv = [os.path.expanduser(str(arg)) for arg in argv]

    retries = _non_negative_int("max_retries", max_retries)
    backoff_base = _non_negative_number("base", base)
    jitter_cap = _non_negative_number("jitter", jitter)
    input_limit = _optional_cap("max_input_chars", max_input_chars)
    output_limit = _optional_cap("max_output_chars", max_output_chars)
    completion_limit = _optional_cap(
        "max_completion_tokens", max_completion_tokens
    )
    if not isinstance(truncate_input, bool):
        raise TypeError("truncate_input must be a boolean")
    monotonic = time.monotonic if clock is None else clock
    sleep = time.sleep if sleeper is None else sleeper
    random_source = random if rng is None else rng
    if not callable(monotonic):
        raise TypeError("clock must be callable")
    if not callable(sleep):
        raise TypeError("sleeper must be callable")
    if attempt_log is not None and not hasattr(attempt_log, "append"):
        raise TypeError("attempt_log must support append")

    if persona_file:
        argv, stdin_text = providers.inject_persona(
            argv, persona_file, stdin_text, delimiter=True
        )

    if input_limit is not None and stdin_text is not None:
        capped_input, input_truncated = gates.enforce_input_cap(stdin_text, input_limit)
        if input_truncated:
            event = {
                "kind": "input",
                "limit": input_limit,
                "original_chars": len(stdin_text),
                "truncated": truncate_input,
            }
            metadata["cap_events"].append(event)
            if not truncate_input:
                metadata["input_rejected"] = True
                return _result(
                    "",
                    f"Input exceeds max_input_chars ({len(stdin_text)} > {input_limit})",
                    2,
                    None,
                    include_usage,
                    metadata,
                )
            stdin_text = capped_input

    if ledger is not None and not callable(getattr(ledger, "record", None)):
        raise TypeError("ledger must provide record()")
    if budget is not None and ledger is None:
        raise ValueError("budget requires a shared CostLedger")
    if budget is not None and completion_limit is None:
        raise ValueError(
            "budget requires max_completion_tokens matching the provider limit"
        )
    effective_model = model or _model_id(argv)
    prompt_tokens_estimate = costs.estimate_tokens(stdin_text)
    completion_tokens_estimate = completion_limit or 0

    stdout = stderr = ""
    returncode = -1
    usage_records = []
    provider_name = providers.detect_provider(argv)
    for attempt_index in range(retries + 1):
        reservation = None
        if budget is not None:
            budget_status, reservation = _reserve_budget(
                ledger,
                budget,
                effective_model,
                prompt_tokens_estimate,
                completion_tokens_estimate,
            )
            metadata["budget"] = budget_status
            if budget_status["refused"]:
                metadata["budget_exceeded"] = True
                stdout = ""
                stderr = (
                    "Cost budget would be exceeded before provider attempt "
                    f"{attempt_index + 1}: projected USD "
                    f"{budget_status['projected_total_usd']:.10f} > USD "
                    f"{budget_status['limit_usd']:.10f}"
                )
                returncode = COST_BUDGET_EXIT_CODE
                break

        started_at = monotonic()
        try:
            (
                raw_stdout,
                raw_stderr,
                returncode,
                attempt_started,
                genuine_timeout,
            ) = _execute_attempt(argv, stdin_text, timeout, cwd)
        except BaseException:
            if reservation is not None:
                ledger.release_budget(reservation)
            raise
        elapsed = max(0.0, monotonic() - started_at)
        stdout, stderr = raw_stdout, raw_stderr
        native_usage = providers.extract_usage_metadata(
            raw_stdout, raw_stderr, provider=provider_name
        )
        retry_reason = None
        if not genuine_timeout:
            retry_reason = providers.classify_transient_error(
                argv, returncode, raw_stderr, elapsed=elapsed, timeout=timeout
            )
        retryable = retry_reason is not None
        record = {
            "attempt": attempt_index + 1,
            "returncode": returncode,
            "elapsed": elapsed,
            "retryable": retryable,
            "reason": retry_reason or ("success" if returncode == 0 else "permanent"),
        }

        if output_limit is not None:
            stdout = _cap_attempt_output(
                raw_stdout, "stdout", output_limit, attempt_index + 1, metadata
            )
            stderr = _cap_attempt_output(
                raw_stderr, "stderr", output_limit, attempt_index + 1, metadata
            )

        should_retry = returncode != 0 and retryable and attempt_index < retries
        if attempt_started and ledger is not None:
            # Explicit usage describes the terminal attempt. Retried failures
            # must use their own provider metadata (or deterministic estimates).
            effective_usage = (
                usage if not should_retry and usage is not None else native_usage
            )
            usage_record = ledger.record(
                effective_model,
                prompt_text=stdin_text,
                completion_text=raw_stdout,
                usage=effective_usage,
                phase=phase,
                persona=persona,
                reservation=reservation,
            )
            usage_records.append(usage_record)
            record["usage"] = asdict(usage_record)
        elif native_usage is not None:
            record["native_usage"] = dict(native_usage)
            metadata["native_usage"] = dict(native_usage)
        if not attempt_started and reservation is not None:
            ledger.release_budget(reservation)

        if should_retry:
            delay = backoff_base * (2 ** attempt_index)
            if jitter_cap:
                delay += _random_jitter(random_source, jitter_cap)
            record["delay"] = delay
        _record_attempt(record, metadata, attempt_log)
        if not should_retry:
            break
        sleep(delay)

    if show_costs and ledger is not None:
        ledger.print_summary(file=sys.stderr)
    usage_record = _aggregate_usage(usage_records)
    if usage_record is not None:
        metadata["usage"] = asdict(usage_record)
    return _result(
        stdout, stderr, returncode, usage_record, include_usage, metadata
    )


def run_research(
    queries,
    provider_cmd=None,
    *,
    enabled=False,
    command=None,
    provider_command=None,
    max_queries=DEFAULT_RESEARCH_MAX_QUERIES,
    max_results=DEFAULT_RESEARCH_MAX_RESULTS,
    timeout=DEFAULT_RESEARCH_TIMEOUT,
    max_input_chars=DEFAULT_RESEARCH_MAX_INPUT_CHARS,
    max_output_chars=DEFAULT_RESEARCH_MAX_OUTPUT_CHARS,
    ledger=None,
    budget=None,
    model=None,
    max_completion_tokens=None,
    cwd=None,
    log=None,
    stderr=None,
    env=None,
):
    """Run opt-in, bounded external research through a configured provider.

    Disabled mode returns None before inspecting inputs or configuration. This
    lets callers omit the research field entirely and preserves legacy output
    byte-for-byte. Enabled failures are represented as non-fatal skip records
    and are also logged to stderr.
    """
    if not enabled:
        return None

    query_limit = _positive_int("max_queries", max_queries)
    result_limit = _non_negative_int("max_results", max_results)
    input_limit = _positive_int("max_input_chars", max_input_chars)
    output_limit = _positive_int("max_output_chars", max_output_chars)
    timeout_value = _non_negative_number("timeout", timeout)
    if timeout_value <= 0:
        raise ValueError("timeout must be positive")
    if env is not None and not isinstance(env, Mapping):
        raise TypeError("env must be a mapping or None")

    configured = [
        value for value in (provider_cmd, command, provider_command)
        if value is not None
    ]
    if len(configured) > 1 and any(value != configured[0] for value in configured[1:]):
        raise ValueError("research provider command was configured more than once")
    provider_cmd = configured[0] if configured else (
        os.environ if env is None else env
    ).get("ADVERSARIAL_RESEARCH_CMD")
    if not provider_cmd:
        return _research_skip(
            "research provider command is not configured",
            log=log,
            stderr=stderr,
        )
    if budget is not None and ledger is None:
        return _research_skip(
            "research budget requires a shared CostLedger",
            log=log,
            stderr=stderr,
        )

    if isinstance(queries, str):
        requested_queries = [queries]
    elif isinstance(queries, Sequence) and not isinstance(
        queries, (bytes, bytearray)
    ):
        requested_queries = list(queries)
    else:
        raise TypeError("queries must be a string or sequence of strings")
    if any(not isinstance(query, str) for query in requested_queries):
        raise TypeError("research queries must be strings")

    selected_queries = requested_queries[:query_limit]
    warnings = []
    if len(requested_queries) > query_limit:
        warnings.append({
            "code": "research_query_limit",
            "message": (
                f"research queries capped at {query_limit}; "
                f"{len(requested_queries) - query_limit} omitted"
            ),
        })
    response = {
        "enabled": True,
        "status": "complete",
        "non_fatal": True,
        "queries_requested": len(requested_queries),
        "queries_run": 0,
        "result_count": 0,
        "limits": {
            "queries": query_limit,
            "results": result_limit,
            "input_chars": input_limit,
            "output_chars": output_limit,
            "timeout": timeout_value,
        },
        "findings": [],
        "calls": [],
        "warnings": warnings,
    }
    if result_limit == 0 or not selected_queries:
        return response

    completion_limit = max_completion_tokens
    if budget is not None and completion_limit is None:
        completion_limit = (output_limit + 3) // 4
    output_remaining = output_limit

    for query_index, query in enumerate(selected_queries, start=1):
        if len(response["findings"]) >= result_limit or output_remaining <= 0:
            break
        bounded_query, input_truncated = gates.enforce_input_cap(query, input_limit)
        if input_truncated:
            warnings.append({
                "code": "research_input_truncated",
                "query": query_index,
                "message": f"research query {query_index} exceeded the input cap",
            })

        try:
            result = run_cli(
                provider_cmd,
                stdin_text=bounded_query,
                timeout=timeout_value,
                cwd=cwd,
                ledger=ledger,
                budget=budget,
                model=model,
                phase="research",
                persona="research",
                max_completion_tokens=completion_limit,
                max_retries=0,
                max_input_chars=input_limit,
                max_output_chars=output_remaining,
                truncate_input=True,
            )
        except Exception as exc:
            reason = f"research provider error: {type(exc).__name__}: {exc}"
            _record_research_failure(response, reason, log, stderr)
            break

        try:
            stdout, provider_stderr, returncode = result[:3]
        except (TypeError, ValueError) as exc:
            reason = f"research provider returned an invalid result: {exc}"
            _record_research_failure(response, reason, log, stderr)
            break
        stdout = stdout if isinstance(stdout, str) else str(stdout)
        provider_stderr = (
            provider_stderr if isinstance(provider_stderr, str)
            else str(provider_stderr)
        )
        bounded_stdout, output_truncated = gates.enforce_output_cap(
            stdout, output_remaining
        )
        bounded_stderr, stderr_truncated = gates.enforce_output_cap(
            provider_stderr, output_limit
        )
        call = {
            "query": query_index,
            "returncode": returncode,
            "stdout": bounded_stdout,
            "stderr": bounded_stderr,
        }
        metadata = getattr(result, "metadata", None)
        if isinstance(metadata, Mapping):
            call["metadata"] = _json_safe(metadata)
        response["calls"].append(call)
        response["queries_run"] += 1

        if output_truncated:
            warnings.append({
                "code": "research_output_truncated",
                "query": query_index,
                "message": f"research output exceeded the {output_limit}-character cap",
            })
        if stderr_truncated:
            warnings.append({
                "code": "research_stderr_truncated",
                "query": query_index,
                "message": "research provider stderr was truncated",
            })
        output_remaining -= len(bounded_stdout)

        if returncode != 0:
            if (
                returncode == COST_BUDGET_EXIT_CODE
                and isinstance(metadata, Mapping)
                and metadata.get("budget_exceeded")
            ):
                reason = "research skipped because the cost budget was exhausted"
            elif returncode == 127:
                reason = "research provider is unavailable"
            else:
                detail = bounded_stderr.strip() or f"exit code {returncode}"
                reason = f"research provider failed: {detail}"
            _record_research_failure(response, reason, log, stderr)
            break

        items = _research_result_items(bounded_stdout)
        available = result_limit - len(response["findings"])
        for item in items[:available]:
            response["findings"].append(
                _external_research_finding(item, bounded_query)
            )
        if len(items) > available:
            warnings.append({
                "code": "research_result_limit",
                "query": query_index,
                "message": f"research results capped at {result_limit}",
            })
            break

    response["result_count"] = len(response["findings"])
    return response


def _record_research_failure(response, reason, log, stderr):
    response["status"] = "partial" if response["findings"] else "skipped"
    response["reason"] = reason
    _emit_research_skip(reason, log=log, stderr=stderr)


def _research_skip(reason, *, log, stderr):
    _emit_research_skip(reason, log=log, stderr=stderr)
    return {
        "enabled": True,
        "status": "skipped",
        "non_fatal": True,
        "reason": reason,
        "queries_requested": 0,
        "queries_run": 0,
        "result_count": 0,
        "findings": [],
        "calls": [],
        "warnings": [],
    }


def _emit_research_skip(reason, *, log, stderr):
    message = f"Research skipped: {reason}"
    if log is not None:
        append = getattr(log, "append", None)
        if callable(append):
            append(message)
        elif callable(log):
            log(message)
        else:
            raise TypeError("log must be callable or support append")
    print(message, file=sys.stderr if stderr is None else stderr)


def _research_result_items(text):
    if not text.strip():
        return []
    payload = _decode_json_value(text)
    if isinstance(payload, Mapping):
        for key in ("findings", "results", "items", "evidence"):
            items = payload.get(key)
            if isinstance(items, Sequence) and not isinstance(
                items, (str, bytes, bytearray)
            ):
                return list(items)
        return [payload]
    if isinstance(payload, Sequence) and not isinstance(
        payload, (str, bytes, bytearray)
    ):
        return list(payload)
    if payload is not None:
        return [payload]
    return [text]


def _external_research_finding(item, query):
    if isinstance(item, Mapping):
        finding = _research_json_safe(item)
    else:
        finding = {"evidence": _research_json_safe(item)}
    if not isinstance(finding, dict):
        finding = {"evidence": finding}
    finding["basis"] = "external"
    confidence = _policy_name(finding.get("confidence"))
    finding["confidence"] = (
        confidence if confidence in _CONFIDENCE_LEVELS else "low"
    )
    finding.setdefault("origin", "research")
    finding.setdefault("query", query)
    return finding


def _research_json_safe(value):
    if isinstance(value, Mapping):
        return {
            str(key): _research_json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_research_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _execute_attempt(argv, stdin_text, timeout, cwd):
    out_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                text=True,
                cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError:
            missing_command = str(argv[0])
            if providers.detect_provider(argv) == "claude-tmux":
                diagnostic = (
                    "Claude wrapper command not found: "
                    f"{shlex.quote(missing_command)}. Set "
                    f"{providers.CLAUDE_TMUX_PATH_ENV} to the wrapper path "
                    f"or install {missing_command} on PATH."
                )
            else:
                diagnostic = f"Command not found: {shlex.quote(missing_command)}"
            return "", diagnostic, 127, False, False
        except OSError as exc:
            return "", f"OS error: {exc}", -1, False, False
        try:
            proc.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            proc.wait()
            out_f.seek(0)
            stdout = out_f.read().strip()
            return stdout, f"TIMEOUT after {timeout}s", 124, True, True
        out_f.seek(0)
        err_f.seek(0)
        return out_f.read().strip(), err_f.read().strip(), proc.returncode, True, False
    finally:
        out_f.close()
        err_f.close()


def _cap_attempt_output(text, stream, limit, attempt, metadata):
    capped, truncated = gates.enforce_output_cap(text, limit)
    if truncated:
        metadata["cap_events"].append({
            "kind": "output",
            "stream": stream,
            "attempt": attempt,
            "limit": limit,
            "original_chars": len(text),
            "truncated": True,
        })
    return capped


def _record_attempt(record, metadata, attempt_log):
    metadata["attempts"].append(record)
    if attempt_log is not None:
        attempt_log.append(dict(record))


def _random_jitter(source, maximum):
    uniform = getattr(source, "uniform", None)
    if callable(uniform):
        value = uniform(0.0, maximum)
    elif callable(source):
        try:
            value = source(0.0, maximum)
        except TypeError:
            value = source() * maximum
    else:
        random_method = getattr(source, "random", None)
        if not callable(random_method):
            raise TypeError("rng must be callable or provide uniform/random")
        value = random_method() * maximum
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("rng must return a number")
    return min(max(float(value), 0.0), maximum)


def run_parallel(calls, concurrency=None, *, max_concurrency=6):
    """Run independent ``(label, run_cli_args)`` calls in bounded threads.

    Results preserve input order. A failed or malformed call produces an error
    record for its own label and never cancels completed sibling calls.
    ``run_cli_args`` may be a keyword mapping, a positional sequence, or a
    command value accepted directly by :func:`run_cli`.
    """
    call_list = list(calls)
    if not call_list:
        return []
    cap = _positive_int("max_concurrency", max_concurrency)
    workers = 3 if concurrency is None else _positive_int("concurrency", concurrency)
    workers = min(workers, cap, len(call_list))
    results: list[dict[str, Any] | None] = [None] * len(call_list)

    def invoke(index, call):
        if not isinstance(call, (list, tuple)) or len(call) != 2:
            raise TypeError("each parallel call must be a (label, run_cli_args) pair")
        label, call_args = call
        return _call_record(label, _invoke_run_cli(call_args))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="adversarial") as pool:
        pending = {
            pool.submit(invoke, index, call): (index, call)
            for index, call in enumerate(call_list)
        }
        for future in as_completed(pending):
            index, call = pending[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                label = call[0] if isinstance(call, (list, tuple)) and call else index
                results[index] = {
                    "label": str(label),
                    "ok": False,
                    "stdout": "",
                    "stderr": str(exc),
                    "returncode": -1,
                    "error": f"{type(exc).__name__}: {exc}",
                    "result": ("", str(exc), -1),
                }
    return results


def run_delegated(
    input: str,  # noqa: A002 - mirrors gates.estimate_complexity's public API.
    decomposition_call,
    worker_call,
    synthesis_call,
    *,
    fallback_call=None,
    concurrency=None,
    max_concurrency=6,
    complexity=None,
):
    """Decompose and execute a high-complexity input with bounded workers.

    Call specifications use the same forms as :func:`run_parallel`. A mapping
    receives a default ``stdin_text`` containing the stage payload; a callable
    is treated as a call factory and receives the input, task, or surviving
    worker records for its stage. Explicit ``stdin_text`` values are preserved.

    Decomposition output must be JSON containing a ``tasks`` or ``subtasks``
    list (a top-level list is also accepted). Fan-out is capped by both the R5
    agent recommendation and ``max_concurrency``. Inputs below R5's ``high``
    tier run ``fallback_call`` when supplied and never start the orchestrator.
    The returned mapping retains every worker record and synthesis result so a
    caller can audit partial failures without parsing logs.
    """
    if not isinstance(input, str):
        raise TypeError("input must be a string")
    cap = _positive_int("max_concurrency", max_concurrency)
    if concurrency is not None:
        _positive_int("concurrency", concurrency)

    complexity_result = (
        gates.estimate_complexity(input, max_agents=cap)
        if complexity is None
        else _validate_complexity(complexity)
    )
    if complexity_result["level"] != "high":
        reason = (
            "delegated execution skipped: complexity level "
            f"{complexity_result['level']!r} is below required level 'high'"
        )
        return _delegated_fallback(
            input, fallback_call, complexity_result, reason
        )

    decomposition_spec = _stage_call(decomposition_call, input)
    decomposition = _safe_call_record("decomposition", decomposition_spec)
    if not decomposition["ok"]:
        reason = "delegated execution skipped: decomposition call failed"
        return _delegated_fallback(
            input,
            fallback_call,
            complexity_result,
            reason,
            decomposition=decomposition,
        )

    tasks, task_error = _decomposition_tasks(decomposition["stdout"])
    if task_error is not None:
        reason = f"delegated execution skipped: {task_error}"
        return _delegated_fallback(
            input,
            fallback_call,
            complexity_result,
            reason,
            decomposition=decomposition,
        )

    recommended = _positive_int(
        "complexity.recommended_agents",
        complexity_result["recommended_agents"],
    )
    worker_limit = min(recommended, cap)
    selected_tasks = tasks[:worker_limit]
    worker_calls = [
        (_task_label(task, index), _stage_call(worker_call, task))
        for index, task in enumerate(selected_tasks)
    ]
    workers = run_parallel(
        worker_calls,
        concurrency=concurrency,
        max_concurrency=cap,
    )
    for task, result in zip(selected_tasks, workers, strict=True):
        result["task"] = task
        result["origin"] = "worker"
        if result["ok"]:
            _tag_worker_output(result)

    survivors = [result for result in workers if result["ok"]]
    response = {
        "delegated": True,
        "mode": "delegated",
        "status": "failed",
        "reason": "all delegated workers failed",
        "complexity": complexity_result,
        "decomposition": decomposition,
        "tasks_total": len(tasks),
        "tasks_dispatched": len(selected_tasks),
        "tasks_omitted": max(0, len(tasks) - len(selected_tasks)),
        "workers": workers,
        "survivors": survivors,
        "partial": len(survivors) != len(workers),
        "synthesis": None,
        "result": None,
    }
    if not survivors:
        return response

    synthesis_spec = _stage_call(synthesis_call, survivors)
    synthesis = _safe_call_record("synthesis", synthesis_spec)
    if synthesis["ok"]:
        _tag_worker_output(synthesis)
        response.update({
            "status": "synthesized",
            "reason": (
                "synthesized surviving workers after partial failure"
                if response["partial"] else "synthesized all workers"
            ),
        })
    else:
        response["reason"] = "synthesis call failed; worker results preserved"
    response["synthesis"] = synthesis
    response["result"] = synthesis["result"]
    return response


def _invoke_run_cli(call_args):
    if callable(call_args):
        return call_args()
    if isinstance(call_args, Mapping):
        return run_cli(**dict(call_args))
    if isinstance(call_args, Sequence) and not isinstance(call_args, (str, bytes)):
        return run_cli(*call_args)
    return run_cli(call_args)


def _call_record(label, raw):
    if not isinstance(raw, Sequence) or len(raw) < 3:
        raise TypeError("run_cli result must contain stdout, stderr, and returncode")
    return {
        "label": str(label),
        "ok": raw[2] == 0,
        "stdout": raw[0],
        "stderr": raw[1],
        "returncode": raw[2],
        "result": raw,
    }


def _safe_call_record(label, call_args):
    try:
        return _call_record(label, _invoke_run_cli(call_args))
    except Exception as exc:
        return {
            "label": str(label),
            "ok": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "error": f"{type(exc).__name__}: {exc}",
            "result": ("", str(exc), -1),
        }


def _stage_call(call_or_factory, payload):
    call_args = call_or_factory(payload) if callable(call_or_factory) else call_or_factory
    if isinstance(call_args, Mapping):
        call_args = dict(call_args)
        call_args.setdefault("stdin_text", _payload_text(payload))
    return call_args


def _payload_text(payload):
    if isinstance(payload, str):
        return payload
    return json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True)


def _json_safe(value):
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if key != "result"
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _validate_complexity(value):
    if not isinstance(value, Mapping):
        raise TypeError("complexity must be a mapping or None")
    level = value.get("level")
    if level not in {"trivial", "low", "medium", "high"}:
        raise ValueError("complexity.level must be trivial, low, medium, or high")
    recommended = value.get("recommended_agents")
    _positive_int("complexity.recommended_agents", recommended)
    result = dict(value)
    result["level"] = level
    result["recommended_agents"] = recommended
    return result


def _delegated_fallback(
    input,
    fallback_call,
    complexity,
    reason,
    *,
    decomposition=None,
):
    fallback = None
    if fallback_call is not None:
        fallback = _safe_call_record("fallback", _stage_call(fallback_call, input))
    return {
        "delegated": False,
        "mode": "direct",
        "status": "fallback" if fallback is None or fallback["ok"] else "failed",
        "reason": reason,
        "log": [reason],
        "complexity": complexity,
        "decomposition": decomposition,
        "fallback": fallback,
        "result": None if fallback is None else fallback["result"],
    }


def _decomposition_tasks(text):
    payload = _decode_json_value(text)
    if isinstance(payload, Mapping):
        tasks = payload.get("tasks")
        if tasks is None:
            tasks = payload.get("subtasks")
    else:
        tasks = payload
    if not isinstance(tasks, list):
        return [], "decomposition output must contain a JSON task list"
    if not tasks:
        return [], "decomposition returned no tasks"
    return tasks, None


def _decode_json_value(text):
    if not isinstance(text, str) or not text.strip():
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = "\n".join(
            line for line in candidate.splitlines()
            if not line.strip().startswith("```")
        ).strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        decoder = json.JSONDecoder()
        decoded = []
        for start, character in enumerate(candidate):
            if character not in "[{":
                continue
            try:
                value, end = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
            decoded.append((end, -start, value))
        return max(decoded, key=lambda item: item[:2])[2] if decoded else None


def _task_label(task, index):
    if isinstance(task, Mapping):
        label = task.get("label", task.get("id"))
        if label is not None and str(label).strip():
            return str(label)
    return f"worker-{index + 1}"


def _tag_worker_output(record):
    payload = _decode_json_value(record["stdout"])
    if payload is None:
        return
    _mark_worker_findings(payload)
    stdout = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    raw = record["result"]
    metadata = getattr(raw, "metadata", None)
    values = (stdout, raw[1], raw[2], *raw[3:])
    record["stdout"] = stdout
    record["payload"] = payload
    record["result"] = RunResult(values, metadata)


def _mark_worker_findings(value, *, findings=False):
    if isinstance(value, Mapping):
        if findings:
            value.setdefault("origin", "worker")
        for key, item in value.items():
            _mark_worker_findings(item, findings=key == "findings")
        return
    if isinstance(value, list):
        for item in value:
            _mark_worker_findings(item, findings=findings)


def fail_phase(label, code, stderr):
    """Terminate the run when a CLI phase fails."""
    print(f"X Phase '{label}' failed (exit code {code})")
    if stderr:
        snippet = stderr[:500]
        suffix = "..." if len(stderr) > 500 else ""
        print(f"   stderr: {snippet}{suffix}")
    sys.exit(1)


def _reserve_budget(
    ledger, budget, model, prompt_tokens, completion_tokens
):
    reserve_method = getattr(ledger, "reserve_budget", None)
    release_method = getattr(ledger, "release_budget", None)
    if not callable(reserve_method) or not callable(release_method):
        raise TypeError(
            "budget requires a CostLedger with reserve_budget() and release_budget()"
        )
    return reserve_method(
        budget,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _aggregate_usage(records):
    if not records:
        return None
    if len(records) == 1:
        return records[0]
    first = records[0]
    return costs.UsageRecord(
        model=first.model,
        prompt_tokens=sum(record.prompt_tokens for record in records),
        completion_tokens=sum(record.completion_tokens for record in records),
        est_cost_usd=round(sum(record.est_cost_usd for record in records), 10),
        estimated=any(record.estimated for record in records),
        phase=first.phase,
        persona=first.persona,
    )


def _result(stdout, stderr, code, usage_record, include_usage, metadata=None):
    base = (stdout, stderr, code)
    if not include_usage:
        return RunResult(base, metadata)
    if usage_record is None:
        return RunResult(base + (None,), metadata)
    return RunResult(base + (asdict(usage_record),), metadata)


def _model_id(argv):
    for index, argument in enumerate(argv):
        if argument in {"--model", "-m"} and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith("--model="):
            return argument.partition("=")[2] or providers.detect_provider(argv)
    return providers.detect_provider(argv)


def _positive_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _non_negative_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _non_negative_number(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return float(value)


def _optional_cap(name, value):
    if value is None:
        return None
    return _non_negative_int(name, value)


__all__ = [
    "CI_EXIT_BLOCKING", "CI_EXIT_CLEAN", "CI_EXIT_CONTEXT_BLOCKED",
    "CI_EXIT_INFRASTRUCTURE", "CI_EXIT_NON_BLOCKING", "COST_BUDGET_EXIT_CODE",
    "DEFAULT_MAX_INPUT_CHARS", "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_RESEARCH_MAX_INPUT_CHARS", "DEFAULT_RESEARCH_MAX_OUTPUT_CHARS",
    "DEFAULT_RESEARCH_MAX_QUERIES", "DEFAULT_RESEARCH_MAX_RESULTS",
    "DEFAULT_RESEARCH_TIMEOUT", "RunResult", "build_final_payload", "ci_exit_code",
    "ci_mode", "ci_print", "collect_provider_history", "ensure_final_payload", "fail_phase",
    "parse_fail_on", "run_cli", "run_delegated", "run_parallel",
    "run_phase_cmd",
    "run_research",
]
