"""Git workflow utilities for the v4 adversarial-loop pipeline.

All operations are synchronous, run via ``subprocess.run`` (never ``shell=True``),
and raise :class:`GitError` on any non-zero git exit. Every public function takes
``workdir`` as its first argument so callers stay explicit about which checkout a
mutation lands in.
"""

import os
import re
import subprocess
import uuid
from pathlib import Path


class GitError(Exception):
    """Raised when a git operation fails."""


# Repo-local identity set when no identity is configured anywhere, so commits
# never fail on a pre-existing repo lacking user.name/user.email (F6).
_LOOP_IDENTITY = ("adversarial-loop", "loop@adversarial.local")
DEFAULT_GIT_TIMEOUT = 120


def _run(workdir, args, timeout=DEFAULT_GIT_TIMEOUT):
    """Run ``git <args>`` in *workdir*. Returns (stdout, stderr, returncode).

    *timeout* (seconds) is forwarded to ``subprocess.run``. It defaults to
    :data:`DEFAULT_GIT_TIMEOUT` so a stalled git cannot hang the pipeline;
    callers may pass a different limit for operations with tighter bounds.
    """
    proc = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=workdir, timeout=timeout,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _git(workdir, args):
    """Run ``git <args>``, raising :class:`GitError` on non-zero exit."""
    out, err, rc = _run(workdir, args)
    if rc != 0:
        detail = (err or out).strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return out


# --- detection & initialization --------------------------------------------

def ensure_git_available():
    """Check ``git`` is installed and version >= 2.0.

    Returns ``(True, version_string)`` or ``(False, error_message)``.
    """
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return False, "git not found on PATH"
    if proc.returncode != 0:
        return False, f"git --version exited {proc.returncode}"
    m = re.search(r"git version (\d+)\.(\d+)", proc.stdout)
    if not m:
        return False, f"could not parse git version: {proc.stdout.strip()!r}"
    major = int(m.group(1))
    if major < 2:
        return False, f"git >= 2.0 required, found {m.group(0)}"
    return True, proc.stdout.strip()


def detect_enclosing_repo(workdir):
    """Return the absolute repo root enclosing *workdir*, or None.

    Uses ``git rev-parse --show-toplevel`` so worktrees and ``.git`` file pointers
    are handled correctly (equivalent to walking up for a ``.git`` entry).
    """
    out, _err, rc = _run(workdir, ["rev-parse", "--show-toplevel"])
    if rc != 0:
        return None
    root = out.strip()
    return root or None


def auto_init(workdir):
    """``git init`` *workdir*, ensure an identity and create the first commit.

    Preserves configured ``user.name`` / ``user.email`` values. For each value
    that is not configured locally, globally, or system-wide, sets the repo-local
    fallback (adversarial-loop / loop@adversarial.local) so commits do not fail.
    Pins the initial branch to ``main`` regardless of the host's
    ``init.defaultBranch``.

    Safety guard: refuses to auto-initialize when *workdir* contains child
    directories that are themselves git repositories. This prevents the pipeline
    from creating a parent repo over sibling repos (e.g. ``~/.hermes/skills/``
    containing adversarial-common, adversarial-code-loop, etc.), whose git
    operations (stash, checkout) can delete untracked content in sibling dirs.
    """
    # Safety guard: detect child git repos
    if _has_child_repos(workdir):
        children = _find_child_repos(workdir)
        raise GitError(
            f"refusing to auto-initialize git repo in {workdir}: "
            f"it contains {len(children)} child git repo(s) "
            f"({', '.join(children[:5])}{'...' if len(children) > 5 else ''}). "
            f"This would make the pipeline create a parent repo over sibling "
            f"repos, and git stash/checkout operations may destroy untracked "
            f"content in sibling directories. "
            f"Use --workdir on the specific repo instead."
        )
    _git(workdir, ["init"])
    # Pin HEAD to main before the first commit (works on every git >= 2.0).
    _git(workdir, ["symbolic-ref", "HEAD", "refs/heads/main"])
    ensure_git_identity(workdir)
    _git(workdir, ["commit", "--allow-empty", "-m", "Initial commit"])


