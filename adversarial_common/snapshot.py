"""Git working-tree baseline snapshot (used by the loop's FIXER fallback)."""

import os
import subprocess


def snapshot_workdir(workdir):
    """Record files already dirty at pipeline start, so the FIXER fallback only
    attributes NEW disk changes (since this snapshot) to the fixer. Returns a
    set of relative paths, or None when workdir is not a git repo."""
    if not (workdir and os.path.isdir(os.path.join(workdir, ".git"))):
        return None
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, cwd=workdir,
        )
        return {line[3:].strip() for line in proc.stdout.splitlines() if line.strip()}
    except Exception:
        return None
