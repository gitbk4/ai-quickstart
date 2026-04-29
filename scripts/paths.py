"""Path helpers for ai-quickstart.

All scripts import from here for the ~/.ai-quickstart/ layout, run-id
generation, and macOS sync'd-filesystem detection (iCloud/Dropbox/OneDrive/NFS).

Stdlib only. Soft-fail on filesystem detection — warnings to stderr,
never raises.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Layout constants — see PLAN.md "Persona system" section.
# ---------------------------------------------------------------------------

ROOT_DIR_NAME = ".ai-quickstart"

PERSONA_SUBDIR = "persona"
RUNS_SUBDIR = "runs"
CACHE_SUBDIR = "cache"
ANECDOTES_SUBDIR = "anecdotes"

PERSONA_FILE = "persona.md"
PERSONA_BAK = "persona.md.bak"
ACTIVITY_FILE = "activity.jsonl"
ACTIVITY_SUMMARY = "activity-summary.json"
HEAL_LOCK = ".heal.lock"

MANAGED_PROJECTS_FILE = "managed-projects.json"
INSTALLED_HOOKS_FILE = "installed-hooks.json"
HEAL_ERRORS_FILE = "heal-errors.jsonl"
CONFIG_FILE = "config.json"

GITHUB_CACHE_SUBDIR = "github"
MCPMARKET_CACHE_SUBDIR = "mcpmarket"


# ---------------------------------------------------------------------------
# Root + subpath helpers.
# ---------------------------------------------------------------------------


def home_root(home: Path | None = None) -> Path:
    """Return ``~/.ai-quickstart``. ``home`` overrides for tests."""
    base = Path(home) if home is not None else Path.home()
    return base / ROOT_DIR_NAME


def persona_dir(home: Path | None = None) -> Path:
    return home_root(home) / PERSONA_SUBDIR


def persona_path(home: Path | None = None) -> Path:
    return persona_dir(home) / PERSONA_FILE


def persona_backup_path(home: Path | None = None) -> Path:
    return persona_dir(home) / PERSONA_BAK


def activity_path(home: Path | None = None) -> Path:
    return persona_dir(home) / ACTIVITY_FILE


def activity_summary_path(home: Path | None = None) -> Path:
    return persona_dir(home) / ACTIVITY_SUMMARY


def anecdotes_dir(home: Path | None = None) -> Path:
    return persona_dir(home) / ANECDOTES_SUBDIR


def heal_lock_path(home: Path | None = None) -> Path:
    return persona_dir(home) / HEAL_LOCK


def runs_dir(home: Path | None = None) -> Path:
    return home_root(home) / RUNS_SUBDIR


def run_dir(run_id: str, home: Path | None = None) -> Path:
    return runs_dir(home) / run_id


def cache_dir(home: Path | None = None) -> Path:
    return home_root(home) / CACHE_SUBDIR


def github_cache_dir(home: Path | None = None) -> Path:
    return cache_dir(home) / GITHUB_CACHE_SUBDIR


def mcpmarket_cache_dir(home: Path | None = None) -> Path:
    return cache_dir(home) / MCPMARKET_CACHE_SUBDIR


def managed_projects_path(home: Path | None = None) -> Path:
    return home_root(home) / MANAGED_PROJECTS_FILE


def installed_hooks_path(home: Path | None = None) -> Path:
    return home_root(home) / INSTALLED_HOOKS_FILE


def heal_errors_path(home: Path | None = None) -> Path:
    return home_root(home) / HEAL_ERRORS_FILE


def config_path(home: Path | None = None) -> Path:
    return home_root(home) / CONFIG_FILE


# ---------------------------------------------------------------------------
# Directory creation.
# ---------------------------------------------------------------------------


def ensure_dirs(home: Path | None = None) -> dict:
    """Create the full ~/.ai-quickstart/ tree if missing.

    Returns a dict listing created/existing directories. Idempotent.
    """
    targets = [
        home_root(home),
        persona_dir(home),
        anecdotes_dir(home),
        runs_dir(home),
        cache_dir(home),
        github_cache_dir(home),
        mcpmarket_cache_dir(home),
    ]
    created = []
    existing = []
    for d in targets:
        if d.exists():
            existing.append(str(d))
        else:
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))
    return {"created": created, "existing": existing}


def ensure_run_dir(run_id: str, home: Path | None = None) -> Path:
    """Create ~/.ai-quickstart/runs/<run-id>/ and return its path."""
    rd = run_dir(run_id, home)
    rd.mkdir(parents=True, exist_ok=True)
    return rd


# ---------------------------------------------------------------------------
# Run-id generation: ISO timestamp (compact) + short uuid suffix.
# ---------------------------------------------------------------------------


def generate_run_id(now: _dt.datetime | None = None) -> str:
    """Return a sortable run id like ``20260429T133500Z-ab12cd34``.

    The timestamp is UTC, second-precision, basic ISO 8601 (no separators).
    The suffix is the first 8 hex chars of a uuid4 — collision-resistant
    enough for run-level uniqueness without being unwieldy.
    """
    ts = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    return f"{stamp}-{suffix}"


# ---------------------------------------------------------------------------
# Filesystem-sync detection (macOS-focused, warn-only).
# ---------------------------------------------------------------------------

# Folder fragments (relative to $HOME) that indicate a third-party sync client.
# Match is path-prefix-based, case-insensitive on macOS HFS+/APFS conventions.
_SYNC_HINTS = (
    # iCloud Drive — canonical container under ~/Library/Mobile Documents.
    ("Library/Mobile Documents", "icloud"),
    ("Library/CloudStorage/iCloud", "icloud"),
    # Modern macOS surfaces external providers under CloudStorage/<Provider>.
    ("Library/CloudStorage/Dropbox", "dropbox"),
    ("Library/CloudStorage/OneDrive", "onedrive"),
    ("Library/CloudStorage/GoogleDrive", "googledrive"),
    # Legacy ~/Dropbox and ~/OneDrive home-level folders.
    ("Dropbox", "dropbox"),
    ("OneDrive", "onedrive"),
    ("Google Drive", "googledrive"),
)


def _normalize(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path.expanduser())


def detect_sync_kind(path: Path, home: Path | None = None) -> str | None:
    """Return a sync-kind label (icloud|dropbox|onedrive|googledrive|nfs)
    if ``path`` looks like it's on a sync'd or network filesystem, else None.

    Heuristic, macOS-focused:
      * Path prefix match against known sync providers under ``$HOME``.
      * ``os.statvfs`` sniff: NFS mounts surface ``f_fsid`` patterns and the
        device is typically not on a local APFS volume — we can't reliably
        identify NFS without ``mount`` parsing, so we use a coarse hint:
        if statvfs raises or the basename of the device-mount looks remote.

    Soft-fail: if any check raises, returns None and prints a stderr warning.
    """
    try:
        norm = _normalize(path)
        home_str = _normalize(Path(home) if home is not None else Path.home())
    except Exception as e:  # pylint: disable=broad-except
        print(f"ai-quickstart: filesystem detect skipped: {e}", file=sys.stderr)
        return None

    norm_lower = norm.lower()
    home_lower = home_str.lower()

    # Path-prefix sync-provider checks.
    for fragment, label in _SYNC_HINTS:
        candidate = os.path.join(home_lower, fragment.lower())
        if norm_lower.startswith(candidate):
            return label

    # NFS / network-fs sniff via os.statvfs. We can't classify NFS portably
    # from Python stdlib, so we treat any non-zero ``f_flag & ST_NOSUID``
    # combined with an unusual device id as a soft hint. Most macOS local
    # volumes are APFS and statvfs works fine; if statvfs itself raises
    # ENOTSUP, that's a strong signal of a remote/odd FS.
    statvfs = getattr(os, "statvfs", None)
    if statvfs is not None:
        try:
            statvfs(str(path) if path.exists() else str(path.parent))
        except OSError as e:
            # ENOTSUP / ENODEV / EIO from statvfs is suspicious.
            print(
                f"ai-quickstart: statvfs failed on {path} ({e}); "
                "filesystem may be remote (NFS) — flock semantics unreliable",
                file=sys.stderr,
            )
            return "nfs"

    return None


def warn_if_synced(path: Path, home: Path | None = None) -> str | None:
    """Print a stderr warning if ``path`` is on a sync'd filesystem.

    Returns the detected kind (or None). Always non-blocking.
    """
    kind = detect_sync_kind(path, home=home)
    if kind:
        print(
            f"ai-quickstart: WARNING: {path} appears to be on {kind}. "
            "flock semantics are unreliable on sync'd filesystems — "
            "run /ai-quickstart heal from one Claude Code session at a time, "
            "or move ~/.ai-quickstart to a local-only volume.",
            file=sys.stderr,
        )
    return kind


# ---------------------------------------------------------------------------
# CLI entry point — handy for ad-hoc inspection.
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="ai-quickstart paths inspector")
    parser.add_argument("--ensure-dirs", action="store_true",
                        help="Create the ~/.ai-quickstart tree")
    parser.add_argument("--detect", type=str, default=None,
                        help="Check if a path is on a sync'd filesystem")
    parser.add_argument("--run-id", action="store_true",
                        help="Generate a fresh run id and print it")
    args = parser.parse_args(argv)

    out: dict = {"home_root": str(home_root())}
    if args.ensure_dirs:
        out["ensure"] = ensure_dirs()
    if args.detect:
        out["detect"] = detect_sync_kind(Path(args.detect))
    if args.run_id:
        out["run_id"] = generate_run_id()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
