"""Tests for scripts/paths.py — layout helpers, run-id, sync detection."""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import paths  # noqa: E402  pylint: disable=wrong-import-position


# ---------------------------------------------------------------------------
# Layout helpers — exercise every public function with a tmp HOME.
# ---------------------------------------------------------------------------


class TestLayoutHelpers(unittest.TestCase):
    def test_home_root_uses_default_home(self):
        # Default Path.home(): just verify suffix.
        root = paths.home_root()
        self.assertTrue(str(root).endswith("/.ai-quickstart"))

    def test_home_root_with_explicit_home(self):
        root = paths.home_root(home=Path("/tmp/fake-home"))
        self.assertEqual(root, Path("/tmp/fake-home/.ai-quickstart"))

    def test_persona_paths(self):
        h = Path("/tmp/h")
        self.assertEqual(paths.persona_dir(h), h / ".ai-quickstart" / "persona")
        self.assertEqual(paths.persona_path(h),
                         h / ".ai-quickstart" / "persona" / "persona.md")
        self.assertEqual(paths.persona_backup_path(h),
                         h / ".ai-quickstart" / "persona" / "persona.md.bak")
        self.assertEqual(paths.activity_path(h),
                         h / ".ai-quickstart" / "persona" / "activity.jsonl")
        self.assertEqual(paths.activity_summary_path(h),
                         h / ".ai-quickstart" / "persona" / "activity-summary.json")
        self.assertEqual(paths.anecdotes_dir(h),
                         h / ".ai-quickstart" / "persona" / "anecdotes")
        self.assertEqual(paths.heal_lock_path(h),
                         h / ".ai-quickstart" / "persona" / ".heal.lock")

    def test_runs_paths(self):
        h = Path("/tmp/h")
        self.assertEqual(paths.runs_dir(h), h / ".ai-quickstart" / "runs")
        self.assertEqual(paths.run_dir("abc-123", h),
                         h / ".ai-quickstart" / "runs" / "abc-123")

    def test_cache_paths(self):
        h = Path("/tmp/h")
        self.assertEqual(paths.cache_dir(h), h / ".ai-quickstart" / "cache")
        self.assertEqual(paths.github_cache_dir(h),
                         h / ".ai-quickstart" / "cache" / "github")
        self.assertEqual(paths.mcpmarket_cache_dir(h),
                         h / ".ai-quickstart" / "cache" / "mcpmarket")

    def test_top_level_files(self):
        h = Path("/tmp/h")
        self.assertEqual(paths.managed_projects_path(h),
                         h / ".ai-quickstart" / "managed-projects.json")
        self.assertEqual(paths.installed_hooks_path(h),
                         h / ".ai-quickstart" / "installed-hooks.json")
        self.assertEqual(paths.heal_errors_path(h),
                         h / ".ai-quickstart" / "heal-errors.jsonl")
        self.assertEqual(paths.config_path(h),
                         h / ".ai-quickstart" / "config.json")


# ---------------------------------------------------------------------------
# Directory creation.
# ---------------------------------------------------------------------------


