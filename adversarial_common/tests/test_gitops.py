"""Tests for adversarial_common.gitops — exercise every public function against
a throwaway git repo created per-test."""

import os
import shutil
import subprocess
import tempfile

import pytest

from adversarial_common import gitops


def _git(workdir, *args):
    """Raw git helper for test assertions (no raising)."""
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=workdir,
    )
    return proc.stdout.strip(), proc.stderr.strip(), proc.returncode


def _write(workdir, name, content):
    path = os.path.join(workdir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w") as fh:
        fh.write(content)


def _read(workdir, name):
    with open(os.path.join(workdir, name)) as fh:
        return fh.read()


class TestGitOps:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- detection & init ---------------------------------------------------

    def test_ensure_git_available(self):
        ok, info = gitops.ensure_git_available()
        assert ok is True
        assert "git" in info.lower()

    def test_auto_init_creates_repo(self):
        gitops.auto_init(self.tmpdir)
        assert os.path.isdir(os.path.join(self.tmpdir, ".git"))

    def test_auto_init_sets_identity(self, monkeypatch):
        monkeypatch.setenv(
            "GIT_CONFIG_GLOBAL", os.path.join(self.tmpdir, "isolated-global-config")
        )
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

        gitops.auto_init(self.tmpdir)

        name, _, _ = _git(self.tmpdir, "config", "--local", "user.name")
        email, _, _ = _git(self.tmpdir, "config", "--local", "user.email")
        assert name == "adversarial-loop"
        assert email == "loop@adversarial.local"

    def test_auto_init_preserves_configured_global_identity(self, monkeypatch):
        global_config = os.path.join(self.tmpdir, "isolated-global-config")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", global_config)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        _git(self.tmpdir, "config", "--global", "user.name", "Existing User")
        _git(
            self.tmpdir,
            "config",
            "--global",
            "user.email",
            "existing@example.test",
        )

        gitops.auto_init(self.tmpdir)

        name, _, _ = _git(self.tmpdir, "config", "user.name")
        email, _, _ = _git(self.tmpdir, "config", "user.email")
        _, _, local_name_rc = _git(
            self.tmpdir, "config", "--local", "--get", "user.name"
        )
        _, _, local_email_rc = _git(
            self.tmpdir, "config", "--local", "--get", "user.email"
        )
        assert name == "Existing User"
        assert email == "existing@example.test"
        assert local_name_rc == 1
        assert local_email_rc == 1

    def test_ensure_git_identity_preserves_configured_local_identity(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            "GIT_CONFIG_GLOBAL", os.path.join(self.tmpdir, "isolated-global-config")
        )
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        _git(self.tmpdir, "init")
        _git(self.tmpdir, "config", "--local", "user.name", "Local User")
        _git(
            self.tmpdir,
            "config",
            "--local",
            "user.email",
            "local@example.test",
        )

        gitops.ensure_git_identity(self.tmpdir)

        name, _, _ = _git(self.tmpdir, "config", "--local", "user.name")
        email, _, _ = _git(self.tmpdir, "config", "--local", "user.email")
        assert name == "Local User"
        assert email == "local@example.test"

    def test_is_dirty_true(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "new.txt", "x")
        assert gitops.is_dirty(self.tmpdir) is True

    def test_is_dirty_false(self):
        gitops.auto_init(self.tmpdir)
        assert gitops.is_dirty(self.tmpdir) is False

    def test_stash_and_unstash(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        _write(self.tmpdir, "f.txt", "v2")
        assert gitops.is_dirty(self.tmpdir)

        ref = gitops.stash_dirty(self.tmpdir)
        stash_head, _, _ = _git(self.tmpdir, "rev-parse", "stash@{0}")
        assert ref == stash_head
        assert _read(self.tmpdir, "f.txt") == "v1"
        assert not gitops.is_dirty(self.tmpdir)

        gitops.unstash(self.tmpdir, ref)
        assert _read(self.tmpdir, "f.txt") == "v2"

    def test_unstash_resolves_sha_after_stash_stack_moves(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")

        _write(self.tmpdir, "f.txt", "v2")
        original_sha = gitops.stash_dirty(self.tmpdir)

        _write(self.tmpdir, "later.txt", "newer stash")
        newer_sha = gitops.stash_dirty(self.tmpdir)
        stash_shas, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert stash_shas.splitlines() == [newer_sha, original_sha]

        gitops.unstash(self.tmpdir, original_sha)

        assert _read(self.tmpdir, "f.txt") == "v2"
        remaining_shas, _, _ = _git(
            self.tmpdir, "stash", "list", "--format=%H"
        )
        assert remaining_shas.splitlines() == [newer_sha]

    # --- stash capture robustness (A2) -------------------------------------

    def test_stash_dirty_recovers_sha_when_rev_parse_fails(self):
        # `git stash push -u` can succeed while the follow-up `stash@{0}`
        # resolution fails (git error / timeout). stash_dirty must still return
        # the new stash's SHA recovered from the reflog head, so a caller that
        # records the id only on return never orphans a stash it cannot pop.
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        _write(self.tmpdir, "f.txt", "v2")

        orig_git = gitops._git

        def flaky_git(workdir, args):
            if args[:3] == ["rev-parse", "--verify", "stash@{0}"]:
                raise gitops.GitError("simulated rev-parse failure")
            return orig_git(workdir, args)

        gitops._git = flaky_git
        try:
            ref = gitops.stash_dirty(self.tmpdir)
        finally:
            gitops._git = orig_git

        assert ref  # not empty — the stash id was recovered, not orphaned
        stash_shas, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert stash_shas.splitlines() == [ref]
        # and the recovered id still pops by identity, restoring the work
        gitops.unstash(self.tmpdir, ref)
        assert _read(self.tmpdir, "f.txt") == "v2"
        remaining, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert remaining.strip() == ""

    def test_stash_dirty_never_orphans_when_all_resolution_fails(self):
        # A2 broad claim: `git stash push -u` can succeed while EVERY follow-up
        # resolution command fails (rev-parse AND the reflog-list recovery).
        # stash_dirty must still return a non-empty, recoverable id instead of
        # raising with state["stash_id"] empty and orphaning the user's work.
        # The just-pushed stash is deterministically stash@{0}, so the literal
        # selector is returned and unstash re-resolves it (git healthy by then).
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        _write(self.tmpdir, "f.txt", "v2")

        orig_git = gitops._git

        def total_git(workdir, args):
            if args[:3] == ["rev-parse", "--verify", "stash@{0}"]:
                raise gitops.GitError("simulated rev-parse failure")
            if args[:2] == ["stash", "list"]:
                raise gitops.GitError("simulated reflog-list failure")
            return orig_git(workdir, args)

        gitops._git = total_git
        try:
            ref = gitops.stash_dirty(self.tmpdir)
        finally:
            gitops._git = orig_git

        # The push marker is the deterministic, recoverable fallback.
        assert ref.startswith(gitops._STASH_MARKER_PREFIX)
        # The stash exists on disk (push succeeded) and the id still pops,
        # restoring the user's work end-to-end.
        stash_list, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert stash_list.strip() != ""
        gitops.unstash(self.tmpdir, ref)
        assert _read(self.tmpdir, "f.txt") == "v2"
        remaining, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert remaining.strip() == ""

    def test_stash_dirty_records_id_before_resolution_can_be_interrupted(self):
        # A2 residual gap the GitError-only tests could not prove: a
        # BaseException (e.g. KeyboardInterrupt) raised during the post-push
        # SHA resolution propagates before stash_dirty returns, so a caller
        # that records the id only from the return value would orphan the
        # stash. on_pushed fires the instant `git stash push -u` succeeds and
        # BEFORE any resolution, so the caller's state is populated with a
        # recoverable id even when stash_dirty itself raises mid-resolution.
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        _write(self.tmpdir, "f.txt", "v2")

        recorded = {}

        def recorder(stash_id):
            recorded["stash_id"] = stash_id

        orig_git = gitops._git

        def interrupting_git(workdir, args):
            if args[:3] == ["rev-parse", "--verify", "stash@{0}"]:
                raise KeyboardInterrupt("simulated Ctrl-C during capture")
            return orig_git(workdir, args)

        gitops._git = interrupting_git
        try:
            with pytest.raises(KeyboardInterrupt):
                gitops.stash_dirty(self.tmpdir, on_pushed=recorder)
        finally:
            gitops._git = orig_git

        # The id was recorded before the interruption: state is recoverable
        # even though stash_dirty raised without returning.
        assert recorded["stash_id"].startswith(gitops._STASH_MARKER_PREFIX)
        # The stash exists on disk (push succeeded) and the recorded selector
        # pops by identity, restoring the user's work end-to-end.
        gitops.unstash(self.tmpdir, recorded["stash_id"])
        assert _read(self.tmpdir, "f.txt") == "v2"
        remaining, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert remaining.strip() == ""

    def test_stash_dirty_interrupted_marker_survives_an_intervening_stash(self):
        # M1 AC2: the marker on_pushed records during an interrupted capture
        # must stay resolvable even if another stash lands on top of it
        # before restore_git eventually calls unstash — a positional
        # "stash@{0}" guess would instead pop the newer, wrong stash.
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        _write(self.tmpdir, "f.txt", "v2-original")

        recorded = {}

        def recorder(stash_id):
            recorded["stash_id"] = stash_id

        orig_git = gitops._git

        def interrupting_git(workdir, args):
            if args[:3] == ["rev-parse", "--verify", "stash@{0}"]:
                raise KeyboardInterrupt("simulated Ctrl-C during capture")
            return orig_git(workdir, args)

        gitops._git = interrupting_git
        try:
            with pytest.raises(KeyboardInterrupt):
                gitops.stash_dirty(self.tmpdir, on_pushed=recorder)
        finally:
            gitops._git = orig_git

        # A second, unrelated stash lands on top of the interrupted one
        # before restore ever runs, shifting stash@{0} to point elsewhere.
        _write(self.tmpdir, "f.txt", "v3-intervening")
        second_sha = gitops.stash_dirty(self.tmpdir)
        stash_shas, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert len(stash_shas.splitlines()) == 2

        gitops.unstash(self.tmpdir, recorded["stash_id"])

        # The FIRST (interrupted) stash's content is restored, not the
        # second's — proving the marker resolves by content, not position.
        assert _read(self.tmpdir, "f.txt") == "v2-original"
        remaining, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert remaining.splitlines() == [second_sha]

    # --- P10: stash transaction atomicity -----------------------------------

    def test_stash_identity_is_sha_atomic(self):
        # AC1: push a stash, let a concurrent/unrelated stash land on top of
        # it, then unstash by the recorded SHA — the SHA is bound to this
        # stash's content, not to a stack position, so the right content
        # comes back even though it is no longer stash@{0}.
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")

        _write(self.tmpdir, "f.txt", "v2-mine")
        my_sha = gitops.stash_dirty(self.tmpdir)
        assert not gitops.is_dirty(self.tmpdir)

        # A concurrent stash push (e.g. another process/run) lands on top,
        # shifting stash@{0} to point at it instead of my_sha.
        _write(self.tmpdir, "other.txt", "concurrent")
        concurrent_sha = gitops.stash_dirty(self.tmpdir)
        stash_shas, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert stash_shas.splitlines() == [concurrent_sha, my_sha]

        gitops.unstash(self.tmpdir, my_sha)

        assert _read(self.tmpdir, "f.txt") == "v2-mine"
        assert not os.path.exists(os.path.join(self.tmpdir, "other.txt"))
        remaining, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert remaining.splitlines() == [concurrent_sha]

    def test_stash_interrupted_resolution_recoverable(self):
        # AC2: an interruption mid-resolution (after push, before the SHA
        # lookup completes) must still leave the stash recoverable by
        # content via the id handed to on_pushed — run_transaction's
        # rollback never drops the just-pushed stash, since doing so would
        # destroy the user's work instead of merely failing to name it.
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        _write(self.tmpdir, "f.txt", "v2")

        recorded = {}

        def recorder(stash_id):
            recorded["stash_id"] = stash_id

        orig_git = gitops._git

        def interrupting_git(workdir, args):
            if args[:3] == ["rev-parse", "--verify", "stash@{0}"]:
                raise KeyboardInterrupt("simulated Ctrl-C during resolution")
            return orig_git(workdir, args)

        gitops._git = interrupting_git
        try:
            with pytest.raises(KeyboardInterrupt):
                gitops.stash_dirty(self.tmpdir, on_pushed=recorder)
        finally:
            gitops._git = orig_git

        # The push step already succeeded and recorded a content-addressed
        # id before the interruption hit the resolve step.
        assert recorded["stash_id"].startswith(gitops._STASH_MARKER_PREFIX)
        stash_list, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert stash_list.strip() != ""

        gitops.unstash(self.tmpdir, recorded["stash_id"])

        assert _read(self.tmpdir, "f.txt") == "v2"
        remaining, _, _ = _git(self.tmpdir, "stash", "list", "--format=%H")
        assert remaining.strip() == ""

    def test_unstash_verifies_sha_exists(self):
        # AC3: a SHA that resolves to a real git object but is not (or is no
        # longer) in the stash list must fail gracefully with a GitError,
        # not an unhandled exception or a silent no-op.
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        missing_sha = gitops.head_sha(self.tmpdir)

        with pytest.raises(gitops.GitError, match="no longer in the stash list"):
            gitops.unstash(self.tmpdir, missing_sha)

        # An opaque id that resolves to nothing at all (not a real object,
        # not a marker in the stash reflog) is equally graceful.
        bogus_marker = gitops._STASH_MARKER_PREFIX + "0" * 32
        with pytest.raises(gitops.GitError, match="could not resolve stash identity"):
            gitops.unstash(self.tmpdir, bogus_marker)

    # --- branch management --------------------------------------------------

    def test_create_loop_branch(self):
        gitops.auto_init(self.tmpdir)
        name = gitops.create_loop_branch(self.tmpdir, "my feature", "main")
        assert name == "loop/my-feature/1"
        assert gitops.branch_exists(self.tmpdir, name)

    def test_create_loop_branch_increments_N(self):
        gitops.auto_init(self.tmpdir)
        first = gitops.create_loop_branch(self.tmpdir, "feat", "main")
        second = gitops.create_loop_branch(self.tmpdir, "feat", "main")
        assert first == "loop/feat/1"
        assert second == "loop/feat/2"

    # --- commits & diffs ----------------------------------------------------

    def test_commit_all(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "a.txt", "alpha")
        gitops.commit_all(self.tmpdir, "add a")
        assert not gitops.is_dirty(self.tmpdir)
        msg, _, _ = _git(self.tmpdir, "log", "-1", "--pretty=%s")
        assert msg == "add a"

    def test_commit_all_empty(self):
        gitops.auto_init(self.tmpdir)
        before, _, _ = _git(self.tmpdir, "rev-list", "--count", "HEAD")
        gitops.commit_all(self.tmpdir, "nothing new")
        after, _, _ = _git(self.tmpdir, "rev-list", "--count", "HEAD")
        assert int(after) == int(before) + 1
        msg, _, _ = _git(self.tmpdir, "log", "-1", "--pretty=%s")
        assert msg == "nothing new"

    def test_record_branch_point(self):
        gitops.auto_init(self.tmpdir)
        loop = gitops.create_loop_branch(self.tmpdir, "feat", "main")
        _git(self.tmpdir, "checkout", loop)
        _write(self.tmpdir, "c.txt", "c")
        gitops.commit_all(self.tmpdir, "on loop")
        sha = gitops.record_branch_point(self.tmpdir, "main")
        assert isinstance(sha, str) and len(sha) == 40
        assert all(ch in "0123456789abcdef" for ch in sha)

    def test_get_diff(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "d.txt", "one\n")
        gitops.commit_all(self.tmpdir, "base d")
        loop = gitops.create_loop_branch(self.tmpdir, "feat", "main")
        _git(self.tmpdir, "checkout", loop)
        _write(self.tmpdir, "d.txt", "one\ntwo\n")
        gitops.commit_all(self.tmpdir, "edit d")
        diff = gitops.get_diff(self.tmpdir, "main")
        assert "+two" in diff

    # --- merge & reject -----------------------------------------------------

    def test_squash_merge(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "m.txt", "1\n")
        gitops.commit_all(self.tmpdir, "base")
        loop = gitops.create_loop_branch(self.tmpdir, "feat", "main")
        _git(self.tmpdir, "checkout", loop)
        _write(self.tmpdir, "m.txt", "1\n2\n")
        gitops.commit_all(self.tmpdir, "loop change")

        gitops.squash_merge(self.tmpdir, loop, "main", "squashed feat")

        assert _read(self.tmpdir, "m.txt") == "1\n2\n"
        assert not gitops.branch_exists(self.tmpdir, loop)
        msg, _, _ = _git(self.tmpdir, "log", "-1", "--pretty=%s")
        assert msg == "squashed feat"

    def test_squash_merge_conflict_raises_and_cleans_worktree(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "shared.txt", "base\n")
        gitops.commit_all(self.tmpdir, "base")
        loop = gitops.create_loop_branch(self.tmpdir, "conflict", "main")

        _git(self.tmpdir, "checkout", loop)
        _write(self.tmpdir, "shared.txt", "feature\n")
        gitops.commit_all(self.tmpdir, "feature edit")

        _git(self.tmpdir, "checkout", "main")
        _write(self.tmpdir, "shared.txt", "parent\n")
        gitops.commit_all(self.tmpdir, "parent edit")

        with pytest.raises(gitops.GitError, match="squash merge.*failed") as exc:
            gitops.squash_merge(self.tmpdir, loop, "main", "must not commit")

        # Git localizes its conflict marker (for example, ``CONFLIT`` under a
        # French locale), but the path still proves that its diagnostic was
        # preserved in our GitError.
        assert "shared.txt" in str(exc.value)
        assert gitops.get_current_branch(self.tmpdir) == "main"
        assert gitops.branch_exists(self.tmpdir, loop)
        assert not gitops.is_dirty(self.tmpdir)
        assert _read(self.tmpdir, "shared.txt") == "parent\n"

    def test_squash_merge_dirty_tree_raises(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "tracked.txt", "original")
        gitops.commit_all(self.tmpdir, "tracked base")
        loop = gitops.create_loop_branch(self.tmpdir, "dirty", "main")
        _write(self.tmpdir, "tracked.txt", "keep me")

        with pytest.raises(gitops.GitError, match="tracked or staged changes"):
            gitops.squash_merge(self.tmpdir, loop, "main", "must not commit")

        assert gitops.branch_exists(self.tmpdir, loop)
        assert _read(self.tmpdir, "tracked.txt") == "keep me"

    def test_reject_marker(self):
        gitops.auto_init(self.tmpdir)
        gitops.reject_marker(self.tmpdir, "tests failed")
        msg, _, _ = _git(self.tmpdir, "log", "-1", "--pretty=%s")
        assert msg == "[REJECTED] tests failed"

    # --- misc ---------------------------------------------------------------

    def test_sanitize_feature_name(self):
        assert gitops.sanitize_feature_name("My Cool Feature!") == "my-cool-feature"
        assert len(gitops.sanitize_feature_name("a" * 50)) == 40
        assert gitops.sanitize_feature_name("a/b@c") == "a-b-c"

    def test_ensure_gitignore_not_present(self):
        gitops.ensure_gitignore(self.tmpdir, "*.log")
        with open(os.path.join(self.tmpdir, ".gitignore")) as fh:
            content = fh.read()
        assert "*.log" in content.splitlines()

    def test_prune_worktrees_runs_git_worktree_prune(self):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")

        wt_path = os.path.join(self.tmpdir, "wt")
        gitops.create_worktree(self.tmpdir, wt_path, "HEAD")
        wt_list, _, _ = _git(self.tmpdir, "worktree", "list")
        assert wt_path in wt_list

        # Simulate a stale metadata entry: remove the working dir but leave
        # .git/worktrees/<id> behind.
        shutil.rmtree(wt_path)
        gitops.prune_worktrees(self.tmpdir)

        wt_list_after, _, _ = _git(self.tmpdir, "worktree", "list")
        assert wt_path not in wt_list_after

    def test_ensure_gitignore_already_present(self):
        gitops.ensure_gitignore(self.tmpdir, "*.log")
        gitops.ensure_gitignore(self.tmpdir, "*.log")
        with open(os.path.join(self.tmpdir, ".gitignore")) as fh:
            content = fh.read()
        assert content.count("*.log") == 1

    def test_remove_worktree_raises_on_real_failure(self, monkeypatch):
        # R2/AC2: a genuine git failure (not the already-gone case, excluded
        # by remove_worktree's existence check) must raise GitError instead
        # of being swallowed.
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        wt_path = os.path.join(self.tmpdir, "wt")
        gitops.create_worktree(self.tmpdir, wt_path, "HEAD")

        def fake_run(workdir, args, timeout=gitops.DEFAULT_GIT_TIMEOUT):
            return "", "fatal: simulated worktree remove failure", 1

        monkeypatch.setattr(gitops, "_run", fake_run)
        with pytest.raises(gitops.GitError, match="simulated worktree remove failure"):
            gitops.remove_worktree(self.tmpdir, wt_path)


class TestWorktreeTransaction:
    """teardown_worktree: remove -> delete branch -> prune as one transaction."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_worktree(self, branch):
        gitops.auto_init(self.tmpdir)
        _write(self.tmpdir, "f.txt", "v1")
        gitops.commit_all(self.tmpdir, "base")
        wt_path = os.path.join(self.tmpdir, "wt")
        gitops.create_worktree(self.tmpdir, wt_path, "HEAD", branch_name=branch)
        return wt_path

    def test_worktree_transaction_atomic(self):
        # AC1: remove fails -> worktree still present, branch intact, and
        # the failure/rollback-skipped log is recorded; later steps
        # (delete branch, prune) never run.
        branch = "loop/atomic/1"
        wt_path = self._make_worktree(branch)

        orig_run = gitops._run

        def failing_run(workdir, args, timeout=gitops.DEFAULT_GIT_TIMEOUT):
            if args[:2] == ["worktree", "remove"]:
                return "", "simulated remove failure", 1
            return orig_run(workdir, args, timeout=timeout)

        gitops._run = failing_run
        state = {}
        try:
            with pytest.raises(gitops.GitError, match="simulated remove failure"):
                gitops.teardown_worktree(self.tmpdir, wt_path, branch, state=state)
        finally:
            gitops._run = orig_run

        assert os.path.isdir(wt_path)
        wt_list, _, _ = _git(self.tmpdir, "worktree", "list")
        assert wt_path in wt_list
        assert gitops.branch_exists(self.tmpdir, branch)

        assert [entry["step"] for entry in state["transactions"]] == [
            "_remove", "rollback",
        ]
        assert state["transactions"][0]["status"] == "failed"
        assert state["transactions"][1] == {
            "step": "rollback", "status": "skipped", "detail": "",
        }
        assert state["last_transaction"]["outcome"] == "failed"
        assert state["last_transaction"]["failed_step"] == "_remove"
        assert state["last_transaction"]["rollback"] == "skipped"

    def test_remove_worktree_raises_on_real_failure_via_transaction(self):
        # AC2 (transaction path): the same real-failure check applies when
        # remove_worktree is driven through teardown_worktree.
        branch = "loop/real-failure/1"
        wt_path = self._make_worktree(branch)

        orig_run = gitops._run

        def failing_run(workdir, args, timeout=gitops.DEFAULT_GIT_TIMEOUT):
            if args[:2] == ["worktree", "remove"]:
                return "", "fatal: simulated failure", 1
            return orig_run(workdir, args, timeout=timeout)

        gitops._run = failing_run
        try:
            with pytest.raises(gitops.GitError):
                gitops.teardown_worktree(self.tmpdir, wt_path, branch)
        finally:
            gitops._run = orig_run

    def test_worktree_transaction_retry_converges(self):
        # AC3: a transient remove failure leaves the worktree and branch
        # intact; retrying teardown_worktree once the failure clears
        # converges to a fully clean state.
        branch = "loop/retry/1"
        wt_path = self._make_worktree(branch)

        orig_run = gitops._run
        fail_once = {"triggered": False}

        def flaky_run(workdir, args, timeout=gitops.DEFAULT_GIT_TIMEOUT):
            if args[:2] == ["worktree", "remove"] and not fail_once["triggered"]:
                fail_once["triggered"] = True
                return "", "simulated transient failure", 1
            return orig_run(workdir, args, timeout=timeout)

        gitops._run = flaky_run
        try:
            with pytest.raises(gitops.GitError, match="simulated transient failure"):
                gitops.teardown_worktree(self.tmpdir, wt_path, branch)

            assert os.path.isdir(wt_path)
            assert gitops.branch_exists(self.tmpdir, branch)

            # Retry after the transient failure clears.
            gitops.teardown_worktree(self.tmpdir, wt_path, branch)
        finally:
            gitops._run = orig_run

        assert not os.path.exists(wt_path)
        assert not gitops.branch_exists(self.tmpdir, branch)

    def test_worktree_transaction_converges_when_dir_deleted_out_of_band(self):
        # A1: the worktree directory can go missing without git's metadata
        # being updated (e.g. an out-of-band `shutil.rmtree`). remove_worktree
        # must still recognize the registered-but-dirless worktree and remove
        # it instead of silently no-op'ing, or delete_branch fails forever
        # with "used by worktree" and retries never converge (R1).
        branch = "loop/stale/1"
        wt_path = self._make_worktree(branch)

        shutil.rmtree(wt_path)

        gitops.teardown_worktree(self.tmpdir, wt_path, branch)
        assert not gitops.branch_exists(self.tmpdir, branch)
        wt_list, _, _ = _git(self.tmpdir, "worktree", "list")
        assert wt_path not in wt_list

        # A retry (in case any user calls it twice) stays a clean no-op.
        gitops.teardown_worktree(self.tmpdir, wt_path, branch)
        wt_list, _, _ = _git(self.tmpdir, "worktree", "list")
        assert wt_path not in wt_list


def _setup_conflict(workdir):
    """Build a repo where cherry-picking one ``feature`` commit onto ``main``
    conflicts, while a later ``feature`` commit cherry-picks cleanly.

    Returns ``(conflicting_sha, safe_sha, main_head_before_pick)``:

    - *conflicting_sha*: edits the same line of ``f.txt`` that *main* also
      edited, so applying it onto *main* conflicts.
    - *safe_sha*: a later commit on ``feature`` that only adds ``g.txt``,
      independent of ``f.txt`` — always applies cleanly onto *main*
      regardless of ``f.txt``'s state, useful for exercising a retry that
      must converge after a *conflicting_sha* attempt is aborted.
    """
    gitops.auto_init(workdir)
    _write(workdir, "f.txt", "base\n")
    gitops.commit_all(workdir, "base")
    gitops.create_branch(workdir, "feature", "main")

    gitops.checkout(workdir, "feature")
    _write(workdir, "f.txt", "feature change\n")
    gitops.commit_all(workdir, "feature change")
    conflicting_sha = gitops.head_sha(workdir)

    _write(workdir, "g.txt", "new file\n")
    gitops.commit_all(workdir, "add g.txt")
    safe_sha = gitops.head_sha(workdir)

    gitops.checkout(workdir, "main")
    _write(workdir, "f.txt", "main change\n")
    gitops.commit_all(workdir, "main change")
    main_head = gitops.head_sha(workdir)

    return conflicting_sha, safe_sha, main_head


class TestCherryPickTransaction:
    """cherry_pick: apply + abort-on-failure as one transaction (P12)."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cherry_pick_transaction_atomic(self):
        # AC1: apply fails (real conflict), abort runs and succeeds, leaving
        # a clean state: no CHERRY_PICK_HEAD, HEAD/index restored.
        conflicting_sha, _safe_sha, main_head = _setup_conflict(self.tmpdir)

        state = {}
        with pytest.raises(gitops.GitError, match="cherry-pick"):
            gitops.cherry_pick(self.tmpdir, conflicting_sha, state=state)

        _out, _err, rc = _git(
            self.tmpdir, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"
        )
        assert rc != 0
        assert gitops.head_sha(self.tmpdir) == main_head
        status_out, _err, _rc = _git(self.tmpdir, "status", "--porcelain")
        assert status_out == ""

        assert "warnings" not in state
        assert state["last_transaction"]["outcome"] == "failed"
        assert state["last_transaction"]["rollback"] == "executed"

    def test_cherry_pick_abort_failure_recoverable(self):
        # AC2: abort fails (simulated), leaving mid-merge state (CHERRY_PICK_HEAD,
        # branch) intact; a retry clears that leftover partial merge and
        # re-enters apply logic, converging to a clean apply.
        conflicting_sha, safe_sha, _main_head = _setup_conflict(self.tmpdir)

        orig_run = gitops._run

        def failing_abort(workdir, args, timeout=gitops.DEFAULT_GIT_TIMEOUT):
            if args[:2] == ["cherry-pick", "--abort"]:
                return "", "simulated abort failure", 1
            return orig_run(workdir, args, timeout=timeout)

        gitops._run = failing_abort
        state = {}
        try:
            with pytest.raises(gitops.GitError, match="cherry-pick"):
                gitops.cherry_pick(self.tmpdir, conflicting_sha, state=state)
        finally:
            gitops._run = orig_run

        # Mid-merge state left intact: partial merge and branch untouched.
        _out, _err, rc = _git(
            self.tmpdir, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"
        )
        assert rc == 0
        assert gitops.get_current_branch(self.tmpdir) == "main"
        assert gitops.branch_exists(self.tmpdir, "feature")

        # Retry (abort no longer simulated-failing) with a non-conflicting
        # commit: clears the stale partial merge, then re-enters apply logic
        # and converges to a clean apply.
        gitops.cherry_pick(self.tmpdir, safe_sha, state=state)

        _out, _err, rc = _git(
            self.tmpdir, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"
        )
        assert rc != 0
        status_out, _err, _rc = _git(self.tmpdir, "status", "--porcelain")
        assert status_out == ""
        assert _read(self.tmpdir, "g.txt") == "new file\n"
        assert state["last_transaction"]["outcome"] == "success"

    def test_cherry_pick_abort_failure_warns(self):
        # AC3: abort's non-zero exit is checked, not silently swallowed — a
        # warning is appended to state["warnings"] instead.
        conflicting_sha, _safe_sha, _main_head = _setup_conflict(self.tmpdir)

        orig_run = gitops._run

        def failing_abort(workdir, args, timeout=gitops.DEFAULT_GIT_TIMEOUT):
            if args[:2] == ["cherry-pick", "--abort"]:
                return "", "simulated abort failure", 1
            return orig_run(workdir, args, timeout=timeout)

        gitops._run = failing_abort
        state = {}
        try:
            with pytest.raises(gitops.GitError, match="cherry-pick"):
                gitops.cherry_pick(self.tmpdir, conflicting_sha, state=state)
        finally:
            gitops._run = orig_run

        assert len(state["warnings"]) == 1
        assert "simulated abort failure" in state["warnings"][0]
        assert state["last_transaction"]["outcome"] == "failed"
        assert state["last_transaction"]["rollback"] == "error"

    def test_cherry_pick_retry_raises_when_stale_merge_cannot_be_cleared(self):
        # R1: if the first call's rollback abort fails AND the retry's own
        # cleanup abort (clearing that same stale partial merge) also fails,
        # the retry must not proceed to attempt the new apply against the
        # unresolved index — that would just fail with an unrelated
        # "already in progress" error and mask the real cause. Unlike
        # test_cherry_pick_abort_failure_recoverable, _run stays patched
        # (failing) across the retry so this failure mode is actually
        # exercised instead of being reset away.
        conflicting_sha, safe_sha, _main_head = _setup_conflict(self.tmpdir)

        orig_run = gitops._run

        def failing_abort(workdir, args, timeout=gitops.DEFAULT_GIT_TIMEOUT):
            if args[:2] == ["cherry-pick", "--abort"]:
                return "", "simulated abort failure", 1
            return orig_run(workdir, args, timeout=timeout)

        gitops._run = failing_abort
        try:
            state = {}
            with pytest.raises(gitops.GitError, match="cherry-pick"):
                gitops.cherry_pick(self.tmpdir, conflicting_sha, state=state)

            # Retry with the cleanup abort still failing: must raise about
            # the cleanup failure itself, not silently attempt the apply.
            retry_state = {}
            with pytest.raises(
                gitops.GitError, match="stale partial merge"
            ):
                gitops.cherry_pick(self.tmpdir, safe_sha, state=retry_state)

            # The partial merge is still intact — neither call cleared it.
            _out, _err, rc = _git(
                self.tmpdir, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"
            )
            assert rc == 0
        finally:
            gitops._run = orig_run

        # Now that the abort genuinely works again, a further retry
        # converges to a clean apply, confirming the repo was never
        # corrupted by the earlier fail-fast path.
        gitops.cherry_pick(self.tmpdir, safe_sha, state={})
        _out, _err, rc = _git(
            self.tmpdir, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"
        )
        assert rc != 0
        status_out, _err, _rc = _git(self.tmpdir, "status", "--porcelain")
        assert status_out == ""


class TestRunTransaction:
    """run_transaction: atomic step execution with rollback + transaction log."""

    def test_transaction_runs_all_steps_and_logs(self):
        # AC1: step1 succeeds, step2 fails, rollback runs, log records all.
        calls = []

        def step_one():
            calls.append("step_one")

        def step_two():
            calls.append("step_two")
            raise RuntimeError("boom")

        rollback_calls = []

        def rollback():
            rollback_calls.append("rollback")

        log, outcome = gitops.run_transaction(
            [step_one, step_two], rollback, label="build",
        )

        # Steps ran in order until the failure; rollback ran after it.
        assert calls == ["step_one", "step_two"]
        assert rollback_calls == ["rollback"]

        assert [entry["step"] for entry in log] == [
            "step_one", "step_two", "rollback",
        ]
        assert log[0] == {
            "step": "step_one", "status": "success", "detail": "",
        }
        assert log[1]["step"] == "step_two"
        assert log[1]["status"] == "failed"
        assert "RuntimeError: boom" in log[1]["detail"]
        assert log[2] == {
            "step": "rollback", "status": "executed", "detail": "",
        }

        assert outcome["outcome"] == "failed"
        assert outcome["label"] == "build"
        assert outcome["failed_step"] == "step_two"
        assert outcome["rollback"] == "executed"

    def test_transaction_no_rollback_on_all_success(self):
        # AC2: all steps pass, rollback never called, log shows all succeeded.
        rollback_calls = []

        def rollback():
            rollback_calls.append("rollback")

        def step_one():
            pass

        def step_two():
            pass

        log, outcome = gitops.run_transaction(
            [step_one, step_two], rollback, label="ok",
        )

        assert rollback_calls == []
        assert [entry["step"] for entry in log] == ["step_one", "step_two"]
        assert all(entry["status"] == "success" for entry in log)
        assert outcome["outcome"] == "success"
        assert outcome["label"] == "ok"
        assert "failed_step" not in outcome
        assert "rollback" not in outcome

    def test_transaction_appends_log_to_state(self):
        # R2: the transaction log is appended to run diagnostics (state).
        state = {}

        def boom():
            raise ValueError("nope")

        log, outcome = gitops.run_transaction(
            [boom], lambda: None, label="t", state=state,
        )

        assert state["transactions"] == log
        assert state["last_transaction"] == outcome

    def test_transaction_records_workdir_from_scope(self):
        # R1: the helper is contextual — the bound workdir is recorded.
        with gitops.transaction_scope("/repo/path"):
            _log, outcome = gitops.run_transaction(
                [lambda: None], None, label="scoped",
            )
        assert outcome["workdir"] == "/repo/path"

    def test_transaction_non_reentrant(self):
        # R1: calling run_transaction while one is in flight raises GitError.
        gitops._transaction_ctx.active = True
        gitops._transaction_ctx.label = "outer"
        try:
            with pytest.raises(gitops.GitError, match="non-reentrant"):
                gitops.run_transaction([lambda: None], None, label="inner")
        finally:
            gitops._transaction_ctx.active = False
            gitops._transaction_ctx.label = None

    def test_transaction_rollback_error_does_not_mask_step_failure(self):
        # A failing rollback is recorded, not raised over the original failure.
        def step():
            raise RuntimeError("step blew up")

        def rollback():
            raise OSError("rollback also blew up")

        log, outcome = gitops.run_transaction([step], rollback, label="messy")

        assert log[0]["status"] == "failed"
        assert log[1]["step"] == "rollback"
        assert log[1]["status"] == "error"
        assert "OSError" in log[1]["detail"]
        assert outcome["outcome"] == "failed"
        assert outcome["rollback"] == "error"

    def test_transaction_keyboardinterrupt_rolls_back_records_and_reraises(self):
        # A1: an interruption must still run rollback, leave diagnostics in
        # state, and then propagate so the run is not left half-applied.
        import json
        from pathlib import Path

        rollback_calls = []

        def step():
            raise KeyboardInterrupt("ctrl-c")

        def rollback():
            rollback_calls.append(True)

        state = {}
        with pytest.raises(KeyboardInterrupt):
            gitops.run_transaction(
                [step], rollback, label="kbi", state=state,
            )

        assert rollback_calls == [True]
        assert state["last_transaction"]["outcome"] == "failed"
        assert state["last_transaction"]["failed_step"] == "step"
        assert state["last_transaction"]["rollback"] == "executed"
        # outcome is JSON-serializable (also covers A2 for the failure path).
        json.dumps(state["last_transaction"])

    def test_transaction_path_workdir_is_json_serializable(self):
        # A2: a pathlib.Path bound via transaction_scope must not leak a
        # PosixPath into state, where json.dumps has no custom encoder.
        import json
        from pathlib import Path

        state = {}
        with gitops.transaction_scope(Path("/repo/path")):
            _log, outcome = gitops.run_transaction(
                [lambda: None], None, label="scoped", state=state,
            )

        assert outcome["workdir"] == "/repo/path"
        assert isinstance(outcome["workdir"], str)
        # state["last_transaction"] survives the project's plain json.dumps.
        json.dumps(state["last_transaction"])

