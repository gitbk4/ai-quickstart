#!/usr/bin/env python3
"""Heal: deterministic side of the persona read->synthesize->write loop.

Pattern (compathy split):
  * Python (this module) is the deterministic half: lock, read, prepare context,
    accept rewritten prose, atomic write with backup + diff, release lock.
  * Claude (in SKILL.md orchestration) is the synthesis half: takes the JSON
    context emitted by ``prepare-context`` and produces new prose + frontmatter
    field updates.

Subcommands:
  * ``prepare-context`` — acquire ``.heal.lock`` (LOCK_EX|LOCK_NB), read
    activity.jsonl (current week only), activity-summary.json (if present),
    all anecdotes, and current persona; emit a JSON context blob to stdout.
    The lock is held until the process exits, so the caller is expected to
    pipe directly into ``write``.
  * ``write`` — read the new prose from stdin (or ``--prose-file``), bump
    frontmatter version + updated_at, optionally apply caller-supplied
    activity counters via ``--activity-json``, atomically write persona.md
    (backing up first), print a unified diff to stderr, and exit 0.
  * ``rotate`` — rotate ``activity.jsonl`` weekly (rename to
    ``activity-YYYY-WW.jsonl``) and compact archived weeks into
    ``activity-summary.json`` on a month boundary. Idempotent.

Stdlib only. Python 3.9+.

The intended pipeline shape is:

    python3 -m scripts.heal prepare-context  \\
      | <Claude rewrites prose, prints new prose to stdout>  \\
      | python3 -m scripts.heal write

Design notes:
  * stdout is reserved for machine-readable output; warnings/errors and the
    diff go to stderr so callers can pipe safely.
  * heal-errors.jsonl entries are atomic appends, ``\u22644096`` bytes per line.
  * AI_QUICKSTART_HOME env var overrides ``~`` for tests.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make sibling ``persona`` importable when run as a script (``python3 heal.py``)
# AND from tests that put scripts/ on sys.path. Always use bare ``persona`` so
# tests' ``monkeypatch.setattr(persona, ...)`` reaches the same module instance
# (PEP 420 namespace packages would otherwise produce two separate modules).
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))
import persona  # type: ignore  # noqa: E402


# ---------- constants & paths ----------

ACTIVITY_FILE = "activity.jsonl"
ACTIVITY_SUMMARY = "activity-summary.json"
PERSONA_FILE = "persona.md"
PERSONA_BAK = "persona.md.bak"
HEAL_LOCK = ".heal.lock"
HEAL_ERRORS_FILE = "heal-errors.jsonl"
PERSONA_SUBDIR = "persona"
ANECDOTES_SUBDIR = "anecdotes"
ROOT_DIR_NAME = ".ai-quickstart"

# Max bytes per heal-errors.jsonl line so POSIX appends stay atomic
# (matches the activity.jsonl invariant from PLAN.md).
MAX_ERROR_LINE_BYTES = 4096


def _home_root(home: Optional[Path] = None) -> Path:
    """Return the ai-quickstart home dir.

    Resolution order:
      1. explicit ``home`` argument (used by tests)
      2. ``$AI_QUICKSTART_HOME`` env var
      3. ``~/.ai-quickstart``
    """
    if home is not None:
        return Path(home)
    env = os.environ.get("AI_QUICKSTART_HOME")
    if env:
        return Path(env)
    return Path.home() / ROOT_DIR_NAME


def _persona_dir(home: Optional[Path] = None) -> Path:
    return _home_root(home) / PERSONA_SUBDIR


def _persona_path(home: Optional[Path] = None) -> Path:
    return _persona_dir(home) / PERSONA_FILE


def _persona_bak_path(home: Optional[Path] = None) -> Path:
    return _persona_dir(home) / PERSONA_BAK


def _activity_path(home: Optional[Path] = None) -> Path:
    return _persona_dir(home) / ACTIVITY_FILE


def _activity_summary_path(home: Optional[Path] = None) -> Path:
    return _persona_dir(home) / ACTIVITY_SUMMARY


def _anecdotes_dir(home: Optional[Path] = None) -> Path:
    return _persona_dir(home) / ANECDOTES_SUBDIR


def _heal_lock_path(home: Optional[Path] = None) -> Path:
    return _persona_dir(home) / HEAL_LOCK


def _heal_errors_path(home: Optional[Path] = None) -> Path:
    return _home_root(home) / HEAL_ERRORS_FILE


def _ensure_persona_dirs(home: Optional[Path] = None) -> None:
    """Make sure persona/ and anecdotes/ exist."""
    _persona_dir(home).mkdir(parents=True, exist_ok=True)
    _anecdotes_dir(home).mkdir(parents=True, exist_ok=True)


# ---------- time helpers ----------

def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_week_key(ts: _dt.datetime) -> str:
    """Return an ISO-week archive key like ``2026-17`` for ``ts``."""
    iso_year, iso_week, _ = ts.isocalendar()
    return f"{iso_year:04d}-{iso_week:02d}"


def _archive_filename(ts: _dt.datetime) -> str:
    return f"activity-{_iso_week_key(ts)}.jsonl"


def _archive_glob_pattern() -> str:
    return "activity-*.jsonl"


# ---------- error logging ----------

def _log_heal_error(
    phase: str,
    error: str,
    tb_first_line: str,
    home: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> None:
    """Append a single JSON line to ~/.ai-quickstart/heal-errors.jsonl.

    Best-effort: any failure here is itself swallowed (we don't want a
    failed error-log to mask the real heal error). Lines are truncated
    to ``MAX_ERROR_LINE_BYTES`` so POSIX appends remain atomic.
    """
    try:
        record: Dict[str, Any] = {
            "ts": _utcnow_iso(),
            "phase": phase,
            "error": str(error)[:1024],
            "traceback_first_line": str(tb_first_line)[:512],
        }
        if run_id:
            record["run_id"] = run_id
        line = json.dumps(record, ensure_ascii=False)
        encoded = line.encode("utf-8")
        # Reserve newline byte. Truncate the ``error`` field iteratively until
        # the encoded line fits. JSON escape sequences can grow the string
        # nontrivially, so we shrink and re-check rather than computing once.
        while len(encoded) + 1 > MAX_ERROR_LINE_BYTES and record["error"]:
            # Halve the error field each iteration. Worst case: O(log N) re-serializations.
            record["error"] = record["error"][: max(1, len(record["error"]) // 2)]
            line = json.dumps(record, ensure_ascii=False)
            encoded = line.encode("utf-8")
            if len(record["error"]) <= 1:
                # Final defensive trim: if even an empty error field leaves us
                # too big (shouldn't happen with our schema), drop the field.
                if len(encoded) + 1 > MAX_ERROR_LINE_BYTES:
                    record.pop("error", None)
                    line = json.dumps(record, ensure_ascii=False)
                    encoded = line.encode("utf-8")
                break
        path = _heal_errors_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # pylint: disable=broad-except
        # Never let error logging itself crash heal.
        pass


def _first_traceback_line(exc: BaseException) -> str:
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    if not tb:
        return ""
    # The first line of format_exception is the "Traceback (most recent call last):"
    # banner; the second line is typically the location frame which is more useful.
    if len(tb) >= 2:
        return tb[1].strip().splitlines()[0] if tb[1].strip() else tb[0].strip()
    return tb[0].strip()


# ---------- locking ----------

class _LockHandle:
    """Holds an open file descriptor with an exclusive flock.

    The lock is released when the file is closed (process exit also releases
    it). We expose ``release()`` for explicit cleanup in tests.
    """

    def __init__(self, fd: int, path: Path):
        self.fd = fd
        self.path = path
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self._released = True


def _acquire_lock(home: Optional[Path] = None) -> _LockHandle:
    """Acquire an exclusive non-blocking flock on ``.heal.lock``.

    Raises ``BlockingIOError`` if another holder has the lock.
    """
    _ensure_persona_dirs(home)
    lock_path = _heal_lock_path(home)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    except OSError:
        os.close(fd)
        raise
    return _LockHandle(fd, lock_path)


# ---------- activity reading ----------

def _read_activity_current_week(
    home: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Read activity.jsonl, returning (entries, malformed_skipped).

    "Current week only" means: callers should have already rotated archived
    weeks out via ``rotate``. activity.jsonl by convention contains only the
    current ISO week's events, but we also defensively filter by week here
    so a missing/skipped rotation doesn't poison the heal context.
    """
    path = _activity_path(home)
    if not path.exists():
        return [], 0
    entries: List[Dict[str, Any]] = []
    malformed = 0
    week_key = _iso_week_key(now or _utcnow())
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(obj, dict):
                    malformed += 1
                    continue
                # Defensive: skip entries from a different ISO week.
                ts = obj.get("ts")
                if isinstance(ts, str):
                    parsed = _parse_iso_ts(ts)
                    if parsed is not None and _iso_week_key(parsed) != week_key:
                        # Out-of-week entries are NOT counted as malformed —
                        # they'll be picked up by the next rotation. We just
                        # keep them out of the heal context.
                        continue
                entries.append(obj)
    except OSError:
        # Disk read failure: treat as zero entries and return; caller's
        # higher-level error handler will surface this if it matters.
        return [], 0
    return entries, malformed


