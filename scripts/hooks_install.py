"""ai-quickstart hook installer.

Installs two PostToolUse hook entries into ``~/.claude/settings.json``:

1. One matching the ``Skill`` tool.
2. One matching the ``Edit`` and ``Write`` tools.

Each hook command is a small bash one-liner that does a fast stat-check on
``~/.ai-quickstart/managed-projects.json`` for the current cwd and only
``exec``s the python ``hook_runner.py`` if the cwd matches a managed project.
This keeps the hot path under 1ms when the user is working outside an
ai-quickstart project.

The exact entries written are recorded in
``~/.ai-quickstart/installed-hooks.json`` (the manifest). The manifest is the
source of truth for ``uninstall()`` so we can remove ONLY our entries and
leave the user's other hooks untouched.

Stdlib only. Python 3.9+ compatible.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Tuple

# ---------------------------------------------------------------------------
# Hook command (bash one-liner)
# ---------------------------------------------------------------------------
#
# This is the EXACT command string written into settings.json for both hook
# entries. It is intentionally tiny so the hot path stays under ~1ms when the
# user's cwd is not a managed ai-quickstart project.
#
# Behavior:
#   1. ``[ -f "$HOME/.ai-quickstart/managed-projects.json" ]``
#        Fast stat-check; if the registry doesn't exist we exit silently.
#   2. ``grep -q "\"$(pwd)\"" ...``
#        Cheap substring match for the current cwd inside the JSON registry.
#        We match the cwd wrapped in quotes so a substring like ``/foo`` does
#        not falsely match ``/foo/bar``.
#   3. ``exec python3 ~/.claude/skills/ai-quickstart/scripts/hook_runner.py``
#        Only spawned when cwd is a managed project. ``exec`` replaces the
#        shell so we don't leave an extra process around.
HOOK_COMMAND = (
    '[ -f "$HOME/.ai-quickstart/managed-projects.json" ] && '
    'grep -q "\\"$(pwd)\\"" "$HOME/.ai-quickstart/managed-projects.json" && '
    'exec python3 "$HOME/.claude/skills/ai-quickstart/scripts/hook_runner.py"'
)


# ---------------------------------------------------------------------------
# Path helpers (env-overridable for tests)
# ---------------------------------------------------------------------------
def _ai_quickstart_home() -> Path:
    override = os.environ.get("AI_QUICKSTART_HOME")
    if override:
        return Path(override)
    return Path.home() / ".ai-quickstart"


def _claude_home() -> Path:
    override = os.environ.get("CLAUDE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def _settings_path() -> Path:
    return _claude_home() / "settings.json"


def _manifest_path() -> Path:
    return _ai_quickstart_home() / "installed-hooks.json"


def _managed_projects_path() -> Path:
    return _ai_quickstart_home() / "managed-projects.json"


# ---------------------------------------------------------------------------
# Hook entry construction
# ---------------------------------------------------------------------------
def _build_hook_entries() -> list:
    """Return the list of PostToolUse hook entries we install.

    The structure follows Claude Code's settings.json hook format::

        {
          "matcher": "Skill",
          "hooks": [
            {"type": "command", "command": "<bash one-liner>"}
          ]
        }
    """
    return [
        {
            "matcher": "Skill",
            "hooks": [{"type": "command", "command": HOOK_COMMAND}],
        },
        {
            "matcher": "Edit|Write",
            "hooks": [{"type": "command", "command": HOOK_COMMAND}],
        },
    ]


# ---------------------------------------------------------------------------
# Atomic JSON write
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, data) -> None:
    """Write ``data`` as JSON to ``path`` atomically (tmp + rename).

    Validates the serialized JSON parses before the rename so we never leave a
    corrupt settings.json on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2)
    # Validate parse before we touch the destination.
    json.loads(serialized)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json_or(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def is_installed() -> bool:
    """Return True iff the manifest file exists."""
    return _manifest_path().exists()


def detect_claude_code_version() -> Tuple[str, bool]:
    """Return ``(version_string, is_compatible)``.

    Tries ``claude --version``. If that fails for any reason, returns
    ``("unknown", True)`` and prints a warning. Compatibility check is
    intentionally permissive for now (any version is accepted); we just want
    to record what we saw.
    """
    try:
        proc = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            version = (proc.stdout or proc.stderr or "").strip() or "unknown"
            return version, True
        print(
            "warning: `claude --version` exited non-zero; assuming compatible.",
            file=sys.stderr,
        )
        return "unknown", True
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"warning: could not detect Claude Code version ({exc}); "
            "assuming compatible.",
            file=sys.stderr,
        )
        return "unknown", True


