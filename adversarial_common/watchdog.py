"""Anti-stall watchdog: detects 0% CPU processes and terminates them.

Reuses ``runner._terminate_processes`` for SIGTERM/SIGKILL escalation.
Platform: Linux-first via ``/proc/<pid>/stat``; cross-platform via optional
``psutil``.
"""

from __future__ import annotations

import logging
import os
import select
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class WatchdogResult:
    stalled: bool
    reason: str
    killed: bool


def _get_cpu_time_seconds(pid: int) -> float | None:
    """Cumulative CPU time (user + system) in seconds.  psutil first, then
    ``/proc/<pid>/stat`` on Linux.  Returns ``None`` when the process cannot
    be queried (permission error, process gone, unsupported platform)."""
    try:
        import psutil  # noqa: F811  # ponytail: optional dep
        p = psutil.Process(pid)
        times = p.cpu_times()
        return times.user + times.system
    except ImportError:
        pass
    except Exception:
        # psutil available but query failed (process gone, permission, etc.).
        # Fall through to /proc.
        pass

    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            stat = fh.read()
        # /proc/<pid>/stat: "pid (comm) state ..." — comm may contain
        # spaces but never ")", so rfind locates the closing paren reliably.
        close_paren = stat.rfind(")")
        if close_paren == -1:
            return None
        after_comm = stat[close_paren + 2:].split()
        utime = int(after_comm[11])  # field 14 (1-indexed)
        stime = int(after_comm[12])  # field 15
        clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return (utime + stime) / clock_ticks
    except (FileNotFoundError, IndexError, ValueError, OSError):
        return None


def _check_sentinel(process: subprocess.Popen, sentinel: str) -> bool:
    """Non-blocking drain of stdout/stderr looking for *sentinel*.

    Returns ``True`` the moment the marker is found.
    """
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            while True:
                ready, _, _ = select.select([stream], [], [], 0)
                if not ready:
                    break
                # read() returns whatever is buffered without waiting for a
                # newline; readline() could block indefinitely on a partial
                # line that will never terminate.
                data = stream.read(4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
                if sentinel in text:
                    return True
        except (OSError, ValueError):
            pass
    return False


def _direct_kill(process: subprocess.Popen) -> None:
    """SIGTERM + grace + SIGKILL for a plain Popen handle (no session)."""
    try:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    except (ProcessLookupError, OSError):
        pass


def monitor(
    process: subprocess.Popen,
    timeout: float = 120,
    sentinel: str | None = None,
    poll_interval: float = 1.0,
) -> WatchdogResult:
    """Monitor *process* for stalls and terminate if 0 % CPU persists.

    Parameters
    ----------
    process:
        A running ``subprocess.Popen`` instance.  Reading ``stdout``/``stderr``
        requires that the caller opened the streams with ``PIPE``.
    timeout:
        Seconds of continuous 0 % CPU before the process is considered stalled.
    sentinel:
        If given, any occurrence of this string in ``stdout`` or ``stderr``
        resets the stall timer.
    poll_interval:
        Seconds between CPU samples.

    Returns
    -------
    WatchdogResult
        ``stalled`` is ``True`` when the process was killed or the kill
        attempt failed; ``killed`` indicates whether termination succeeded.
        A process that exits cleanly before the timeout returns
        ``stalled=False, killed=False``.
    """
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    last_cpu_time = _get_cpu_time_seconds(process.pid)
    stall_start: float | None = None
    cpu_readable = last_cpu_time is not None
    wall_start = time.monotonic()
    max_wall = timeout * 10  # ponytail: generous fallback when /proc is silent

    while True:
        if process.poll() is not None:
            return WatchdogResult(
                stalled=False, reason="process exited normally", killed=False
            )

        if sentinel is not None and _check_sentinel(process, sentinel):
            stall_start = None
            last_cpu_time = _get_cpu_time_seconds(process.pid)
            wall_start = time.monotonic()
            continue

        time.sleep(poll_interval)

        # Bail out if we can never read CPU and have been idling long enough.
        if not cpu_readable and time.monotonic() - wall_start >= max_wall:
            return WatchdogResult(
                stalled=True,
                reason="CPU unreadable; giving up after wall-clock timeout",
                killed=False,
            )

        current_cpu_time = _get_cpu_time_seconds(process.pid)
        if current_cpu_time is not None and last_cpu_time is not None:
            cpu_readable = True
            if current_cpu_time <= last_cpu_time:
                if stall_start is None:
                    stall_start = time.monotonic()
                elif time.monotonic() - stall_start >= timeout:
                    # --- attempt termination via existing machinery ---
                    # _terminate_processes only works on session-leader
                    # processes (start_new_session=True).  For plain Popen
                    # handles it is a ~2 s no-op; skip it outright.
                    pgid_works = False
                    try:
                        pgid_works = os.getpgid(process.pid) == process.pid
                    except (ProcessLookupError, OSError):
                        pass

                    if pgid_works:
                        from .runner import _terminate_processes

                        try:
                            _terminate_processes([process])
                        except Exception as exc:
                            logger.error(
                                "Error invoking _terminate_processes for pid %d: %s",
                                process.pid,
                                exc,
                            )
                        if process.poll() is not None:
                            return WatchdogResult(
                                stalled=True,
                                reason=f"CPU stalled at 0 % for {timeout:.0f} s",
                                killed=True,
                            )

                    # Fall back to direct signals for plain Popen handles.
                    if process.poll() is None:
                        _direct_kill(process)

                    if process.poll() is not None:
                        return WatchdogResult(
                            stalled=True,
                            reason=f"CPU stalled at 0 % for {timeout:.0f} s",
                            killed=True,
                        )
                    logger.error("Failed to kill stalled process %d", process.pid)
                    return WatchdogResult(
                        stalled=True,
                        reason="process could not be killed",
                        killed=False,
                    )
            else:
                stall_start = None
        last_cpu_time = current_cpu_time