class TestEnsureDirs(unittest.TestCase):
    def test_creates_full_tree(self, tmp_path=None):
        # Use tmp_path-like via TemporaryDirectory for unittest compatibility.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            result = paths.ensure_dirs(home=home)
            self.assertTrue((home / ".ai-quickstart").is_dir())
            self.assertTrue((home / ".ai-quickstart" / "persona").is_dir())
            self.assertTrue((home / ".ai-quickstart" / "persona" / "anecdotes").is_dir())
            self.assertTrue((home / ".ai-quickstart" / "runs").is_dir())
            self.assertTrue((home / ".ai-quickstart" / "cache" / "github").is_dir())
            self.assertTrue((home / ".ai-quickstart" / "cache" / "mcpmarket").is_dir())
            # All targets reported.
            total = len(result["created"]) + len(result["existing"])
            self.assertEqual(total, 7)
            # First call: most are created.
            self.assertGreater(len(result["created"]), 0)

    def test_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            paths.ensure_dirs(home=home)
            second = paths.ensure_dirs(home=home)
            self.assertEqual(second["created"], [])
            self.assertEqual(len(second["existing"]), 7)

    def test_ensure_run_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            paths.ensure_dirs(home=home)
            rd = paths.ensure_run_dir("20260429T120000Z-deadbeef", home=home)
            self.assertTrue(rd.is_dir())
            self.assertEqual(
                rd,
                home / ".ai-quickstart" / "runs" / "20260429T120000Z-deadbeef",
            )

    def test_ensure_run_dir_creates_parents(self):
        """ensure_run_dir works even when ensure_dirs hasn't been called."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            rd = paths.ensure_run_dir("xyz", home=home)
            self.assertTrue(rd.is_dir())


# ---------------------------------------------------------------------------
# Run-id generation.
# ---------------------------------------------------------------------------


class TestGenerateRunId(unittest.TestCase):
    RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

    def test_format(self):
        rid = paths.generate_run_id()
        self.assertRegex(rid, self.RUN_ID_RE)

    def test_deterministic_with_fixed_now(self):
        ts = _dt.datetime(2026, 4, 29, 13, 35, 0, tzinfo=_dt.timezone.utc)
        rid = paths.generate_run_id(now=ts)
        self.assertTrue(rid.startswith("20260429T133500Z-"))

    def test_naive_datetime_treated_as_utc(self):
        ts = _dt.datetime(2026, 4, 29, 13, 35, 0)  # no tzinfo
        rid = paths.generate_run_id(now=ts)
        self.assertTrue(rid.startswith("20260429T133500Z-"))

    def test_unique(self):
        ids = {paths.generate_run_id() for _ in range(50)}
        # Even at the same second, uuid suffix differs.
        self.assertEqual(len(ids), 50)

    def test_sortable(self):
        a = paths.generate_run_id(
            now=_dt.datetime(2026, 4, 29, 1, 0, 0, tzinfo=_dt.timezone.utc))
        b = paths.generate_run_id(
            now=_dt.datetime(2026, 4, 29, 2, 0, 0, tzinfo=_dt.timezone.utc))
        self.assertLess(a, b)


# ---------------------------------------------------------------------------
# Filesystem-sync detection.
# ---------------------------------------------------------------------------


class TestDetectSyncKind(unittest.TestCase):
    def test_local_path_returns_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            kind = paths.detect_sync_kind(Path(td), home=Path("/Users/fakehome"))
            # Local /tmp/xxx not under any sync provider.
            self.assertIsNone(kind)

    def test_icloud_mobile_documents(self):
        # Synthetic path that *would* be iCloud — we don't need it to exist
        # because the prefix check is purely lexical against home.
        fake_home = Path("/Users/u")
        candidate = fake_home / "Library" / "Mobile Documents" / "iCloud~com~foo" / "x"
        kind = paths.detect_sync_kind(candidate, home=fake_home)
        self.assertEqual(kind, "icloud")

    def test_icloud_cloudstorage(self):
        fake_home = Path("/Users/u")
        candidate = fake_home / "Library" / "CloudStorage" / "iCloud" / "Documents"
        kind = paths.detect_sync_kind(candidate, home=fake_home)
        self.assertEqual(kind, "icloud")

    def test_dropbox_cloudstorage(self):
        fake_home = Path("/Users/u")
        candidate = fake_home / "Library" / "CloudStorage" / "Dropbox-Personal" / "x"
        kind = paths.detect_sync_kind(candidate, home=fake_home)
        self.assertEqual(kind, "dropbox")

    def test_onedrive_cloudstorage(self):
        fake_home = Path("/Users/u")
        candidate = fake_home / "Library" / "CloudStorage" / "OneDrive-Personal" / "x"
        kind = paths.detect_sync_kind(candidate, home=fake_home)
        self.assertEqual(kind, "onedrive")

    def test_google_drive_cloudstorage(self):
        fake_home = Path("/Users/u")
        candidate = fake_home / "Library" / "CloudStorage" / "GoogleDrive-x" / "y"
        kind = paths.detect_sync_kind(candidate, home=fake_home)
        self.assertEqual(kind, "googledrive")

    def test_legacy_dropbox_home(self):
        fake_home = Path("/Users/u")
        candidate = fake_home / "Dropbox" / "code" / "proj"
        kind = paths.detect_sync_kind(candidate, home=fake_home)
        self.assertEqual(kind, "dropbox")

    def test_legacy_onedrive_home(self):
        fake_home = Path("/Users/u")
        candidate = fake_home / "OneDrive" / "x"
        kind = paths.detect_sync_kind(candidate, home=fake_home)
        self.assertEqual(kind, "onedrive")

    def test_statvfs_failure_returns_nfs(self):
        """If statvfs raises, treat as a remote/odd FS and return 'nfs'."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)

            def _raise(*_a, **_k):
                raise OSError("ENOTSUP")

            captured = io.StringIO()
            with patch.object(os, "statvfs", _raise), \
                 patch.object(sys, "stderr", captured):
                kind = paths.detect_sync_kind(target, home=Path(td))
            self.assertEqual(kind, "nfs")
            self.assertIn("statvfs failed", captured.getvalue())

    def test_warn_if_synced_emits_warning(self):
        fake_home = Path("/Users/u")
        candidate = fake_home / "Library" / "Mobile Documents" / "x"
        captured = io.StringIO()
        with patch.object(sys, "stderr", captured):
            kind = paths.warn_if_synced(candidate, home=fake_home)
        self.assertEqual(kind, "icloud")
        self.assertIn("WARNING", captured.getvalue())
        self.assertIn("icloud", captured.getvalue())

    def test_warn_if_synced_silent_for_local(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            captured = io.StringIO()
            with patch.object(sys, "stderr", captured):
                kind = paths.warn_if_synced(Path(td), home=Path("/Users/fakehome"))
            self.assertIsNone(kind)
            self.assertEqual(captured.getvalue(), "")


# ---------------------------------------------------------------------------
# CLI smoke test.
# ---------------------------------------------------------------------------


class TestCliMain(unittest.TestCase):
    def test_run_id_subcommand(self):
        captured = io.StringIO()
        with patch.object(sys, "stdout", captured):
            rc = paths._main(["--run-id"])
        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertIn("run_id", out)
        self.assertRegex(out["run_id"], TestGenerateRunId.RUN_ID_RE)

    def test_ensure_dirs_subcommand(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.object(Path, "home", classmethod(lambda cls: Path(td))):
                captured = io.StringIO()
                with patch.object(sys, "stdout", captured):
                    rc = paths._main(["--ensure-dirs"])
            self.assertEqual(rc, 0)
            out = json.loads(captured.getvalue())
            self.assertIn("ensure", out)

    def test_detect_subcommand_with_local_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            captured = io.StringIO()
            with patch.object(sys, "stdout", captured):
                rc = paths._main(["--detect", td])
            self.assertEqual(rc, 0)
            out = json.loads(captured.getvalue())
            self.assertIn("detect", out)


if __name__ == "__main__":
    unittest.main()