def _has_child_repos(workdir):
    """True if any immediate subdirectory of *workdir* contains a ``.git``."""
    return len(_find_child_repos(workdir)) > 0


def _find_child_repos(workdir):
    """Return sorted list of subdirectory names that contain a ``.git``."""
    try:
        entries = sorted(
            e.name for e in os.scandir(workdir)
            if e.is_dir() and not e.name.startswith(".")
            and os.path.isdir(os.path.join(e.path, ".git"))
        )
    except OSError:
        return []
    return entries


def ensure_git_identity(workdir):
    """Set a repo-local fallback for each missing identity value.

    Values configured locally, globally, or system-wide are never overridden.
    """
    name, email = _LOOP_IDENTITY
    for key, value in (("user.name", name), ("user.email", email)):
        out, _err, rc = _run(workdir, ["config", key])
        if rc != 0 or not out.strip():
            _git(workdir, ["config", key, value])


def is_dirty(workdir):
    """True if there are uncommitted changes (tracked or untracked)."""
    out, _err, rc = _run(workdir, ["status", "--porcelain"])
    if rc != 0:
        raise GitError(f"git status failed in {workdir}")
    return bool(out.strip())


_STASH_MARKER_PREFIX = "adversarial-loop-stash:"


def _new_stash_marker():
    """Return a unique token embeddable in a stash's ``-m`` message.

    Generating it costs nothing (no git call, cannot fail), so it can be
    handed to *on_pushed* the instant the push succeeds and later used to
    find the exact stash by content instead of by position.
    """
    return f"{_STASH_MARKER_PREFIX}{uuid.uuid4().hex}"


def _resolve_stash_identity(workdir, stash_ref):
    """Resolve *stash_ref* to its current stash commit SHA.

    A real stash SHA (the normal case, once ``stash_dirty`` has resolved one)
    resolves directly and unambiguously via ``rev-parse``, regardless of how
    many other stashes have since been pushed or popped. A push marker (the
    ``stash_dirty`` interruption/total-failure fallback identity) has no
    fixed git-rev syntax, so it is instead located by matching the unique
    message ``stash_dirty`` recorded in the stash's reflog subject at push
    time — this still finds the right stash even if other stashes were
    pushed afterward and shifted every position, which a positional
    ``stash@{0}`` guess would not.
    """
    try:
        return _git(workdir, ["rev-parse", "--verify", stash_ref]).strip()
    except GitError:
        pass
    entries = _git(workdir, ["stash", "list", "--format=%H%x09%gs"]).splitlines()
    for line in entries:
        sha, _, subject = line.partition("\t")
        if stash_ref and stash_ref in subject:
            return sha.strip()
    raise GitError(f"could not resolve stash identity {stash_ref!r}")


