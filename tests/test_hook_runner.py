"""Tests for scripts/hook_runner.py.

Critical invariant: the runner must never raise. Every test verifies exit 0.
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

# Make the scripts/ directory importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import hook_runner  # noqa: E402


class HookRunnerTestCase(unittest.TestCase):
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
        self.activity_path = self.aq_home / "persona" / "activity.jsonl"

    # -------------------------------------------------------------------
    # helpers
    # -------------------------------------------------------------------
    def _write_registry(self, paths):
        with open(self.projects_path, "w") as fh:
            json.dump(paths, fh)

    def _run_with_stdin(self, payload: str) -> int:
        with mock.patch.object(sys, "stdin", io.StringIO(payload)):
            return hook_runner.main()

    def _read_activity_lines(self):
        if not self.activity_path.exists():
            return []
        with open(self.activity_path) as fh:
            return [json.loads(line) for line in fh.read().splitlines() if line]

    # -------------------------------------------------------------------
    # match / no-match
    # -------------------------------------------------------------------
    def test_matched_cwd_writes_line(self):
        cwd = "/path/to/project"
        self._write_registry([cwd])
        event = {
            "cwd": cwd,
            "tool_name": "Edit",
            "session_id": "run-abc",
            "tool_input": {"file_path": "/path/to/project/foo.py"},
        }
        rc = self._run_with_stdin(json.dumps(event))
        self.assertEqual(rc, 0)
        lines = self._read_activity_lines()
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertEqual(rec["cwd"], cwd)
        self.assertEqual(rec["tool"], "Edit")
        self.assertEqual(rec["run_id"], "run-abc")
        self.assertEqual(rec["file"], "/path/to/project/foo.py")
        self.assertIn("ts", rec)
        self.assertEqual(rec["event"], "tool_use")

    def test_unmatched_cwd_no_writes(self):
        self._write_registry(["/some/other/path"])
        event = {"cwd": "/path/not/registered", "tool_name": "Edit"}
        rc = self._run_with_stdin(json.dumps(event))
        self.assertEqual(rc, 0)
        self.assertFalse(self.activity_path.exists())

    def test_missing_registry_silent_exit_zero(self):
        # No projects file at all.
        self.assertFalse(self.projects_path.exists())
        rc = self._run_with_stdin(json.dumps({"cwd": "/x", "tool_name": "Edit"}))
        self.assertEqual(rc, 0)
        self.assertFalse(self.activity_path.exists())

    def test_activity_jsonl_created_on_first_hit(self):
        cwd = "/repo"
        self._write_registry([cwd])
        self.assertFalse(self.activity_path.exists())
        rc = self._run_with_stdin(json.dumps({"cwd": cwd, "tool_name": "Skill"}))
        self.assertEqual(rc, 0)
        self.assertTrue(self.activity_path.exists())
        self.assertEqual(len(self._read_activity_lines()), 1)

    def test_multiple_appends_accumulate(self):
        cwd = "/repo"
        self._write_registry([cwd])
        for i in range(3):
            rc = self._run_with_stdin(
                json.dumps({"cwd": cwd, "tool_name": f"T{i}"})
            )
            self.assertEqual(rc, 0)
        self.assertEqual(len(self._read_activity_lines()), 3)

    # -------------------------------------------------------------------
    # cwd extraction fallbacks
    # -------------------------------------------------------------------
    def test_cwd_from_working_directory_field(self):
        cwd = "/wd-style"
        self._write_registry([cwd])
        rc = self._run_with_stdin(
            json.dumps({"working_directory": cwd, "tool_name": "Edit"})
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._read_activity_lines()), 1)

    def test_cwd_falls_back_to_pwd_env(self):
        cwd = "/pwd-fallback"
        self._write_registry([cwd])
        with mock.patch.dict(os.environ, {"PWD": cwd}, clear=False):
            rc = self._run_with_stdin(
                json.dumps({"tool_name": "Edit"})  # no cwd field
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._read_activity_lines()), 1)

    # -------------------------------------------------------------------
    # IO failure modes
    # -------------------------------------------------------------------
    def test_disk_full_silently_exits_zero(self):
        cwd = "/repo"
        self._write_registry([cwd])
        with mock.patch(
            "hook_runner.os.write", side_effect=OSError("ENOSPC")
        ):
            rc = self._run_with_stdin(
                json.dumps({"cwd": cwd, "tool_name": "Edit"})
            )
        self.assertEqual(rc, 0)

    def test_open_failure_silently_exits_zero(self):
        cwd = "/repo"
        self._write_registry([cwd])
        with mock.patch(
            "hook_runner.os.open", side_effect=OSError("EACCES")
        ):
            rc = self._run_with_stdin(
                json.dumps({"cwd": cwd, "tool_name": "Edit"})
            )
        self.assertEqual(rc, 0)

    def test_malformed_stdin_silent_exit_zero(self):
        cwd = "/repo"
        self._write_registry([cwd])
        rc = self._run_with_stdin("not json {{{")
        self.assertEqual(rc, 0)
        # We have no cwd from a malformed event, but PWD fallback might match.
        # Either way: no crash.

    def test_empty_stdin_silent_exit_zero(self):
        rc = self._run_with_stdin("")
        self.assertEqual(rc, 0)

    def test_non_dict_stdin_silent_exit_zero(self):
        rc = self._run_with_stdin(json.dumps(["not", "a", "dict"]))
        self.assertEqual(rc, 0)

    def test_corrupt_registry_silent_exit_zero(self):
        with open(self.projects_path, "w") as fh:
            fh.write("{not json")
        rc = self._run_with_stdin(json.dumps({"cwd": "/x", "tool_name": "E"}))
        self.assertEqual(rc, 0)
        self.assertFalse(self.activity_path.exists())

    def test_registry_not_a_list_silent_exit_zero(self):
        with open(self.projects_path, "w") as fh:
            json.dump({"not": "a list"}, fh)
        rc = self._run_with_stdin(json.dumps({"cwd": "/x", "tool_name": "E"}))
        self.assertEqual(rc, 0)
        self.assertFalse(self.activity_path.exists())

    def test_stdin_read_failure_silent_exit_zero(self):
        cwd = "/repo"
        self._write_registry([cwd])
        broken_stdin = mock.Mock()
        broken_stdin.read.side_effect = OSError("read fail")
        with mock.patch.object(sys, "stdin", broken_stdin):
            rc = hook_runner.main()
        self.assertEqual(rc, 0)

    # -------------------------------------------------------------------
    # Line size cap
    # -------------------------------------------------------------------
    def test_small_line_within_cap(self):
        cwd = "/repo"
        self._write_registry([cwd])
        rc = self._run_with_stdin(
            json.dumps(
                {
                    "cwd": cwd,
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "/repo/x.py"},
                }
            )
        )
        self.assertEqual(rc, 0)
        with open(self.activity_path, "rb") as fh:
            line = fh.readline()
        self.assertLessEqual(len(line), hook_runner.MAX_LINE_BYTES)

    def test_large_file_path_truncated_to_cap(self):
        cwd = "/repo"
        self._write_registry([cwd])
        # 10k-byte file path forces truncation.
        huge = "/repo/" + ("a" * 10_000)
        rc = self._run_with_stdin(
            json.dumps(
                {
                    "cwd": cwd,
                    "tool_name": "Edit",
                    "session_id": "rid",
                    "tool_input": {"file_path": huge},
                }
            )
        )
        self.assertEqual(rc, 0)
        with open(self.activity_path, "rb") as fh:
            line = fh.readline()
        self.assertLessEqual(len(line), hook_runner.MAX_LINE_BYTES)
        # Line is still valid JSON.
        rec = json.loads(line)
        self.assertEqual(rec["cwd"], cwd)
        # File field truncated but present.
        self.assertIn("file", rec)
        self.assertTrue(rec["file"].startswith("/repo/"))
        self.assertLess(len(rec["file"]), len(huge))

    def test_serialize_capped_drops_file_when_required(self):
        # Build a record where even an empty file value won't fit; ensure
        # the field is dropped rather than crashing.
        record = {
            "ts": "x",
            "event": "tool_use",
            "tool": "T" * 5000,  # absurdly large tool name
            "cwd": "C" * 1000,
            "file": "F" * 5000,
        }
        encoded = hook_runner._serialize_capped(record)
        self.assertLessEqual(len(encoded), hook_runner.MAX_LINE_BYTES)
        # Ends with newline.
        self.assertTrue(encoded.endswith(b"\n"))

    # -------------------------------------------------------------------
    # extract helpers (unit)
    # -------------------------------------------------------------------
    def test_extract_tool_fallback_to_unknown(self):
        self.assertEqual(hook_runner._extract_tool({}), "unknown")
        self.assertEqual(hook_runner._extract_tool({"tool": "X"}), "X")
        self.assertEqual(hook_runner._extract_tool({"tool_name": "Y"}), "Y")
        self.assertEqual(hook_runner._extract_tool({"name": "Z"}), "Z")

    def test_extract_run_id_optional(self):
        self.assertIsNone(hook_runner._extract_run_id({}))
        self.assertEqual(hook_runner._extract_run_id({"run_id": "a"}), "a")
        self.assertEqual(hook_runner._extract_run_id({"session_id": "b"}), "b")

    def test_extract_file_from_various_shapes(self):
        self.assertIsNone(hook_runner._extract_file({}))
        self.assertEqual(
            hook_runner._extract_file({"file": "/a"}), "/a"
        )
        self.assertEqual(
            hook_runner._extract_file({"file_path": "/b"}), "/b"
        )
        self.assertEqual(
            hook_runner._extract_file({"tool_input": {"file_path": "/c"}}),
            "/c",
        )
        self.assertEqual(
            hook_runner._extract_file({"tool_input": {"path": "/d"}}), "/d"
        )

    def test_extract_cwd_falls_back_to_getcwd(self):
        # No cwd in event, no PWD env var.
        env = dict(os.environ)
        env.pop("PWD", None)
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["AI_QUICKSTART_HOME"] = str(self.aq_home)
            cwd = hook_runner._extract_cwd({})
            self.assertTrue(isinstance(cwd, str))


if __name__ == "__main__":
    unittest.main()