def install(consent: bool = False) -> bool:
    """Install ai-quickstart's PostToolUse hooks into settings.json.

    If ``consent`` is False, prints what would be added and returns False
    without writing anything. If True, atomically appends the two hook
    entries and records the exact entries in the manifest.

    Idempotent: if the manifest already exists, returns True without writing.
    """
    if is_installed():
        # Idempotent: already installed.
        return True

    entries = _build_hook_entries()

    if not consent:
        print("ai-quickstart will add 2 PostToolUse hooks to settings.json:")
        for entry in entries:
            print(f"  - matcher: {entry['matcher']}")
            print(f"    command: {entry['hooks'][0]['command']}")
        print("Re-run with consent=True to install.")
        return False

    settings_path = _settings_path()
    settings = _read_json_or(settings_path, {})
    if not isinstance(settings, dict):
        raise RuntimeError(
            f"settings.json at {settings_path} is not a JSON object; refusing to install."
        )

    hooks_section = settings.setdefault("hooks", {})
    if not isinstance(hooks_section, dict):
        raise RuntimeError(
            "settings.json `hooks` is not an object; refusing to install."
        )

    post_tool_use = hooks_section.setdefault("PostToolUse", [])
    if not isinstance(post_tool_use, list):
        raise RuntimeError(
            "settings.json `hooks.PostToolUse` is not a list; refusing to install."
        )

    for entry in entries:
        post_tool_use.append(entry)

    _atomic_write_json(settings_path, settings)

    manifest = {
        "version": 1,
        "hook_command": HOOK_COMMAND,
        "entries": entries,
    }
    _atomic_write_json(_manifest_path(), manifest)
    return True


def uninstall() -> bool:
    """Remove the hook entries we installed (per manifest) from settings.json.

    Preserves any other hooks the user has. Removes the manifest file when
    done. Returns False if no manifest is present (nothing to do).
    """
    manifest_path = _manifest_path()
    if not manifest_path.exists():
        return False

    with open(manifest_path, "r") as fh:
        manifest = json.load(fh)

    entries_to_remove = manifest.get("entries", [])
    settings_path = _settings_path()
    settings = _read_json_or(settings_path, None)

    if isinstance(settings, dict):
        hooks_section = settings.get("hooks")
        if isinstance(hooks_section, dict):
            post_tool_use = hooks_section.get("PostToolUse")
            if isinstance(post_tool_use, list):
                remaining = [e for e in post_tool_use if e not in entries_to_remove]
                if remaining:
                    hooks_section["PostToolUse"] = remaining
                else:
                    # Clean up empty list entirely so we don't leave clutter.
                    hooks_section.pop("PostToolUse", None)
                if not hooks_section:
                    settings.pop("hooks", None)
                _atomic_write_json(settings_path, settings)

    try:
        manifest_path.unlink()
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# Managed-projects registry
# ---------------------------------------------------------------------------
def _read_managed_projects() -> list:
    data = _read_json_or(_managed_projects_path(), [])
    if not isinstance(data, list):
        return []
    return data


def add_managed_project(project_path: Path) -> None:
    """Append ``project_path`` (resolved absolute) to the managed-projects registry.

    Idempotent: a path already present is not duplicated.
    """
    abs_path = str(Path(project_path).expanduser().resolve())
    projects = _read_managed_projects()
    if abs_path in projects:
        return
    projects.append(abs_path)
    _atomic_write_json(_managed_projects_path(), projects)


def remove_managed_project(project_path: Path) -> None:
    """Remove ``project_path`` from the managed-projects registry, if present."""
    abs_path = str(Path(project_path).expanduser().resolve())
    projects = _read_managed_projects()
    if abs_path not in projects:
        return
    projects = [p for p in projects if p != abs_path]
    _atomic_write_json(_managed_projects_path(), projects)


# ---------------------------------------------------------------------------
# CLI entry point (optional convenience)
# ---------------------------------------------------------------------------
def _main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: hooks_install.py [install [--yes] | uninstall | status]")
        return 0
    cmd = argv[0]
    if cmd == "install":
        consent = "--yes" in argv[1:]
        ok = install(consent=consent)
        return 0 if ok else 1
    if cmd == "uninstall":
        uninstall()
        return 0
    if cmd == "status":
        print("installed" if is_installed() else "not installed")
        version, _ = detect_claude_code_version()
        print(f"claude version: {version}")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