def _parse_iso_ts(ts: str) -> Optional[_dt.datetime]:
    """Parse an ISO 8601 timestamp; return None on failure.

    Accepts both ``...Z`` and ``...+00:00`` forms.
    """
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(ts)
    except ValueError:
        return None


def _read_activity_summary(home: Optional[Path] = None) -> Dict[str, Any]:
    path = _activity_summary_path(home)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------- anecdotes reading ----------

def _read_anecdotes(home: Optional[Path] = None) -> List[Dict[str, str]]:
    """Return [{slug, content}, ...] for each anecdote markdown file.

    Sorted by slug for determinism.
    """
    d = _anecdotes_dir(home)
    if not d.exists():
        return []
    out: List[Dict[str, str]] = []
    for p in sorted(d.glob("*.md")):
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        out.append({"slug": p.stem, "content": content})
    return out


# ---------- atomic file IO ----------

def _atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Write ``content`` to ``path`` atomically via tmp+rename.

    The tmp file lives in the same directory so ``os.replace`` is atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".tmp-",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # Some filesystems don't support fsync; replace is still atomic.
                pass
        os.chmod(tmp_path, mode)
        os.replace(str(tmp_path), str(path))
    except Exception:
        # Clean up tmp on failure so partial writes don't litter the dir.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------- subcommand: prepare-context ----------

