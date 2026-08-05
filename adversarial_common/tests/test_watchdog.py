"""Acceptance tests for the anti-stall watchdog (P3)."""

import os
import signal
import subprocess
import sys
import time

from adversarial_common.watchdog import WatchdogResult, monitor


def _alive(proc):
    """True when *proc* is still running."""
    return proc.poll() is None


# ----------------------------------------------------------------- AC1: normal exit
def test_normal_exit():
    """A short-lived process returns stalled=False, killed=False."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "exit(0)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = monitor(proc, timeout=5, poll_interval=0.1)
    assert isinstance(result, WatchdogResult)
    assert result.stalled is False
    assert result.killed is False
    assert not _alive(proc)


# ----------------------------------------------------------------- AC2: stall → kill
def test_stall_and_kill():
    """A process idling at 0 % CPU is killed by the watchdog."""
    # sleep uses negligible CPU after startup.
    proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = monitor(proc, timeout=1.0, poll_interval=0.15)
    assert result.stalled is True
    assert result.killed is True
    assert "stalled" in result.reason.lower()
    # Process should be dead.
    assert not _alive(proc)


# ----------------------------------------------------------------- AC3: sentinel heartbeat
def test_sentinel_heartbeat():
    """A process printing the sentinel resets the stall timer and is not killed."""
    script = (
        "import sys,time\n"
        "for _ in range(20):\n"
        "    print('HEARTBEAT', flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    result = monitor(proc, timeout=1.0, sentinel="HEARTBEAT", poll_interval=0.15)

    # The sentinel keeps the watchdog happy, but our script exits after ~2 s.
    # The monitor should see the process exit normally.
    if not _alive(proc):
        proc.wait()
    assert result.stalled is False
    assert result.killed is False


# ----------------------------------------------------------------- AC4: already-dead handle
def test_already_dead_handle():
    """A process that exited before monitor() returns cleanly."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "exit(0)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.wait()  # reap it

    result = monitor(proc, timeout=2, poll_interval=0.1)
    assert isinstance(result, WatchdogResult)
    # Already dead → not stalled (the spec allows stalled=True *or* graceful
    # error; we chose the simpler stalled=False path).
    assert result.stalled is False
    assert result.killed is False
