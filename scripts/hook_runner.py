"""ai-quickstart PostToolUse hook handler.

Reads a Claude Code PostToolUse event JSON from stdin, looks up the cwd in
``~/.ai-quickstart/managed-projects.json``, and (if matched) appends a
single JSONL line to ``~/.ai-quickstart/persona/activity.jsonl``.

CRITICAL: this script must NEVER crash Claude Code. Any error -> exit 0 silently.

The append uses ``O_APPEND`` so concurrent writes from multiple Claude Code
processes are atomic on local POSIX filesystems as long as the line is
<= ``PIPE_BUF`` (~4096 bytes). The script enforces a 4096-byte cap by
truncating the ``file`` field if necessary.

Stdlib only. Python 3.9+ compatible.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

MAX_LINE_BYTES = 4096


def _ai_quickstart_home() -> Path:
    override = os.environ.get("AI_QUICKSTART_HOME")
    if override:
        return Path(override)
    return Path.home() / ".ai-quickstart"


def _managed_projects_path() -> Path:
    return _ai_quickstart_home() / "managed-projects.json"


def _activity_path() -> Path:
    return _ai_quickstart_home() / "persona" / "activity.jsonl"


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
    except Exception:
        # Hook MUST NEVER crash Claude Code.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
