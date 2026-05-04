"""Tests for the auto-heal threshold trigger (lane-p).

Covers the hook_runner side (counting entries, sniffing the heal lock,
spawning the trigger subprocess, never raising) and the heal.py side
(offset reset on write success, auto-heal-trigger sentinel).

Critical invariant (carried over from test_hook_runner): the hook MUST
NEVER raise. Every test verifies exit 0 even on contrived failure paths.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Make scripts/ importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import hook_runner  # noqa: E402
import heal  # noqa: E402
import persona  # noqa: E402


class AutoHealThresholdTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.aq_home = Path(self._tmp.name) / "ai-quickstart"
        self.aq_home.mkdir()
        env_patch = mock.patch.dict(
            os.environ, {"AI_QUICKSTART_HOME": str(self.aq_home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self.projects_path = self.aq_home / "managed-projects.json"
        self.persona_dir = self.aq_home / "persona"
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        self.activity_path = self.persona_dir / "activity.jsonl"
        self.lock_path = self.persona_dir / ".heal.lock"
        self.last_heal_path = self.persona_dir / ".last-heal.json"
        self.heal_pending_path = self.persona_dir / ".heal-pending"

        self.cwd = "/path/to/managed-project"
        self._write_registry([self.cwd])

    # --------------------------------------------------------------
    # helpers
    # --------------------------------------------------------------
    def _write_registry(self, paths):
        with open(self.projects_path, "w") as fh:
            json.dump(paths, fh)

    def _seed_activity_lines(self, n: int) -> None:
        """Write ``n`` synthetic activity.jsonl entries directly (bypass hook)."""
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        with open(self.activity_path, "a", encoding="utf-8") as fh:
            for i in range(n):
                fh.write(
                    json.dumps(
                        {"ts": "2026-05-02T12:00:00Z", "event": "tool_use",
                         "tool": "T", "cwd": self.cwd, "i": i},
                    )
                    + "\n"
                )

    def _run_hook_with(self, payload: dict) -> int:
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            return hook_runner.main()

    # --------------------------------------------------------------
    # Below threshold -> no trigger
    # --------------------------------------------------------------
    def test_below_threshold_no_trigger(self):
        # Pre-seed THRESHOLD-2 lines so this single hook fire stays under.
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD - 2)
        with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        spawn.assert_not_called()

    # --------------------------------------------------------------
    # At/above threshold -> trigger
    # --------------------------------------------------------------
    def test_at_threshold_triggers_heal(self):
        # Pre-seed THRESHOLD-1 lines. The hook's append makes it exactly THRESHOLD.
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD - 1)
        with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        spawn.assert_called_once()

    def test_above_threshold_triggers_heal(self):
        # Pre-seed well over the threshold (no prior heal -> offset 0).
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 5)
        with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        spawn.assert_called_once()

    # --------------------------------------------------------------
    # Heal in progress -> don't double-trigger; counter not reset
    # --------------------------------------------------------------
    def test_heal_in_progress_skips_trigger(self):
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 5)
        # Acquire the heal flock from the test process so the hook's
        # non-blocking sniff sees it as held.
        import fcntl
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
                rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
            self.assertEqual(rc, 0)
            spawn.assert_not_called()
            # Counter (last-heal state file) must NOT have been reset.
            self.assertFalse(
                self.last_heal_path.exists(),
                "last-heal state should not be written by the hook",
            )
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    # --------------------------------------------------------------
    # Counter resets after successful heal
    # --------------------------------------------------------------
    def test_counter_resets_after_successful_heal_write(self):
        # Build the bare minimum persona state for heal.cmd_write to succeed.
        # We piggyback on persona helpers used elsewhere in the suite.
        ppath = self.persona_dir / "persona.md"
        fm = persona.default_persona()
        fm["identity"]["role"] = "data engineer"
        fm["identity"]["industry"] = "fintech"
        fm["identity"]["archetype"] = "job"
        persona.write_persona(ppath, fm, "first prose\n")

        # Seed plenty of activity entries -- enough to trigger once.
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 3)
        size_before_heal = self.activity_path.stat().st_size

        # Drive heal.cmd_write directly with a stub stdin so we don't need
        # the LLM pipeline.
        rc = heal.cmd_write(
            home=self.aq_home,
            stdin=io.StringIO("rewritten prose\n"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(rc, 0)

        # State file should now exist with offset == file size at heal time.
        self.assertTrue(self.last_heal_path.exists())
        state = json.loads(self.last_heal_path.read_text(encoding="utf-8"))
        self.assertEqual(state["offset"], size_before_heal)

        # The hook's "entries since last heal" count should be 0 right
        # after heal succeeded -- next hook fire stays below threshold.
        with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        spawn.assert_not_called()

    def test_counter_uses_offset_to_filter_old_entries(self):
        # Pre-seed many entries.
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 5)
        # Manually record a "last heal" offset at the current size, simulating
        # a prior heal that consumed everything so far.
        self.last_heal_path.write_text(
            json.dumps(
                {"offset": self.activity_path.stat().st_size, "ts": "2026-01-01T00:00:00Z"}
            ),
            encoding="utf-8",
        )
        # One more append from the hook should NOT trigger (only 1 since last heal).
        with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        spawn.assert_not_called()

    # --------------------------------------------------------------
    # Hook never raises if subprocess spawn fails
    # --------------------------------------------------------------
    def test_subprocess_spawn_failure_does_not_crash_hook(self):
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 5)
        with mock.patch(
            "hook_runner.subprocess.Popen", side_effect=OSError("EAGAIN")
        ):
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)

    def test_missing_heal_script_is_no_op(self):
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 5)
        # Point heal_script_path at a path that doesn't exist.
        nonexistent = Path(self._tmp.name) / "no" / "such" / "heal.py"
        with mock.patch.object(
            hook_runner, "_heal_script_path", return_value=nonexistent
        ), mock.patch("hook_runner.subprocess.Popen") as popen:
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        popen.assert_not_called()

    def test_corrupt_last_heal_state_falls_back_to_zero(self):
        # Corrupt state file -> read returns 0 -> count covers full file.
        self.last_heal_path.write_text("{not json", encoding="utf-8")
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 1)
        with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        spawn.assert_called_once()

    def test_offset_beyond_file_size_falls_back_to_zero(self):
        # Activity file shrank (rotation). Offset > size -> count from 0.
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 1)
        bogus_offset = self.activity_path.stat().st_size + 10_000
        self.last_heal_path.write_text(
            json.dumps({"offset": bogus_offset, "ts": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
        with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
            rc = self._run_hook_with({"cwd": self.cwd, "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        spawn.assert_called_once()

    def test_unmanaged_cwd_does_not_trigger(self):
        # cwd not in registry: hook returns early, no trigger ever.
        self._write_registry(["/some/other/path"])
        self._seed_activity_lines(hook_runner.AUTO_HEAL_THRESHOLD + 5)
        with mock.patch.object(hook_runner, "_spawn_heal_trigger") as spawn:
            rc = self._run_hook_with({"cwd": "/not/managed", "tool_name": "Edit"})
        self.assertEqual(rc, 0)
        spawn.assert_not_called()

    # --------------------------------------------------------------
    # heal.py: auto-heal-trigger subcommand writes the sentinel
    # --------------------------------------------------------------
    def test_auto_heal_trigger_writes_sentinel(self):
        rc = heal.cmd_auto_heal_trigger(
            home=self.aq_home,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(rc, 0)
        self.assertTrue(self.heal_pending_path.exists())
        payload = json.loads(self.heal_pending_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["reason"], "auto-heal-threshold")
        self.assertIn("ts", payload)

    def test_heal_write_clears_pending_sentinel(self):
        # Drop a sentinel, run heal write, sentinel should be cleared.
        ppath = self.persona_dir / "persona.md"
        fm = persona.default_persona()
        fm["identity"]["role"] = "engineer"
        fm["identity"]["industry"] = "fintech"
        fm["identity"]["archetype"] = "job"
        persona.write_persona(ppath, fm, "prose v0\n")
        self.heal_pending_path.write_text(
            json.dumps({"ts": "2026-01-01T00:00:00Z", "reason": "auto-heal-threshold"}),
            encoding="utf-8",
        )
        rc = heal.cmd_write(
            home=self.aq_home,
            stdin=io.StringIO("new prose\n"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(rc, 0)
        self.assertFalse(self.heal_pending_path.exists())


class AutoHealHelperUnitTests(unittest.TestCase):
    """Direct unit tests for hook_runner helpers (no env override needed)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.aq_home = Path(self._tmp.name) / "ai-quickstart"
        (self.aq_home / "persona").mkdir(parents=True)
        env_patch = mock.patch.dict(
            os.environ, {"AI_QUICKSTART_HOME": str(self.aq_home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_count_entries_since_offset_basic(self):
        path = self.aq_home / "persona" / "activity.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(7):
                fh.write(json.dumps({"i": i}) + "\n")
        # Offset 0 -> 7 lines.
        self.assertEqual(hook_runner._count_entries_since_offset(path, 0), 7)
        # Offset past EOF -> falls back to 0 -> 7 lines.
        self.assertEqual(
            hook_runner._count_entries_since_offset(path, 1_000_000), 7
        )
        # Offset == size -> 0 lines.
        size = path.stat().st_size
        self.assertEqual(hook_runner._count_entries_since_offset(path, size), 0)

    def test_count_entries_missing_file_returns_zero(self):
        path = self.aq_home / "persona" / "no-such.jsonl"
        self.assertEqual(hook_runner._count_entries_since_offset(path, 0), 0)

    def test_read_last_heal_offset_handles_missing_corrupt_and_bad_types(self):
        # Missing file -> 0
        self.assertEqual(hook_runner._read_last_heal_offset(), 0)
        state = self.aq_home / "persona" / ".last-heal.json"
        # Corrupt -> 0
        state.write_text("{ not json", encoding="utf-8")
        self.assertEqual(hook_runner._read_last_heal_offset(), 0)
        # Wrong type at top level -> 0
        state.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        self.assertEqual(hook_runner._read_last_heal_offset(), 0)
        # Negative offset -> 0
        state.write_text(json.dumps({"offset": -5}), encoding="utf-8")
        self.assertEqual(hook_runner._read_last_heal_offset(), 0)
        # Non-int offset -> 0
        state.write_text(json.dumps({"offset": "huh"}), encoding="utf-8")
        self.assertEqual(hook_runner._read_last_heal_offset(), 0)
        # Valid int offset -> echoed back
        state.write_text(json.dumps({"offset": 42}), encoding="utf-8")
        self.assertEqual(hook_runner._read_last_heal_offset(), 42)


if __name__ == "__main__":
    unittest.main()