def stash_dirty(workdir, *, on_pushed=None):
    """Stash any dirty changes (including untracked).

    Returns the stash commit SHA or ``""`` when nothing was stashed. The SHA
    remains bound to this stash even if later stash operations change the
    position of ``stash@{0}``.

    The stash is pushed with a unique marker message so it can be found by
    content rather than by position. After a successful ``git stash push -u``
    the SHA is resolved via ``stash@{0}`` (deterministic — nothing between
    the push and this attempt creates another stash); on failure it is
    recovered by searching the stash list for the marker via
    :func:`_resolve_stash_identity`, which remains correct even if another
    stash lands on top before recovery runs. If every post-push resolution
    command fails or is interrupted, the marker itself is returned (which
    :func:`unstash` re-resolves the same way at restore time). A caller that
    records the id only from this return value therefore never orphans a
    stash it cannot later pop, even when capture is interrupted
    mid-resolution.

    *on_pushed*, if given, is invoked with the marker the instant
    ``git stash push -u`` succeeds and BEFORE any resolution attempt — this
    costs no git call and cannot fail. A caller that records this into its
    run state is protected against an interruption (e.g. ``KeyboardInterrupt``
    — a ``BaseException`` that the resolution guards below do not catch)
    during the subsequent SHA lookup: even if ``stash_dirty`` itself raises
    before returning, the state already holds a recoverable, position-
    independent id that :func:`unstash` re-resolves at restore time, so the
    user's stashed work is never orphaned — even if another stash is pushed
    on top of it before that restore happens.
    """
    if not is_dirty(workdir):
        return ""
    marker = _new_stash_marker()
    _git(workdir, ["stash", "push", "-u", "-m", marker])
    if on_pushed is not None:
        # Record the marker BEFORE resolution, so an interruption
        # (KeyboardInterrupt / any BaseException the guards below don't catch)
        # during the SHA lookup still leaves the caller's state holding a
        # recoverable, position-independent stash identity instead of an
        # empty id.
        on_pushed(marker)
    try:
        sha = _git(workdir, ["rev-parse", "--verify", "stash@{0}"]).strip()
        if sha:
            return sha
    except (GitError, subprocess.TimeoutExpired):
        pass
    try:
        # ponytail: rev-parse failed after the push succeeded; recover the
        # SHA by marker instead of orphaning the stash.
        resolved = _resolve_stash_identity(workdir, marker)
        if resolved:
            return resolved
    except (GitError, subprocess.TimeoutExpired):
        pass
    # ponytail: every post-push resolution failed. Return the marker so
    # unstash re-resolves it (by content, not position) at restore time
    # instead of raising with an empty id and orphaning the user's work. Worst
    # case restore_git reports this id to the user for manual recovery.
    return marker


def unstash(workdir, stash_ref):
    """Pop *stash_ref* by identity, even if its stash position has changed.

    *stash_ref* is either a stash commit SHA (the common case) or, for the
    rare ``stash_dirty`` interruption/failure fallback, an opaque push
    marker — see :func:`_resolve_stash_identity`. Either form is re-resolved
    to a SHA and verified against the current stash list immediately before
    the pop, so a stash pushed later cannot redirect this operation to the
    wrong entry.
    """
    stash_sha = _resolve_stash_identity(workdir, stash_ref)
    stash_entries = _git(workdir, ["stash", "list", "--format=%H"]).splitlines()
    try:
        stash_index = stash_entries.index(stash_sha)
    except ValueError as exc:
        raise GitError(
            f"stash {stash_ref} ({stash_sha}) is no longer in the stash list"
        ) from exc

    pop_ref = f"stash@{{{stash_index}}}"
    out, err, rc = _run(workdir, ["stash", "pop", pop_ref])
    if rc != 0:
        raise GitError(
            f"git stash pop {stash_ref} failed (possible conflict): "
            f"{(err or out).strip()}"
        )


# --- branch management ------------------------------------------------------

