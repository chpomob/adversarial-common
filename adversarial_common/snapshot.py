"""Git working-tree baseline snapshot (used by the loop's FIXER fallback)."""

import os
import subprocess


def snapshot_workdir(workdir):
    """Record files already dirty at pipeline start, so the FIXER fallback only
    attributes NEW disk changes (since this snapshot) to the fixer. Returns a
    set of relative paths, or None when workdir is not a git repo.

    Linked worktrees store ".git" as a file instead of a directory. Their
    snapshots intentionally ignore submodule state: asking Git to inspect a
    submodule can fail when its HEAD refers to worktree-private refs that are
    not available through the main repository.
    """
    if not workdir:
        return None

    git_entry = os.path.join(workdir, ".git")
    if not (os.path.isdir(git_entry) or os.path.isfile(git_entry)):
        return None

    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--ignore-submodules=all"],
            capture_output=True, text=True, timeout=10, cwd=workdir,
        )
        if proc.returncode != 0:
            return None
        return {line[3:].strip() for line in proc.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return None
