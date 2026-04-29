"""Tests for scripts/update.py — self-update via git pull --ff-only.

Mirrors compathy/tests/test_update.py: spin up real git repos in tempdirs and
exercise every branch (skipped, already-current, updated, failed-diverge,
dirty-tree, no-remote, not-a-repo).
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import update  # noqa: E402  pylint: disable=wrong-import-position


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


def _make_repo(root: Path, version: str = "0.1.0") -> None:
    _run(["git", "init", "-q", "-b", "main"], root)
    _run(["git", "config", "user.email", "t@t.com"], root)
    _run(["git", "config", "user.name", "t"], root)
    _run(["git", "config", "commit.gpgsign", "false"], root)
    (root / "VERSION").write_text(f"{version}\n")
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-q", "-m", "init"], root)


class TestReadVersion(unittest.TestCase):
    def test_reads_actual_version_file(self):
        v = update._read_version()
        # Real VERSION file in the worktree.
        self.assertNotEqual(v, "unknown")
        self.assertRegex(v, r"^\d+\.\d+\.\d+")

    def test_missing_version_returns_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(update, "REPO_ROOT", Path(td)):
                self.assertEqual(update._read_version(), "unknown")


class TestUpdateLogic(unittest.TestCase):
    """Test update branches with real git repos in tempdirs."""

    def test_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "VERSION").write_text("0.1.0\n")
            with patch.object(update, "REPO_ROOT", root):
                result = update.update()
            self.assertEqual(result["action"], "skipped")
            self.assertIn("not a git repo", result["message"])

    def test_no_remote(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_repo(root)
            with patch.object(update, "REPO_ROOT", root):
                result = update.update()
            self.assertEqual(result["action"], "skipped")
            self.assertIn("no git remote", result["message"])

    def test_dirty_tree_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))
            # Make the tree dirty.
            (clone / "VERSION").write_text("0.1.0-dirty\n")
            with patch.object(update, "REPO_ROOT", clone):
                result = update.update()
            self.assertEqual(result["action"], "skipped")
            self.assertIn("local changes", result["message"])

    def test_already_current(self):
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))
            with patch.object(update, "REPO_ROOT", clone):
                result = update.update()
            self.assertEqual(result["action"], "already-current")
            self.assertIn("up to date", result["message"])

    def test_pulls_new_version(self):
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))

            # Bump origin.
            (origin / "VERSION").write_text("0.2.0\n")
            _run(["git", "add", "VERSION"], origin)
            _run(["git", "commit", "-q", "-m", "bump"], origin)

            with patch.object(update, "REPO_ROOT", clone):
                result = update.update()
            self.assertEqual(result["action"], "updated")
            self.assertEqual(result["old_version"], "0.1.0")
            self.assertEqual(result["new_version"], "0.2.0")
            self.assertIn("0.1.0", result["message"])
            self.assertIn("0.2.0", result["message"])

    def test_updated_without_version_bump(self):
        """git pull succeeds but VERSION file unchanged."""
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))

            # Add a non-VERSION change to origin.
            (origin / "README.md").write_text("hi\n")
            _run(["git", "add", "README.md"], origin)
            _run(["git", "commit", "-q", "-m", "docs"], origin)

            with patch.object(update, "REPO_ROOT", clone):
                result = update.update()
            self.assertEqual(result["action"], "updated")
            self.assertEqual(result["old_version"], "0.1.0")
            self.assertEqual(result["new_version"], "0.1.0")
            self.assertIn("no version bump", result["message"])

    def test_diverged_fails_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))
            _run(["git", "config", "user.email", "t@t.com"], clone)
            _run(["git", "config", "user.name", "t"], clone)
            _run(["git", "config", "commit.gpgsign", "false"], clone)

            # Diverge: commits on both sides.
            (origin / "a.txt").write_text("o")
            _run(["git", "add", "."], origin)
            _run(["git", "commit", "-q", "-m", "o"], origin)
            (clone / "b.txt").write_text("l")
            _run(["git", "add", "."], clone)
            _run(["git", "commit", "-q", "-m", "l"], clone)

            with patch.object(update, "REPO_ROOT", clone):
                result = update.update()
            self.assertEqual(result["action"], "failed")

    def test_fetch_failure_is_handled(self):
        """If git fetch raises, we fail gracefully."""
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))

            real_git = update._git

            def _fail_fetch(args, cwd):
                if args and args[0] == "fetch":
                    raise subprocess.SubprocessError("network down")
                return real_git(args, cwd)

            with patch.object(update, "REPO_ROOT", clone), \
                 patch.object(update, "_git", _fail_fetch):
                result = update.update()
            self.assertEqual(result["action"], "failed")
            self.assertIn("git fetch failed", result["message"])

    def test_pull_subprocess_error_is_handled(self):
        """Pull raising a SubprocessError is reported as failed."""
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))
            # Force "behind upstream" so we reach the pull branch.
            (origin / "x.txt").write_text("x")
            _run(["git", "add", "."], origin)
            _run(["git", "commit", "-q", "-m", "x"], origin)

            real_git = update._git

            def _fail_pull(args, cwd):
                if args and args[0] == "pull":
                    raise subprocess.SubprocessError("boom")
                return real_git(args, cwd)

            with patch.object(update, "REPO_ROOT", clone), \
                 patch.object(update, "_git", _fail_pull):
                result = update.update()
            self.assertEqual(result["action"], "failed")
            self.assertIn("git pull failed", result["message"])


# ---------------------------------------------------------------------------
# main() must always return 0.
# ---------------------------------------------------------------------------


class TestMainExit(unittest.TestCase):
    def test_main_zero_on_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "VERSION").write_text("0.1.0\n")
            captured = io.StringIO()
            with patch.object(update, "REPO_ROOT", Path(td)), \
                 patch.object(sys, "stderr", captured):
                rc = update.main()
            self.assertEqual(rc, 0)
            self.assertIn("not a git repo", captured.getvalue())

    def test_main_zero_on_already_current(self):
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))
            captured = io.StringIO()
            with patch.object(update, "REPO_ROOT", clone), \
                 patch.object(sys, "stdout", captured):
                rc = update.main()
            self.assertEqual(rc, 0)
            self.assertIn("up to date", captured.getvalue())

    def test_main_zero_on_updated(self):
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))
            (origin / "VERSION").write_text("0.2.0\n")
            _run(["git", "add", "VERSION"], origin)
            _run(["git", "commit", "-q", "-m", "bump"], origin)

            captured = io.StringIO()
            with patch.object(update, "REPO_ROOT", clone), \
                 patch.object(sys, "stdout", captured):
                rc = update.main()
            self.assertEqual(rc, 0)
            self.assertIn("0.2.0", captured.getvalue())

    def test_main_zero_on_failed(self):
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            _make_repo(origin)
            _run(["git", "clone", "-q", str(origin), str(clone)], Path(td))
            _run(["git", "config", "user.email", "t@t.com"], clone)
            _run(["git", "config", "user.name", "t"], clone)
            _run(["git", "config", "commit.gpgsign", "false"], clone)
            (origin / "a.txt").write_text("o")
            _run(["git", "add", "."], origin)
            _run(["git", "commit", "-q", "-m", "o"], origin)
            (clone / "b.txt").write_text("l")
            _run(["git", "add", "."], clone)
            _run(["git", "commit", "-q", "-m", "l"], clone)

            captured = io.StringIO()
            with patch.object(update, "REPO_ROOT", clone), \
                 patch.object(sys, "stderr", captured):
                rc = update.main()
            self.assertEqual(rc, 0)
            self.assertIn("WARNING", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
