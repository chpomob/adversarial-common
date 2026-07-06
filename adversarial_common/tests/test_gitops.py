"""Tests for adversarial_common.gitops — exercise every public function against
a throwaway git repo created per-test."""

import os
import shutil
import subprocess
import tempfile

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

    def test_auto_init_sets_identity(self):
        gitops.auto_init(self.tmpdir)
        name, _, _ = _git(self.tmpdir, "config", "user.name")
        email, _, _ = _git(self.tmpdir, "config", "user.email")
        assert name == "adversarial-loop"
        assert email == "loop@adversarial.local"

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
        assert ref == "stash@{0}"
        assert _read(self.tmpdir, "f.txt") == "v1"
        assert not gitops.is_dirty(self.tmpdir)

        gitops.unstash(self.tmpdir, ref)
        assert _read(self.tmpdir, "f.txt") == "v2"

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

    def test_ensure_gitignore_already_present(self):
        gitops.ensure_gitignore(self.tmpdir, "*.log")
        gitops.ensure_gitignore(self.tmpdir, "*.log")
        with open(os.path.join(self.tmpdir, ".gitignore")) as fh:
            content = fh.read()
        assert content.count("*.log") == 1
