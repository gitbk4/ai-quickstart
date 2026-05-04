"""ai-quickstart PostToolUse hook handler.

Reads a Claude Code PostToolUse event JSON from stdin, looks up the cwd in
``~/.ai-quickstart/managed-projects.json``, and (if matched) appends a
single JSONL line to ``~/.ai-quickstart/persona/activity.jsonl``.

CRITICAL: this script must NEVER crash Claude Code. Any error -> exit 0 silently.

The append uses ``O_APPEND`` so concurrent writes from multiple Claude Code
processes are atomic on local POSIX filesystems as long as the line is
<= ``PIPE_BUF`` (~4096 bytes). The script enforces a 4096-byte cap by
truncating the ``file`` field if necessary.

After a successful append, the hook also checks whether the activity log has
accumulated more than ``AUTO_HEAL_THRESHOLD`` entries since the last
successful heal. If so AND the heal lock is not currently held, the hook
spawns a detached subprocess that writes a ``.heal-pending`` sentinel marker;
the next interactive ``/ai-quickstart`` invocation picks up that sentinel and
runs a full heal pipeline. The hook never blocks on this trigger -- the
subprocess is detached and any failure is swallowed silently.

Stdlib only. Python 3.9+ compatible.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

MAX_LINE_BYTES = 4096

# Auto-heal trigger threshold. When activity.jsonl accumulates >= this many
# new entries since the last successful heal, the next hook invocation that
# crosses the threshold spawns a detached heal-trigger subprocess. The value
# is a refinement knob (see PLAN.md "Auto-heal threshold"); 20 entries is a
# rough sweet spot -- enough to amortize heal cost across many tool uses but
# low enough that the persona stays current within a single working session.
AUTO_HEAL_THRESHOLD = 20


def _ai_quickstart_home() -> Path:
    override = os.environ.get("AI_QUICKSTART_HOME")
    if override:
        return Path(override)
    return Path.home() / ".ai-quickstart"


def _managed_projects_path() -> Path:
    return _ai_quickstart_home() / "managed-projects.json"


def _activity_path() -> Path:
    return _ai_quickstart_home() / "persona" / "activity.jsonl"


def _heal_lock_path() -> Path:
    return _ai_quickstart_home() / "persona" / ".heal.lock"


def _last_heal_state_path() -> Path:
    """State file recording the activity.jsonl byte offset at last heal.

    Shape: ``{"offset": <int>, "ts": "<iso8601>"}``. ``offset`` is the size
    of activity.jsonl (in bytes) at the moment heal.write last succeeded;
    "entries since last heal" = newline-terminated lines after that byte.
    Missing/corrupt file -> offset 0 (count from the beginning of the file).
    """
    return _ai_quickstart_home() / "persona" / ".last-heal.json"


def _heal_pending_path() -> Path:
    """Sentinel marker written by the detached trigger subprocess.

    Presence of this file signals the next interactive /ai-quickstart flow
    to run a heal pipeline. The file is removed by the heal write success
    path; we don't unlink it from the hook.
    """
    return _ai_quickstart_home() / "persona" / ".heal-pending"


def _heal_script_path() -> Path:
    """Best-effort path to scripts/heal.py for the trigger subprocess.

    Resolved relative to this file so the hook works whether the skill is
    installed under ``~/.claude/skills/ai-quickstart/`` or run from a dev
    checkout. The caller falls back gracefully if the path doesn't exist.
    """
    return Path(__file__).resolve().parent / "heal.py"


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_event_from_stdin() -> dict:
    """Read PostToolUse event JSON from stdin. Empty/malformed -> {}."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _extract_cwd(event: dict) -> str:
    """Find the cwd: explicit field on event, then env $PWD as fallback."""
    for key in ("cwd", "working_directory"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    pwd = os.environ.get("PWD")
    if pwd:
        return pwd
    try:
        return os.getcwd()
    except OSError:
        return ""


def _extract_tool(event: dict) -> str:
    for key in ("tool", "tool_name", "name"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    return "unknown"


def _extract_run_id(event: dict):
    for key in ("run_id", "session_id"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_file(event: dict):
    """Best-effort extract of an edited/written file path from the event."""
    direct = event.get("file") or event.get("file_path")
    if isinstance(direct, str) and direct:
        return direct
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("file_path", "path", "file", "notebook_path"):
            val = tool_input.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _is_managed(cwd: str) -> bool:
    """Return True iff cwd is listed in the managed-projects registry."""
    if not cwd:
        return False
    path = _managed_projects_path()
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, list):
        return False
    return cwd in data


def _build_record(event: dict, cwd: str) -> dict:
    tool = _extract_tool(event)
    rec = {
        "ts": _utc_now_iso(),
        "event": "tool_use",
        "tool": tool,
        "cwd": cwd,
    }
    run_id = _extract_run_id(event)
    if run_id:
        rec["run_id"] = run_id
    file_path = _extract_file(event)
    if file_path:
        rec["file"] = file_path
    return rec


def _serialize_capped(record: dict) -> bytes:
    """Serialize ``record`` as a single JSONL line within MAX_LINE_BYTES.

    If the line is too long, truncates the ``file`` field (the only realistic
    source of unbounded growth) until the whole line fits, then appends ``\\n``.
    """
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    encoded = line.encode("utf-8") + b"\n"
    if len(encoded) <= MAX_LINE_BYTES:
        return encoded

    # Truncate the file field if present.
    if "file" in record and isinstance(record["file"], str):
        # Compute how much room we have for the file value.
        candidate = dict(record)
        candidate["file"] = ""
        empty_line = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        empty_encoded = empty_line.encode("utf-8") + b"\n"
        # Bytes still available for the file value (utf-8).
        budget = MAX_LINE_BYTES - len(empty_encoded)
        if budget > 0:
            # Truncate by bytes, not characters, to be safe.
            file_bytes = record["file"].encode("utf-8")[:budget]
            # Re-decode, dropping any partial trailing multibyte char.
            truncated = file_bytes.decode("utf-8", errors="ignore")
            candidate["file"] = truncated
            line = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            encoded = line.encode("utf-8") + b"\n"
            if len(encoded) <= MAX_LINE_BYTES:
                return encoded
        # Drop the file field entirely if even an empty value won't fit.
        candidate.pop("file", None)
        line = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        encoded = line.encode("utf-8") + b"\n"
        if len(encoded) <= MAX_LINE_BYTES:
            return encoded

    # Last-resort: hard byte truncation. Should not happen with our schema.
    return encoded[: MAX_LINE_BYTES - 1] + b"\n"


def _append_line(path: Path, payload: bytes) -> None:
    """Append ``payload`` to ``path`` using O_APPEND for atomic small writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(str(path), flags, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Auto-heal threshold trigger.
#
# Design:
#   * "entries since last heal" is computed by reading activity.jsonl from
#     the byte offset recorded in .last-heal.json (default 0 if absent) and
#     counting newline-terminated lines after that point.
#   * Heal write success resets the offset (heal.py owns that side).
#   * If the count >= AUTO_HEAL_THRESHOLD AND no heal is currently in
#     progress (we sniff the .heal.lock with a non-blocking flock), we
#     spawn a detached subprocess that writes a .heal-pending sentinel.
#     The subprocess is intentionally minimal -- it does NOT acquire the
#     heal lock (full heal needs LLM synthesis, which only the interactive
#     /ai-quickstart pipeline can drive). The sentinel signals the next
#     /ai-quickstart invocation to run a heal.
#   * Every step is wrapped in try/except. Any failure (lock contention,
#     subprocess spawn failure, missing heal.py, disk error) is swallowed
#     silently so the hook never blocks Claude Code.
# ---------------------------------------------------------------------------


def _read_last_heal_offset() -> int:
    """Return the byte offset of activity.jsonl at the last successful heal.

    Returns 0 on missing file, parse failure, or out-of-range values so the
    caller treats the entire file as "since last heal".
    """
    try:
        with open(_last_heal_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    offset = data.get("offset")
    if not isinstance(offset, int) or offset < 0:
        return 0
    return offset


def _count_entries_since_offset(activity_path: Path, offset: int) -> int:
    """Return the number of newline-terminated lines after ``offset``.

    Soft-fails to 0 on any IO error: the caller treats that as "below
    threshold" rather than mistakenly triggering a heal on a flaky read.
    """
    try:
        size = activity_path.stat().st_size
    except OSError:
        return 0
    if offset > size:
        # The activity file shrank (rotation, manual edit). Treat as a
        # fresh start: count from 0. We don't rewrite the offset here --
        # heal write owns that.
        offset = 0
    if size == offset:
        return 0
    try:
        with open(activity_path, "rb") as fh:
            try:
                fh.seek(offset)
            except OSError:
                return 0
            count = 0
            for _ in fh:
                count += 1
            return count
    except OSError:
        return 0


def _heal_in_progress() -> bool:
    """Return True iff another process holds the heal flock.

    We open the lock file and try a non-blocking exclusive acquire. If we
    get it, we release it immediately -- we're only sniffing. If we can't
    create/open the lock file at all, we conservatively report "not in
    progress" so spurious filesystem errors don't permanently disable
    auto-heal triggering.
    """
    lock_path = _heal_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            # Some filesystems (NFS) refuse flock entirely. Treat as "not
            # in progress" so we don't permanently disable triggering.
            return False
        # We got the lock; release immediately.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _spawn_heal_trigger() -> None:
    """Spawn a detached subprocess that writes the .heal-pending sentinel.

    The subprocess is fully detached (start_new_session=True, stdio
    redirected to /dev/null) so the hook returns immediately even if the
    spawned process is slow. Any failure (missing python, missing heal.py,
    permission error) is swallowed -- the next hook tick will retry.
    """
    heal_py = _heal_script_path()
    if not heal_py.exists():
        return
    try:
        devnull = open(os.devnull, "rb+")
    except OSError:
        return
    try:
        # We pass AI_QUICKSTART_HOME explicitly so the subprocess sees the
        # same home as the hook (matters for tests and tmp-dir overrides).
        env = dict(os.environ)
        # Fire and forget: the subprocess is detached via start_new_session
        # so it survives if our parent exits, and stdio is /dev/null so it
        # can't block on our pipes. We intentionally drop the Popen handle.
        subprocess.Popen(
            [sys.executable or "python3", str(heal_py), "auto-heal-trigger"],
            stdin=devnull,
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except (OSError, ValueError):
        # subprocess.Popen can raise on EAGAIN, E2BIG, exec failure, etc.
        # The hook never propagates these.
        pass
    finally:
        try:
            devnull.close()
        except OSError:
            pass


def _maybe_trigger_auto_heal() -> None:
    """Check the auto-heal threshold and spawn a trigger if it's exceeded.

    Called after every successful activity.jsonl append. All exceptions are
    swallowed -- the hook MUST NEVER crash Claude Code (see invariant at
    top of file).
    """
    try:
        activity_path = _activity_path()
        if not activity_path.exists():
            return
        offset = _read_last_heal_offset()
        count = _count_entries_since_offset(activity_path, offset)
        if count < AUTO_HEAL_THRESHOLD:
            return
        if _heal_in_progress():
            # Another heal is already running -- it'll reset the counter
            # when it finishes. Don't double-trigger.
            return
        _spawn_heal_trigger()
    except Exception:
        # Defensive: any unexpected failure must not crash the hook.
        return


def main() -> int:
    """Hook entry point. Always returns 0; never raises."""
    try:
        event = _read_event_from_stdin()
        cwd = _extract_cwd(event)
        if not _is_managed(cwd):
            return 0
        record = _build_record(event, cwd)
        payload = _serialize_capped(record)
        _append_line(_activity_path(), payload)
        _maybe_trigger_auto_heal()
    except Exception:
        # Hook MUST NEVER crash Claude Code.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