def cmd_prepare_context(
    home: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
    stdout=None,
    stderr=None,
) -> int:
    """Acquire the heal lock and emit a JSON context blob on stdout.

    The lock is held until the process exits. Callers are expected to pipe
    stdout into Claude and pipe Claude's rewritten prose into ``write``.

    On lock contention, prints ``heal in progress`` to stderr and exits 2.
    On any other error, logs to heal-errors.jsonl and exits 1.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    run_id = uuid.uuid4().hex

    try:
        try:
            _acquire_lock(home)
        except BlockingIOError:
            err.write("heal in progress\n")
            return 2

        # Read all heal inputs.
        ppath = _persona_path(home)
        parsed = persona.parse_persona(ppath)
        current_frontmatter = parsed["frontmatter"]
        current_prose = parsed["prose"]

        anecdotes = _read_anecdotes(home)
        activity_recent, malformed = _read_activity_current_week(home, now=now)
        activity_summary = _read_activity_summary(home)

        context = {
            "lock_acquired": True,
            "run_id": run_id,
            "current_frontmatter": current_frontmatter,
            "current_prose": current_prose,
            "anecdotes": anecdotes,
            "activity_recent": activity_recent,
            "activity_summary": activity_summary,
            "malformed_lines_skipped": malformed,
        }
        out.write(json.dumps(context, ensure_ascii=False) + "\n")
        out.flush()
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        _log_heal_error(
            phase="prepare-context",
            error=repr(exc),
            tb_first_line=_first_traceback_line(exc),
            home=home,
            run_id=run_id,
        )
        err.write(f"heal prepare-context failed: {exc}\n")
        return 1


# ---------- subcommand: write ----------

def _read_prose_input(prose_file: Optional[str], stdin) -> str:
    """Read new prose from --prose-file or stdin."""
    if prose_file:
        return Path(prose_file).read_text(encoding="utf-8")
    return stdin.read()


def _apply_activity_overrides(
    fm: Dict[str, Any],
    overrides_json: Optional[str],
    err=None,
) -> None:
    """Merge caller-supplied activity counters into the frontmatter in-place.

    ``overrides_json`` is a JSON object with any subset of:
      project_count, total_skill_uses, top_projects, last_active
    Unrecognized keys are ignored. Type-mismatched values are dropped with
    a stderr warning so we never write garbage. ``err`` lets callers capture
    warnings (defaults to ``sys.stderr``).
    """
    err_stream = err if err is not None else sys.stderr
    if not overrides_json:
        return
    try:
        overrides = json.loads(overrides_json)
    except json.JSONDecodeError as e:
        err_stream.write(f"[heal] warning: --activity-json is not valid JSON: {e}\n")
        return
    if not isinstance(overrides, dict):
        err_stream.write("[heal] warning: --activity-json must be a JSON object\n")
        return
    activity = fm.setdefault("activity", {})
    if not isinstance(activity, dict):
        activity = {}
        fm["activity"] = activity
    allowed = {
        "project_count": int,
        "total_skill_uses": int,
        "top_projects": list,
        "last_active": str,
    }
    for k, expected_type in allowed.items():
        if k not in overrides:
            continue
        v = overrides[k]
        if expected_type is list:
            if not isinstance(v, list):
                err_stream.write(f"[heal] warning: activity.{k} must be a list; ignored\n")
                continue
        else:
            if not isinstance(v, expected_type) or isinstance(v, bool):
                err_stream.write(
                    f"[heal] warning: activity.{k} has wrong type ({type(v).__name__}); ignored\n"
                )
                continue
        activity[k] = v


def cmd_write(
    home: Optional[Path] = None,
    prose_file: Optional[str] = None,
    activity_json: Optional[str] = None,
    stdin=None,
    stdout=None,
    stderr=None,
) -> int:
    """Read new prose, write persona.md atomically with backup, print diff.

    Sequence:
      1. Read new prose (stdin or --prose-file).
      2. Parse current persona to get the prior frontmatter + prose.
      3. Make a manual backup of persona.md -> persona.md.bak (if it exists)
         so we have a known-good restoration point even if write_persona's
         own backup logic fails. This is the belt-and-suspenders requested
         by PLAN.md "leaves prior persona.md untouched on heal failure."
      4. Apply --activity-json overrides + bump frontmatter via persona.write_persona.
      5. Compute diff old vs new and print to stderr.
      6. On any error mid-flight: restore from .bak, log to heal-errors.jsonl,
         exit non-zero.
    """
    sin = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    ppath = _persona_path(home)
    bak_path = _persona_bak_path(home)

    pre_existing_bytes: Optional[bytes] = None
    pre_existing = ppath.exists()

    try:
        new_prose = _read_prose_input(prose_file, sin)
        # Snapshot current state before any mutation.
        if pre_existing:
            pre_existing_bytes = ppath.read_bytes()
            # Manual .bak before persona.write_persona's own backup.
            _ensure_persona_dirs(home)
            bak_path.write_bytes(pre_existing_bytes)

        parsed = persona.parse_persona(ppath)
        old_frontmatter = parsed["frontmatter"]
        old_prose = parsed["prose"]

        # Apply caller-supplied activity counter overrides BEFORE write so
        # write_persona's bump (updated_at, version) lands on top of them.
        new_frontmatter = _deep_copy_fm(old_frontmatter)
        _apply_activity_overrides(new_frontmatter, activity_json, err=err)

        persona.write_persona(ppath, new_frontmatter, new_prose)

        # Re-read the bumped frontmatter from disk so summary reflects the actual
        # written version (write_persona bumps its internal copy, not the caller's).
        try:
            written = persona.parse_persona(ppath)
            written_version = written["frontmatter"].get("generated", {}).get("version")
        except Exception:  # pragma: no cover - defensive
            written_version = new_frontmatter.get("generated", {}).get("version")

        # Diff the prose only (not frontmatter), per PLAN.md "show diff to user."
        diff = persona.diff_persona(old_prose, new_prose)
        if diff:
            err.write(diff)
            if not diff.endswith("\n"):
                err.write("\n")
        else:
            err.write("[heal] no prose changes\n")

        # Echo a tiny summary on stdout so callers know it succeeded.
        summary = {
            "ok": True,
            "version": written_version,
            "persona_path": str(ppath),
            "backup_path": str(bak_path) if pre_existing else None,
        }
        out.write(json.dumps(summary, ensure_ascii=False) + "\n")
        out.flush()
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        # Restore from .bak if a partial write may have happened.
        try:
            if pre_existing and pre_existing_bytes is not None:
                # If persona.md was overwritten or removed, put it back.
                if not ppath.exists() or ppath.read_bytes() != pre_existing_bytes:
                    ppath.write_bytes(pre_existing_bytes)
        except OSError:
            # Restoration failure is logged below alongside the original error.
            pass
        _log_heal_error(
            phase="write",
            error=repr(exc),
            tb_first_line=_first_traceback_line(exc),
            home=home,
        )
        err.write(f"heal write failed: {exc}\n")
        return 1


def _deep_copy_fm(fm: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow + one-level-deep copy that mirrors the persona schema shape."""
    out: Dict[str, Any] = {}
    for k, v in fm.items():
        if isinstance(v, dict):
            out[k] = dict(v)
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out


