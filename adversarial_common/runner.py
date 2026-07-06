"""Hardened CLI subprocess execution shared by both adversarial pipelines.

This is the canonical run_cli(): temp files (not pipes) for stdout/stderr so a
hung sandbox grandchild cannot deadlock the parent on a full pipe buffer,
start_new_session=True so the whole process group can be killed on timeout.
"""

import os
import shlex
import signal
import subprocess
import sys
import tempfile

from . import providers


def run_cli(cmd, stdin_text=None, timeout=600, cwd=None, persona_file=None):
    """Run a CLI command (shell=False), optionally injecting a persona.

    `cmd` may be a string (shlex.split) or an argv list/tuple.
    Persona injection is provider-aware (see providers.inject_persona):
    native `claude` gets --append-system-prompt-file, everything else gets the
    persona text prefixed to stdin.

    Returns (stdout_stripped, stderr_stripped, returncode).
    """
    if isinstance(cmd, str):
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return "", f"Invalid command syntax: {e}", -1
    elif isinstance(cmd, (list, tuple)):
        argv = list(cmd)
    else:
        return "", f"Unsupported command type: {type(cmd).__name__}", -1
    if not argv:
        return "", "Empty command", -1

    if persona_file:
        argv, stdin_text = providers.inject_persona(argv, persona_file, stdin_text)

    out_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=out_f, stderr=err_f, text=True, cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            return "", f"Command not found: {e}", 127
        except OSError as e:
            return "", f"OS error: {e}", -1
        try:
            proc.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            proc.wait()
            out_f.seek(0)
            return out_f.read().strip(), f"TIMEOUT after {timeout}s", 124
        out_f.seek(0)
        err_f.seek(0)
        return out_f.read().strip(), err_f.read().strip(), proc.returncode
    finally:
        out_f.close()
        err_f.close()


def _fail_phase(label, code, stderr):
    """Terminate the run when a CLI phase fails — never feed downstream phases.

    Exits 1 (pipeline/infrastructure failure)."""
    print(f"X Phase '{label}' failed (exit code {code})")
    if stderr:
        snippet = stderr[:500]
        suffix = "..." if len(stderr) > 500 else ""
        print(f"   stderr: {snippet}{suffix}")
    sys.exit(1)


fail_phase = _fail_phase  # public alias