def get_current_branch(workdir):
    """Return the current branch name.

    Raises :class:`GitError` on detached HEAD or if git doesn't answer within
    5 seconds (restoring the pre-consolidation inline lookup's ``timeout=5``);
    callers catching ``GitError`` fall back cleanly instead of hanging forever.
    """
    try:
        out, err, rc = _run(workdir, ["symbolic-ref", "--short", "HEAD"], timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git symbolic-ref timed out after 5s: {exc}") from exc
    if rc != 0:
        raise GitError(f"not on a branch (detached HEAD?): {(err or out).strip()}")
    return out.strip()


def checkout(workdir, branch):
    """Checkout an existing local *branch* in *workdir*."""
    _git(workdir, ["checkout", branch])


def branch_exists(workdir, name):
    """True if local branch *name* exists."""
    _out, _err, rc = _run(workdir, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"])
    return rc == 0


def sanitize_feature_name(name):
    """Lowercase *name*, replace non-alphanumerics with hyphens, cap at 40 chars."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return cleaned[:40]


def create_loop_branch(workdir, feature, parent_branch, prefix="loop"):
    """Create ``<prefix>/<feature>/<N>`` from *parent_branch*; return its name.

    *feature* is sanitized; *N* is one more than the highest existing N under
    ``<prefix>/<feature>/`` (starting at 1). *prefix* defaults to ``loop`` so
    existing callers are unaffected; the review pipeline passes ``review`` to
    keep its branch namespace separate from the loop pipeline's.
    """
    feature = sanitize_feature_name(feature)
    ns = f"{prefix}/{feature}/"
    out = _git(workdir, ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{ns}"])
    highest = 0
    for line in out.splitlines():
        tail = line.strip()
        if tail.startswith(ns):
            try:
                highest = max(highest, int(tail[len(ns):]))
            except ValueError:
                continue
    name = f"{ns}{highest + 1}"
    _git(workdir, ["branch", name, parent_branch])
    return name


def create_branch(workdir, name, parent):
    """Create a branch *name* from *parent*. No-op if already exists."""
    _git(workdir, ["branch", name, parent])


def delete_branch(workdir, name):
    """Delete local branch *name*. No-op (not an error) if it doesn't exist."""
    if not branch_exists(workdir, name):
        return
    _git(workdir, ["branch", "-D", name])


# --- commits & diffs --------------------------------------------------------

def commit_all(workdir, message):
    """Stage everything (``git add -A``) and commit.

    If the tree is already clean, create an empty commit so the call always
    advances history.
    """
    _git(workdir, ["add", "-A"])
    _out, _err, rc = _run(workdir, ["diff", "--cached", "--quiet"])
    # rc 0 => nothing staged -> empty commit; rc 1 => staged changes -> real commit.
    if rc == 0:
        _git(workdir, ["commit", "--allow-empty", "-m", message])
    else:
        _git(workdir, ["commit", "-m", message])


def record_branch_point(workdir, parent_branch):
    """Return the merge-base SHA between *parent_branch* and HEAD."""
    return _git(workdir, ["merge-base", parent_branch, "HEAD"]).strip()


def head_sha(workdir):
    """Return the SHA of HEAD."""
    return _git(workdir, ["rev-parse", "HEAD"]).strip()


def get_diff(workdir, base, head="HEAD", *extra_args):
    """Return the text of ``git diff <extra_args> <base>..<head>``.

    *extra_args* are spliced in before the range so callers can request e.g.
    ``--stat`` or a wider context (``-U5``) without reimplementing the call.
    Returns an empty string when nothing changed.
    """
    return _git(workdir, ["diff", *extra_args, f"{base}..{head}"])


# --- merge & reject ---------------------------------------------------------

def squash_merge(workdir, source_branch, target_branch, message):
    """Checkout *target_branch*, squash-merge *source_branch*, commit, drop source.

    Raises :class:`GitError` when the worktree is dirty or any merge operation
    fails. A failed squash is cleaned up before the exception is raised, and
    the caller is left on *target_branch* with the source branch preserved.
    """
    status_out, status_err, status_rc = _run(
        workdir, ["status", "--porcelain", "--untracked-files=no"]
    )
    if status_rc != 0:
        raise GitError(
            "could not check worktree before squash merge: "
            f"{(status_err or status_out).strip() or f'git status exited {status_rc}'}"
        )
    if status_out.strip():
        raise GitError(
            f"cannot squash merge {source_branch} -> {target_branch}: "
            "working tree has tracked or staged changes"
        )

    _git(workdir, ["checkout", target_branch])
    merge_base = _git(workdir, ["merge-base", target_branch, source_branch]).strip()
    source_head = _git(workdir, ["rev-parse", source_branch]).strip()
    # The source is already contained in the target. There is nothing to stage,
    # but finalization is complete and the obsolete source branch can be removed.
    if merge_base == source_head:
        delete_branch(workdir, source_branch)
        return

    out, err, rc = _run(workdir, ["merge", "--squash", source_branch])
    if rc != 0:
        detail = (err or out).strip() or f"git merge --squash exited {rc}"
        _cleanup_out, cleanup_err, cleanup_rc = _run(
            workdir, ["reset", "--merge", "HEAD"]
        )
        if cleanup_rc != 0:
            cleanup_detail = cleanup_err.strip() or f"exit {cleanup_rc}"
            detail = f"{detail}; cleanup failed: {cleanup_detail}"
        raise GitError(
            f"squash merge {source_branch} -> {target_branch} failed: {detail}"
        )

    _out, diff_err, diff_rc = _run(workdir, ["diff", "--cached", "--quiet"])
    if diff_rc == 0:
        # Histories may differ while resolving to the same tree (for example,
        # a change followed by its revert). Treat that as a successful no-op.
        delete_branch(workdir, source_branch)
        return
    if diff_rc != 1:
        raise GitError(
            "could not inspect squash-merge result: "
            f"{diff_err.strip() or f'git diff --cached --quiet exited {diff_rc}'}"
        )
    _git(workdir, ["commit", "-m", message])
    delete_branch(workdir, source_branch)


def reject_marker(workdir, message):
    """Record an empty ``[REJECTED] <message>`` commit on the current branch."""
    _git(workdir, ["commit", "--allow-empty", "-m", f"[REJECTED] {message}"])


# --- tags & gitignore -------------------------------------------------------

def tag_with_evidence(workdir, tag_name, evidence_file):
    """Create annotated tag *tag_name* at HEAD, annotated with *evidence_file*."""
    path = Path(evidence_file)
    if not path.is_absolute():
        path = Path(workdir) / path
    annotation = path.read_text()
    _git(workdir, ["tag", "-a", tag_name, "-m", annotation, "HEAD"])


def ensure_gitignore(workdir, pattern):
    """Append *pattern* to ``.gitignore`` (creating it) if not already present."""
    gi = Path(workdir) / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if pattern in existing.splitlines():
        return
    gi.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if (not existing or existing.endswith("\n")) else "\n"
    with gi.open("a") as fh:
        fh.write(separator + pattern + "\n")


# --- worktree & cherry-pick --------------------------------------------------

def create_worktree(repo, path, base_ref, branch_name=None):
    """Create a git worktree at *path* from *base_ref*.

    If *branch_name* is given, ``git worktree add -b <branch_name> <path>
    <base_ref>`` creates a new branch for the worktree. Otherwise a detached
    HEAD worktree is created.
    """
    args = ["worktree", "add"]
    if branch_name:
        args.extend(["-b", branch_name])
    args.extend([str(path), base_ref])
    _git(repo, args)


def remove_worktree(repo, path):
    """Force-remove a worktree and prune its metadata.

    No-op (not an error) when the worktree does not exist. Raises
    :class:`GitError` on a genuine git failure (the already-gone case is
    excluded by the existence check above).
    """
    p = Path(path)
    if not p.exists():
        return
    out, err, rc = _run(repo, ["worktree", "remove", "--force", str(path)])
    if rc != 0:
        detail = (err or out).strip()
        raise GitError(f"git worktree remove --force {path} failed: {detail}")


def prune_worktrees(repo):
    """Prune stale worktree metadata in *repo* (``git worktree prune``).

    Reconciles ``.git/worktrees/<id>`` entries whose working directories no
    longer exist. Idempotent and safe to call when nothing is stale.
    """
    _git(repo, ["worktree", "prune"])


def cherry_pick(workdir, commit_ref):
    """Cherry-pick *commit_ref* into the current branch in *workdir*.

    Raises :class:`GitError` on conflict. The index is left clean (``cherry-pick
    --abort`` has been run) before the exception is raised.
    """
    out, err, rc = _run(workdir, ["cherry-pick", commit_ref])
    if rc != 0:
        detail = (err or out).strip()
        # Clean up: abort the cherry-pick so the index is not mid-merge.
        _run(workdir, ["cherry-pick", "--abort"])
        raise GitError(f"cherry-pick {commit_ref} failed: {detail}")
