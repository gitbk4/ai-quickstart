"""Tests for host-runtime detection in scripts/paths.py.

Covers:
  * detect_host_runtime() under each env var
  * detect_host_runtime() under each home-dir signal
  * detect_host_runtime() unknown case
  * host_settings_path() / host_skills_dir() for each runtime
  * env-var override precedence over directory presence
  * priority order when multiple signals are present

Stdlib only. Each test uses a tmp HOME and a clean environment so the
host's real ~/.claude / ~/.codex / ~/.antigravity dirs do not leak in.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Make the scripts/ directory importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import paths  # noqa: E402


# Names of the three env vars paths.detect_host_runtime() consults. We always
# clear all three at the start of each test so the host environment cannot
# leak in.
_RUNTIME_ENV_VARS = ("CLAUDE_HOME", "CODEX_HOME", "ANTIGRAVITY_HOME")


class _RuntimeTestBase(unittest.TestCase):
    """Provides a clean environment + a tmp home for runtime tests."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

        # Strip any of the three runtime env vars so host state doesn't bleed
        # into tests. Tests opt in by setting the var explicitly via mock.
        cleared = {var: "" for var in _RUNTIME_ENV_VARS}
        # Use a context that DELETES, not blanks, so .get() returns None.
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        for var in _RUNTIME_ENV_VARS:
            os.environ.pop(var, None)
        self.addCleanup(env_patch.stop)


# ---------------------------------------------------------------------------
# detect_host_runtime() — env var signals
# ---------------------------------------------------------------------------


class TestDetectHostRuntimeEnvVars(_RuntimeTestBase):
    def test_claude_home_env_returns_claude_code(self):
        os.environ["CLAUDE_HOME"] = str(self.home / "claude")
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CLAUDE_CODE)

    def test_codex_home_env_returns_codex(self):
        os.environ["CODEX_HOME"] = str(self.home / "codex")
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CODEX)

    def test_antigravity_home_env_returns_antigravity(self):
        os.environ["ANTIGRAVITY_HOME"] = str(self.home / "ag")
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_ANTIGRAVITY)


# ---------------------------------------------------------------------------
# detect_host_runtime() — directory signals (no env vars)
# ---------------------------------------------------------------------------


class TestDetectHostRuntimeDirSignals(_RuntimeTestBase):
    def test_dot_claude_dir_returns_claude_code(self):
        (self.home / ".claude").mkdir()
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CLAUDE_CODE)

    def test_dot_codex_dir_returns_codex(self):
        (self.home / ".codex").mkdir()
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CODEX)

    def test_dot_antigravity_dir_returns_antigravity(self):
        (self.home / ".antigravity").mkdir()
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_ANTIGRAVITY)


# ---------------------------------------------------------------------------
# detect_host_runtime() — unknown
# ---------------------------------------------------------------------------


class TestDetectHostRuntimeUnknown(_RuntimeTestBase):
    def test_no_signals_returns_unknown(self):
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_UNKNOWN)

    def test_unrelated_dir_does_not_match(self):
        (self.home / ".some-other-tool").mkdir()
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_UNKNOWN)


# ---------------------------------------------------------------------------
# detect_host_runtime() — priority order
# ---------------------------------------------------------------------------


class TestDetectHostRuntimePriority(_RuntimeTestBase):
    def test_claude_wins_over_codex_when_both_dirs_present(self):
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CLAUDE_CODE)

    def test_claude_wins_over_antigravity_when_both_dirs_present(self):
        (self.home / ".claude").mkdir()
        (self.home / ".antigravity").mkdir()
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CLAUDE_CODE)

    def test_codex_wins_over_antigravity_when_both_dirs_present(self):
        (self.home / ".codex").mkdir()
        (self.home / ".antigravity").mkdir()
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CODEX)

    def test_env_var_for_higher_priority_runtime_wins(self):
        # ~/.codex exists but CLAUDE_HOME env var is set: claude-code wins
        # because it is the highest-priority signal that fires.
        (self.home / ".codex").mkdir()
        os.environ["CLAUDE_HOME"] = str(self.home / "claude")
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CLAUDE_CODE)

    def test_env_var_can_override_dir_only_within_priority_order(self):
        # Only ~/.claude exists, plus CODEX_HOME env. claude-code is checked
        # first, finds no env, falls to dir which exists -> claude-code.
        # CODEX_HOME never gets a turn.
        (self.home / ".claude").mkdir()
        os.environ["CODEX_HOME"] = str(self.home / "codex")
        self.assertEqual(paths.detect_host_runtime(home=self.home),
                         paths.RUNTIME_CLAUDE_CODE)


