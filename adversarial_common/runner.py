"""Hardened subprocess execution and bounded parallel dispatch."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import providers


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
):
    """Run a CLI command and optionally account for its token usage.

    Existing callers continue to receive ``(stdout, stderr, returncode)``.
    ``include_usage=True`` appends a fourth, JSON-serializable usage record.
    """
    if isinstance(cmd, str):
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            return _result("", f"Invalid command syntax: {exc}", -1, None, include_usage)
    elif isinstance(cmd, (list, tuple)):
        argv = list(cmd)
    else:
        return _result(
            "", f"Unsupported command type: {type(cmd).__name__}", -1,
            None, include_usage,
        )
    if not argv:
        return _result("", "Empty command", -1, None, include_usage)
    argv = [os.path.expanduser(str(arg)) for arg in argv]

    if persona_file:
        argv, stdin_text = providers.inject_persona(argv, persona_file, stdin_text)

    out_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    stdout = ""
    stderr = ""
    returncode = -1
    started = False
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
            started = True
        except FileNotFoundError as exc:
            return _result("", f"Command not found: {exc}", 127, None, include_usage)
        except OSError as exc:
            return _result("", f"OS error: {exc}", -1, None, include_usage)
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
            stderr = f"TIMEOUT after {timeout}s"
            returncode = 124
        else:
            out_f.seek(0)
            err_f.seek(0)
            stdout = out_f.read().strip()
            stderr = err_f.read().strip()
            returncode = proc.returncode
    finally:
        out_f.close()
        err_f.close()

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
    return _result(stdout, stderr, returncode, usage_record, include_usage)


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


def _result(stdout, stderr, code, usage_record, include_usage):
    base = (stdout, stderr, code)
    if not include_usage:
        return base
    if usage_record is None:
        return base + (None,)
    from dataclasses import asdict
    return base + (asdict(usage_record),)


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


__all__ = ["fail_phase", "run_cli", "run_parallel"]
