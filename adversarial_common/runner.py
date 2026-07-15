"""Hardened subprocess execution and bounded parallel dispatch."""

from __future__ import annotations

import json
import os
import random
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any

from . import costs, gates, providers


DEFAULT_MAX_INPUT_CHARS = 256 * 1024
DEFAULT_MAX_OUTPUT_CHARS = 128 * 1024
COST_BUDGET_EXIT_CODE = 3


class RunResult(tuple):
    """Tuple-compatible CLI result with execution metadata."""

    metadata: dict[str, Any]

    def __new__(cls, values, metadata=None):
        instance = super().__new__(cls, values)
        instance.metadata = metadata if metadata is not None else {}
        return instance


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
        argv, stdin_text = providers.inject_persona(argv, persona_file, stdin_text)

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
        except FileNotFoundError as exc:
            return "", f"Command not found: {exc}", 127, False, False
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
    _mark_worker_findings(payload, root=True)
    stdout = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    raw = record["result"]
    metadata = getattr(raw, "metadata", None)
    values = (stdout, raw[1], raw[2], *raw[3:])
    record["stdout"] = stdout
    record["payload"] = payload
    record["result"] = RunResult(values, metadata)


def _mark_worker_findings(value, *, root=False, findings=False):
    if isinstance(value, Mapping):
        if findings:
            value.setdefault("origin", "worker")
        for key, item in value.items():
            _mark_worker_findings(item, findings=key == "findings")
        return
    if isinstance(value, list):
        for item in value:
            _mark_worker_findings(item, findings=findings or root)


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
    "COST_BUDGET_EXIT_CODE", "DEFAULT_MAX_INPUT_CHARS",
    "DEFAULT_MAX_OUTPUT_CHARS", "RunResult", "fail_phase", "run_cli",
    "run_delegated", "run_parallel",
]
