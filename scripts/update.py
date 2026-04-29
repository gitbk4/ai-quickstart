#!/usr/bin/env python3
"""Auto-update ai-quickstart from GitHub before a skill run.

Mirrors compathy/scripts/update.py contract: ``git pull --ff-only`` in the
ai-quickstart skill repo. Soft-fails on dirty tree, no remote, no network,
or diverged history — never blocks the skill (always exits 0).

Exit codes:
  0 — updated successfully, already up-to-date, or update failed (warned)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _read_version() -> str:
    try:
        return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _is_git_repo() -> bool:
    try:
        r = _git(["rev-parse", "--is-inside-work-tree"], REPO_ROOT)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _has_remote() -> bool:
    try:
        r = _git(["remote"], REPO_ROOT)
        return r.returncode == 0 and r.stdout.strip() != ""
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _is_dirty() -> bool:
    try:
        r = _git(["status", "--porcelain"], REPO_ROOT)
        return r.returncode == 0 and r.stdout.strip() != ""
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


# pylint: disable=too-many-return-statements
def update() -> dict:
    """Attempt to auto-update. Returns a status dict.

    Keys: action (updated|already-current|skipped|failed),
          old_version, new_version, message
    """
    old_version = _read_version()

    if not _is_git_repo():
        return {
            "action": "skipped",
            "old_version": old_version,
            "new_version": old_version,
            "message": "ai-quickstart installed via copy (not a git repo); update manually",
        }

    if not _has_remote():
        return {
            "action": "skipped",
            "old_version": old_version,
            "new_version": old_version,
            "message": "no git remote configured; update manually",
        }

    if _is_dirty():
        return {
            "action": "skipped",
            "old_version": old_version,
            "new_version": old_version,
            "message": "working tree has local changes; skipping auto-update",
        }

    # Fetch first to see if we're behind.
    try:
        fetch = _git(["fetch", "--quiet"], REPO_ROOT)
        if fetch.returncode != 0:
            return {
                "action": "failed",
                "old_version": old_version,
                "new_version": old_version,
                "message": f"git fetch failed: {fetch.stderr.strip()}",
            }
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        return {
            "action": "failed",
            "old_version": old_version,
            "new_version": old_version,
            "message": f"git fetch failed: {e}",
        }

    # Check if behind upstream.
    try:
        behind = _git(["rev-list", "--count", "HEAD..@{upstream}"], REPO_ROOT)
        if behind.returncode != 0 or behind.stdout.strip() == "0":
            return {
                "action": "already-current",
                "old_version": old_version,
                "new_version": old_version,
                "message": f"ai-quickstart v{old_version} (up to date)",
            }
    except (FileNotFoundError, subprocess.SubprocessError):
        pass  # proceed with pull anyway

    # Pull.
    try:
        pull = _git(["pull", "--ff-only"], REPO_ROOT)
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        return {
            "action": "failed",
            "old_version": old_version,
            "new_version": old_version,
            "message": f"git pull failed: {e}",
        }

    if pull.returncode != 0:
        stderr = pull.stderr.strip()
        if "not possible to fast-forward" in stderr or "diverge" in stderr.lower():
            msg = ("local changes diverge from remote; "
                   "run `cd ~/Code/ai-quickstart && git pull` manually")
        else:
            msg = f"git pull --ff-only failed: {stderr}"
        return {
            "action": "failed",
            "old_version": old_version,
            "new_version": old_version,
            "message": msg,
        }

    new_version = _read_version()
    if new_version == old_version:
        message = f"ai-quickstart updated (v{old_version}, no version bump)"
    else:
        message = f"ai-quickstart updated: v{old_version} -> v{new_version}"
    return {
        "action": "updated",
        "old_version": old_version,
        "new_version": new_version,
        "message": message,
    }


def main() -> int:
    """Main entry point. Always returns 0 — never blocks the skill."""
    result = update()
    action = result["action"]

    if action == "updated":
        print(f"ai-quickstart: {result['message']}")
    elif action == "already-current":
        print(f"ai-quickstart: {result['message']}")
    elif action == "skipped":
        print(f"ai-quickstart: {result['message']}", file=sys.stderr)
    elif action == "failed":
        print(f"ai-quickstart: WARNING: {result['message']}", file=sys.stderr)
        print("ai-quickstart: continuing with current version", file=sys.stderr)

    return 0  # always 0 — never block the skill


if __name__ == "__main__":
    sys.exit(main())
