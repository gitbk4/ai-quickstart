"""Tests for scripts/hooks_install.py."""

from __future__ import annotations

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

import hooks_install  # noqa: E402


class HooksInstallTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.aq_home = root / "ai-quickstart"
        self.claude_home = root / "claude"
        self.aq_home.mkdir()
        self.claude_home.mkdir()

        env_patch = mock.patch.dict(
            os.environ,
            {
                "AI_QUICKSTART_HOME": str(self.aq_home),
                "CLAUDE_HOME": str(self.claude_home),
            },
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    @property
    def settings_path(self) -> Path:
        return self.claude_home / "settings.json"

    @property
    def manifest_path(self) -> Path:
        return self.aq_home / "installed-hooks.json"

    @property
    def projects_path(self) -> Path:
        return self.aq_home / "managed-projects.json"

    # -------------------------------------------------------------------
    # install / uninstall
    # -------------------------------------------------------------------
    def test_install_creates_settings_when_missing(self):
        self.assertFalse(self.settings_path.exists())
        ok = hooks_install.install(consent=True)
        self.assertTrue(ok)
        self.assertTrue(self.settings_path.exists())

        with open(self.settings_path) as fh:
            settings = json.load(fh)
        post = settings["hooks"]["PostToolUse"]
        self.assertEqual(len(post), 2)
        matchers = {entry["matcher"] for entry in post}
        self.assertEqual(matchers, {"Skill", "Edit|Write"})
        for entry in post:
            self.assertEqual(entry["hooks"][0]["type"], "command")
            self.assertEqual(
                entry["hooks"][0]["command"], hooks_install.HOOK_COMMAND
            )

        # Manifest written.
        self.assertTrue(self.manifest_path.exists())
        with open(self.manifest_path) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["hook_command"], hooks_install.HOOK_COMMAND)
        self.assertEqual(len(manifest["entries"]), 2)

    def test_install_preserves_existing_hooks(self):
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo unrelated"}
                        ],
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo pre"}],
                    }
                ],
            },
            "permissions": {"allow": ["Bash(ls:*)"]},
        }
        with open(self.settings_path, "w") as fh:
            json.dump(existing, fh)

        hooks_install.install(consent=True)

        with open(self.settings_path) as fh:
            settings = json.load(fh)
        post = settings["hooks"]["PostToolUse"]
        # Existing + 2 new.
        self.assertEqual(len(post), 3)
        commands = [e["hooks"][0]["command"] for e in post]
        self.assertIn("echo unrelated", commands)
        self.assertEqual(commands.count(hooks_install.HOOK_COMMAND), 2)

        # PreToolUse and permissions untouched.
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls:*)"]})
        self.assertEqual(
            settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "echo pre"
        )

    def test_install_idempotent_second_call_no_change(self):
        hooks_install.install(consent=True)
        with open(self.settings_path) as fh:
            first = fh.read()
        # Second install should be a no-op (manifest exists).
        result = hooks_install.install(consent=True)
        self.assertTrue(result)
        with open(self.settings_path) as fh:
            second = fh.read()
        self.assertEqual(first, second)

    def test_install_without_consent_writes_nothing(self):
        result = hooks_install.install(consent=False)
        self.assertFalse(result)
        self.assertFalse(self.settings_path.exists())
        self.assertFalse(self.manifest_path.exists())

    def test_uninstall_removes_only_our_entries(self):
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo unrelated"}
                        ],
                    }
                ]
            }
        }
        with open(self.settings_path, "w") as fh:
            json.dump(existing, fh)
        hooks_install.install(consent=True)

        result = hooks_install.uninstall()
        self.assertTrue(result)

        with open(self.settings_path) as fh:
            settings = json.load(fh)
        post = settings["hooks"]["PostToolUse"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["hooks"][0]["command"], "echo unrelated")
        self.assertFalse(self.manifest_path.exists())

    def test_uninstall_when_no_manifest_returns_false(self):
        self.assertFalse(hooks_install.uninstall())

    def test_uninstall_cleans_empty_hooks_section(self):
        # No pre-existing hooks; only ours.
        hooks_install.install(consent=True)
        hooks_install.uninstall()
        with open(self.settings_path) as fh:
            settings = json.load(fh)
        # After removing our two entries with no others, the hooks section
        # should be cleaned up.
        self.assertNotIn("hooks", settings)

    def test_is_installed_reflects_manifest(self):
        self.assertFalse(hooks_install.is_installed())
        hooks_install.install(consent=True)
        self.assertTrue(hooks_install.is_installed())
        hooks_install.uninstall()
        self.assertFalse(hooks_install.is_installed())

    # -------------------------------------------------------------------
    # managed-projects registry
    # -------------------------------------------------------------------
    def test_add_managed_project_creates_and_appends(self):
        proj = Path(self._tmp.name) / "proj-a"
        proj.mkdir()
        hooks_install.add_managed_project(proj)
        with open(self.projects_path) as fh:
            data = json.load(fh)
        self.assertEqual(data, [str(proj.resolve())])

        proj_b = Path(self._tmp.name) / "proj-b"
        proj_b.mkdir()
        hooks_install.add_managed_project(proj_b)
        with open(self.projects_path) as fh:
            data = json.load(fh)
        self.assertEqual(
            sorted(data), sorted([str(proj.resolve()), str(proj_b.resolve())])
        )

    def test_add_managed_project_idempotent(self):
        proj = Path(self._tmp.name) / "proj"
        proj.mkdir()
        hooks_install.add_managed_project(proj)
        hooks_install.add_managed_project(proj)
        with open(self.projects_path) as fh:
            data = json.load(fh)
        self.assertEqual(data, [str(proj.resolve())])

    def test_remove_managed_project(self):
        proj_a = Path(self._tmp.name) / "a"
        proj_b = Path(self._tmp.name) / "b"
        proj_a.mkdir()
        proj_b.mkdir()
        hooks_install.add_managed_project(proj_a)
        hooks_install.add_managed_project(proj_b)

        hooks_install.remove_managed_project(proj_a)
        with open(self.projects_path) as fh:
            data = json.load(fh)
        self.assertEqual(data, [str(proj_b.resolve())])

        # Removing a non-present project is a no-op.
        hooks_install.remove_managed_project(proj_a)
        with open(self.projects_path) as fh:
            data = json.load(fh)
        self.assertEqual(data, [str(proj_b.resolve())])

    # -------------------------------------------------------------------
    # version detection
    # -------------------------------------------------------------------
    def test_detect_claude_code_version_success(self):
        fake = mock.Mock()
        fake.returncode = 0
        fake.stdout = "1.2.3\n"
        fake.stderr = ""
        with mock.patch("hooks_install.subprocess.run", return_value=fake):
            version, compatible = hooks_install.detect_claude_code_version()
        self.assertEqual(version, "1.2.3")
        self.assertTrue(compatible)

    def test_detect_claude_code_version_failure_assumes_compatible(self):
        with mock.patch(
            "hooks_install.subprocess.run", side_effect=FileNotFoundError("no claude")
        ):
            version, compatible = hooks_install.detect_claude_code_version()
        self.assertEqual(version, "unknown")
        self.assertTrue(compatible)

    def test_detect_claude_code_version_nonzero_returns_unknown(self):
        fake = mock.Mock()
        fake.returncode = 1
        fake.stdout = ""
        fake.stderr = "boom"
        with mock.patch("hooks_install.subprocess.run", return_value=fake):
            version, compatible = hooks_install.detect_claude_code_version()
        self.assertEqual(version, "unknown")
        self.assertTrue(compatible)

    # -------------------------------------------------------------------
    # CLI smoke test
    # -------------------------------------------------------------------
    def test_cli_status(self):
        with mock.patch("hooks_install.subprocess.run") as run_mock:
            fake = mock.Mock()
            fake.returncode = 0
            fake.stdout = "1.0.0"
            fake.stderr = ""
            run_mock.return_value = fake
            self.assertEqual(hooks_install._main(["status"]), 0)

    def test_cli_install_without_yes_prints_only(self):
        rc = hooks_install._main(["install"])
        self.assertEqual(rc, 1)  # consent=False -> install returns False -> rc 1
        self.assertFalse(self.settings_path.exists())

    def test_cli_install_with_yes_writes(self):
        rc = hooks_install._main(["install", "--yes"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.settings_path.exists())

    def test_cli_uninstall(self):
        hooks_install.install(consent=True)
        rc = hooks_install._main(["uninstall"])
        self.assertEqual(rc, 0)

    def test_cli_unknown_command(self):
        rc = hooks_install._main(["nope"])
        self.assertEqual(rc, 2)

    def test_cli_help(self):
        self.assertEqual(hooks_install._main([]), 0)
        self.assertEqual(hooks_install._main(["--help"]), 0)

    # -------------------------------------------------------------------
    # error paths
    # -------------------------------------------------------------------
    def test_install_refuses_non_object_settings(self):
        with open(self.settings_path, "w") as fh:
            json.dump(["not", "an", "object"], fh)
        with self.assertRaises(RuntimeError):
            hooks_install.install(consent=True)

    def test_install_refuses_non_object_hooks(self):
        with open(self.settings_path, "w") as fh:
            json.dump({"hooks": "broken"}, fh)
        with self.assertRaises(RuntimeError):
            hooks_install.install(consent=True)

    def test_install_refuses_non_list_post_tool_use(self):
        with open(self.settings_path, "w") as fh:
            json.dump({"hooks": {"PostToolUse": "nope"}}, fh)
        with self.assertRaises(RuntimeError):
            hooks_install.install(consent=True)


if __name__ == "__main__":
    unittest.main()
