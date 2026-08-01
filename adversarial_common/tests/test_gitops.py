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
