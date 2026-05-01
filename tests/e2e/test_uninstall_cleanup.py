"""E2E: hooks_install.uninstall() removes our hooks but preserves user data.

Scenario:
  * Install hooks via ``hooks_install.install(consent=True)``.
  * Add 2 managed projects.
  * Run ``hooks_install.uninstall()``.
  * Verify settings.json no longer contains our hook entries; manifest deleted.
  * Per PLAN.md "leaves data" decision: managed-projects.json is preserved.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_uninstall_removes_hooks_preserves_data(
    e2e_home: Path, tmp_path: Path
):
    import hooks_install  # type: ignore

    # Pre-seed settings.json with one user-defined hook so we can verify
    # uninstall preserves it.
    claude_home = Path(__import__("os").environ["CLAUDE_HOME"])
    settings_path = claude_home / "settings.json"
    user_hook = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "echo user-hook-runs"}],
    }
    settings_path.write_text(
        json.dumps({"hooks": {"PostToolUse": [user_hook]}}, indent=2),
        encoding="utf-8",
    )

    # ---- install ----
    ok = hooks_install.install(consent=True)
    assert ok is True
    assert hooks_install.is_installed() is True

    # Manifest written, settings.json now contains 1 user + 2 ai-quickstart entries.
    manifest_path = e2e_home / "installed-hooks.json"
    assert manifest_path.is_file()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    post_tool_use = settings["hooks"]["PostToolUse"]
    assert len(post_tool_use) == 3
    # The user hook is preserved alongside ours.
    assert user_hook in post_tool_use

    # ---- add 2 managed projects ----
    proj_a = tmp_path / "p" / "a"
    proj_b = tmp_path / "p" / "b"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    hooks_install.add_managed_project(proj_a)
    hooks_install.add_managed_project(proj_b)
    registry_path = e2e_home / "managed-projects.json"
    assert registry_path.is_file()
    registry_before = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(registry_before) == 2

    # ---- uninstall ----
    ok = hooks_install.uninstall()
    assert ok is True

    # Manifest gone.
    assert not manifest_path.exists()
    assert hooks_install.is_installed() is False

    # settings.json has the user hook only — neither of our two entries remain.
    settings_after = json.loads(settings_path.read_text(encoding="utf-8"))
    post_tool_use_after = settings_after.get("hooks", {}).get("PostToolUse", [])
    assert post_tool_use_after == [user_hook], (
        "uninstall should leave only the user's pre-existing hook"
    )

    # managed-projects.json is preserved (PLAN.md "leaves data").
    assert registry_path.is_file()
    registry_after = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry_after == registry_before, (
        "uninstall must NOT touch managed-projects.json — that's user data "
        "per the PLAN.md 'leaves data' decision"
    )
