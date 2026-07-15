"""Hardened subprocess execution and bounded parallel dispatch."""

from __future__ import annotations

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
from typing import Any

from . import gates, providers


DEFAULT_MAX_INPUT_CHARS = 256 * 1024
DEFAULT_MAX_OUTPUT_CHARS = 128 * 1024


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

    stdout = stderr = ""
    returncode = -1
    started = False
    for attempt_index in range(retries + 1):
        started_at = monotonic()
        stdout, stderr, returncode, attempt_started, genuine_timeout = _execute_attempt(
            argv, stdin_text, timeout, cwd
        )
        elapsed = max(0.0, monotonic() - started_at)
        started = started or attempt_started
        retry_reason = None
        if not genuine_timeout:
            retry_reason = providers.classify_transient_error(
                argv, returncode, stderr, elapsed=elapsed, timeout=timeout
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
                stdout, "stdout", output_limit, attempt_index + 1, metadata
            )
            stderr = _cap_attempt_output(
                stderr, "stderr", output_limit, attempt_index + 1, metadata
            )

        should_retry = returncode != 0 and retryable and attempt_index < retries
        if should_retry:
            delay = backoff_base * (2 ** attempt_index)
            if jitter_cap:
                delay += _random_jitter(random_source, jitter_cap)
            record["delay"] = delay
        _record_attempt(record, metadata, attempt_log)
        if not should_retry:
            break
        sleep(delay)

    usage_record = None
    if started and ledger is not None:
        native_usage = usage or providers.extract_usage_metadata(stdout, stderr)
        usage_record = ledger.record(
            model or _model_id(argv),
            prompt_text=stdin_text,
            completion_text=stdout,
            usage=native_usage,
            phase=phase,
            persona=persona,
        )
        if show_costs:
            ledger.print_summary(file=sys.stderr)
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
        if isinstance(call_args, Mapping):
            raw = run_cli(**dict(call_args))
        elif isinstance(call_args, Sequence) and not isinstance(call_args, (str, bytes)):
            raw = run_cli(*call_args)
        else:
            raw = run_cli(call_args)
        code = raw[2]
        return {
            "label": str(label),
            "ok": code == 0,
            "stdout": raw[0],
            "stderr": raw[1],
            "returncode": code,
            "result": raw,
        }

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


def fail_phase(label, code, stderr):
    """Terminate the run when a CLI phase fails."""
    print(f"X Phase '{label}' failed (exit code {code})")
    if stderr:
        snippet = stderr[:500]
        suffix = "..." if len(stderr) > 500 else ""
        print(f"   stderr: {snippet}{suffix}")
    sys.exit(1)


def _result(stdout, stderr, code, usage_record, include_usage, metadata=None):
    base = (stdout, stderr, code)
    if not include_usage:
        return RunResult(base, metadata)
    if usage_record is None:
        return RunResult(base + (None,), metadata)
    from dataclasses import asdict
    return RunResult(base + (asdict(usage_record),), metadata)


def _model_id(argv):
    for flag in ("--model", "-m"):
        try:
            index = argv.index(flag)
        except ValueError:
            continue
        if index + 1 < len(argv):
            return argv[index + 1]
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
    "DEFAULT_MAX_INPUT_CHARS", "DEFAULT_MAX_OUTPUT_CHARS", "RunResult",
    "fail_phase", "run_cli", "run_parallel",
]