# ---------- subcommand: rotate ----------

def cmd_rotate(
    home: Optional[Path] = None,
    now: Optional[_dt.datetime] = None,
    stdout=None,
    stderr=None,
) -> int:
    """Rotate activity.jsonl weekly and aggregate archives monthly.

    Weekly: if today's ISO week is later than the ISO week of activity.jsonl's
    last mtime, rename activity.jsonl -> activity-YYYY-WW.jsonl (using the
    week of the last mtime so the archive name reflects the data, not the
    moment of rotation).

    Monthly: if today's calendar month differs from the month of the youngest
    archive AND there's at least one archived week, compact all archived
    weekly files into activity-summary.json. Idempotent: if the summary is
    already up to date, no work happens.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    ts_now = now if now is not None else _utcnow()

    try:
        _ensure_persona_dirs(home)
        result: Dict[str, Any] = {"rotated": False, "aggregated": False}

        # ---- weekly rotation ----
        act_path = _activity_path(home)
        if act_path.exists():
            mtime = _dt.datetime.fromtimestamp(act_path.stat().st_mtime, tz=_dt.timezone.utc)
            mtime_week = _iso_week_key(mtime)
            now_week = _iso_week_key(ts_now)
            if now_week > mtime_week:
                archive_name = _archive_filename(mtime)
                archive_path = act_path.parent / archive_name
                # If an archive already exists, append rather than replace (idempotent).
                if archive_path.exists():
                    existing = archive_path.read_bytes()
                    new_data = act_path.read_bytes()
                    archive_path.write_bytes(existing + new_data)
                    act_path.unlink()
                else:
                    os.replace(str(act_path), str(archive_path))
                result["rotated"] = True
                result["archive"] = archive_name

        # ---- monthly aggregation ----
        archives = sorted(_persona_dir(home).glob(_archive_glob_pattern()))
        summary_path = _activity_summary_path(home)
        if archives:
            # Determine month of the youngest archive's mtime.
            youngest_mtime = max(p.stat().st_mtime for p in archives)
            youngest_month = _dt.datetime.fromtimestamp(
                youngest_mtime, tz=_dt.timezone.utc
            ).strftime("%Y-%m")
            now_month = ts_now.strftime("%Y-%m")
            # Aggregate when we've crossed a month boundary OR the summary
            # is missing/empty. This is idempotent because we recompute from
            # scratch and write only if the result differs from on-disk.
            should_aggregate = (now_month != youngest_month) or (not summary_path.exists())
            if should_aggregate:
                summary = _aggregate_archives(archives)
                existing_summary = _read_activity_summary(home)
                # Compare on the data fields only (ignore generated_at) so
                # back-to-back rotate calls are idempotent at the file level.
                summary_data = summary.get("weeks", {})
                existing_data = existing_summary.get("weeks", {}) if isinstance(
                    existing_summary, dict
                ) else {}
                if summary_data != existing_data:
                    _atomic_write_text(
                        summary_path,
                        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    )
                    result["aggregated"] = True
                    result["summary_path"] = str(summary_path)

        out.write(json.dumps(result, ensure_ascii=False) + "\n")
        out.flush()
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        _log_heal_error(
            phase="rotate",
            error=repr(exc),
            tb_first_line=_first_traceback_line(exc),
            home=home,
        )
        err.write(f"heal rotate failed: {exc}\n")
        return 1


def _aggregate_archives(archive_paths: List[Path]) -> Dict[str, Any]:
    """Build the activity-summary.json structure from archived weekly files.

    Output shape::

        {
          "weeks": {
            "2026-17": {
              "project_counts": {"alpha": 5, "beta": 3},
              "top_skills":     [{"name": "compathy", "count": 4}, ...],
              "durations":      {"total_seconds": 0, "event_count": 8}
            },
            ...
          },
          "generated_at": "2026-04-29T..."
        }
    """
    weeks: Dict[str, Dict[str, Any]] = {}
    for path in archive_paths:
        # Parse week key from filename: activity-YYYY-WW.jsonl
        stem = path.stem  # activity-YYYY-WW
        if not stem.startswith("activity-"):
            continue
        week_key = stem[len("activity-"):]
        agg = weeks.setdefault(
            week_key,
            {
                "project_counts": {},
                "skill_counts": {},
                "durations": {"total_seconds": 0, "event_count": 0},
            },
        )
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    # Project counts: bucket by cwd basename if present.
                    cwd = obj.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        slug = os.path.basename(cwd.rstrip("/")) or cwd
                        agg["project_counts"][slug] = (
                            agg["project_counts"].get(slug, 0) + 1
                        )
                    # Skill counts.
                    if obj.get("event") == "skill":
                        skill = obj.get("skill")
                        if isinstance(skill, str) and skill:
                            agg["skill_counts"][skill] = (
                                agg["skill_counts"].get(skill, 0) + 1
                            )
                    # Durations.
                    duration = obj.get("duration_s")
                    if isinstance(duration, (int, float)):
                        agg["durations"]["total_seconds"] += float(duration)
                    agg["durations"]["event_count"] += 1
        except OSError:
            continue

    # Project skill_counts -> top_skills sorted list, drop the dict.
    out_weeks: Dict[str, Any] = {}
    for wk, agg in weeks.items():
        top_skills = sorted(
            ({"name": k, "count": v} for k, v in agg["skill_counts"].items()),
            key=lambda x: (-x["count"], x["name"]),
        )
        out_weeks[wk] = {
            "project_counts": agg["project_counts"],
            "top_skills": top_skills,
            "durations": agg["durations"],
        }

    return {
        "weeks": out_weeks,
        "generated_at": _utcnow_iso(),
    }


# ---------- argparse wiring ----------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heal",
        description=(
            "Persona heal CLI: prepare-context (read+lock), write "
            "(accept new prose, atomic write+diff), rotate (weekly/monthly "
            "activity.jsonl maintenance)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sub.add_parser(
        "prepare-context",
        help="Acquire heal lock, read inputs, emit JSON context to stdout",
    )

    p_write = sub.add_parser(
        "write",
        help="Read new prose from stdin (or --prose-file), atomic write+diff",
    )
    p_write.add_argument(
        "--prose-file",
        default=None,
        help="Read new prose from this file instead of stdin",
    )
    p_write.add_argument(
        "--activity-json",
        default=None,
        help=(
            "Optional JSON object with frontmatter.activity overrides "
            "(project_count, total_skill_uses, top_projects, last_active)"
        ),
    )

    sub.add_parser(
        "rotate",
        help="Rotate activity.jsonl weekly; aggregate archives monthly",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "prepare-context":
        return cmd_prepare_context()
    if args.cmd == "write":
        return cmd_write(prose_file=args.prose_file, activity_json=args.activity_json)
    if args.cmd == "rotate":
        return cmd_rotate()
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