# ---------------------------------------------------------------------------
# host_settings_path()
# ---------------------------------------------------------------------------


class TestHostSettingsPath(_RuntimeTestBase):
    def test_claude_code_returns_settings_json(self):
        result = paths.host_settings_path(paths.RUNTIME_CLAUDE_CODE,
                                          home=self.home)
        self.assertEqual(result, self.home / ".claude" / "settings.json")

    def test_codex_returns_config_toml(self):
        result = paths.host_settings_path(paths.RUNTIME_CODEX, home=self.home)
        self.assertEqual(result, self.home / ".codex" / "config.toml")

    def test_antigravity_returns_settings_json(self):
        result = paths.host_settings_path(paths.RUNTIME_ANTIGRAVITY,
                                          home=self.home)
        self.assertEqual(result, self.home / ".antigravity" / "settings.json")

    def test_unknown_returns_none(self):
        self.assertIsNone(paths.host_settings_path(paths.RUNTIME_UNKNOWN,
                                                   home=self.home))

    def test_garbage_runtime_returns_none(self):
        self.assertIsNone(paths.host_settings_path("not-a-runtime",
                                                   home=self.home))

    def test_env_var_overrides_default_root(self):
        os.environ["CLAUDE_HOME"] = str(self.home / "custom-claude")
        result = paths.host_settings_path(paths.RUNTIME_CLAUDE_CODE,
                                          home=self.home)
        self.assertEqual(result, self.home / "custom-claude" / "settings.json")


# ---------------------------------------------------------------------------
# host_skills_dir()
# ---------------------------------------------------------------------------


class TestHostSkillsDir(_RuntimeTestBase):
    def test_claude_code_returns_skills_dir(self):
        result = paths.host_skills_dir(paths.RUNTIME_CLAUDE_CODE,
                                       home=self.home)
        self.assertEqual(result, self.home / ".claude" / "skills")

    def test_codex_returns_skills_dir(self):
        result = paths.host_skills_dir(paths.RUNTIME_CODEX, home=self.home)
        self.assertEqual(result, self.home / ".codex" / "skills")

    def test_antigravity_returns_skills_dir(self):
        result = paths.host_skills_dir(paths.RUNTIME_ANTIGRAVITY,
                                       home=self.home)
        self.assertEqual(result, self.home / ".antigravity" / "skills")

    def test_unknown_returns_none(self):
        self.assertIsNone(paths.host_skills_dir(paths.RUNTIME_UNKNOWN,
                                                home=self.home))

    def test_env_var_overrides_default_root(self):
        os.environ["CODEX_HOME"] = str(self.home / "custom-codex")
        result = paths.host_skills_dir(paths.RUNTIME_CODEX, home=self.home)
        self.assertEqual(result, self.home / "custom-codex" / "skills")


# ---------------------------------------------------------------------------
# CLI: --detect-runtime
# ---------------------------------------------------------------------------


class TestCliDetectRuntime(_RuntimeTestBase):
    def test_detect_runtime_emits_json_with_runtime_field(self):
        import io
        import json
        # Force CODEX_HOME and override Path.home() so the host's real
        # ~/.claude dir doesn't shadow the env signal.
        os.environ["CODEX_HOME"] = str(self.home / "codex")
        captured = io.StringIO()
        with mock.patch.object(Path, "home",
                               classmethod(lambda cls: self.home)), \
             mock.patch.object(sys, "stdout", captured):
            rc = paths._main(["--detect-runtime"])
        self.assertEqual(rc, 0)
        out = json.loads(captured.getvalue())
        self.assertEqual(out["runtime"], paths.RUNTIME_CODEX)
        self.assertIn("config.toml", out["host_settings_path"])
        self.assertTrue(out["host_skills_dir"].endswith("skills"))


if __name__ == "__main__":
    unittest.main()
