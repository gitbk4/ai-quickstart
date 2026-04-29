"""File-backed JSON TTL cache used by source modules.

Stores cache entries under `${AI_QUICKSTART_HOME}/cache/{namespace}/{safe_key}.json`
(default home is `~/.ai-quickstart`). Each entry is a JSON document of shape
`{"written_at": ISO_TS, "value": <dict>}`. Writes are atomic (tmp file on the
same dir + os.replace).

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Characters that are unsafe or annoying in filesystem paths. We replace any
# run of these (plus whitespace and any URL query separator) with a single
# hyphen so generated filenames stay readable.
_UNSAFE_RE = re.compile(r"[\\/:?#&=\s]+")
_MAX_KEY_LEN = 200
_HASH_SUFFIX_LEN = 12  # sha256 prefix appended on truncation


def _home() -> Path:
    """Return the configured ai-quickstart home directory."""
    env = os.environ.get("AI_QUICKSTART_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".ai-quickstart"


def _cache_root() -> Path:
    return _home() / "cache"


def _namespace_dir(namespace: str) -> Path:
    return _cache_root() / namespace


def _entry_path(namespace: str, key: str) -> Path:
    return _namespace_dir(namespace) / f"{safe_key(key)}.json"


def safe_key(raw_key: str) -> str:
    """Sanitize an arbitrary string for use as a cache filename.

    Replaces filesystem-unsafe characters (path separators, colons, query
    string separators, whitespace) with hyphens, collapses repeated hyphens,
    and truncates to ``_MAX_KEY_LEN`` characters. When truncation occurs (or
    when sanitization removes content), a sha256 prefix of the original key
    is appended to avoid collisions between distinct inputs that normalize
    to the same prefix.
    """
    if raw_key is None:
        raw_key = ""
    sanitized = _UNSAFE_RE.sub("-", raw_key)
    # Collapse repeated hyphens introduced by adjacent unsafe runs.
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    if not sanitized:
        sanitized = "key"

    # Always compute the digest; we only attach it when truncation happens
    # or when sanitization meaningfully altered the key (which is the only
    # scenario where two different raw keys could collide on the prefix).
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:_HASH_SUFFIX_LEN]

    # Reserve room for the digest suffix when we know we'll need it.
    suffix = f"-{digest}"
    truncated = len(sanitized) > _MAX_KEY_LEN
    altered = sanitized != raw_key

    if truncated:
        keep = _MAX_KEY_LEN - len(suffix)
        if keep < 1:
            keep = 1
        return sanitized[:keep] + suffix
    if altered:
        # Append the digest, but still respect the max length.
        candidate = sanitized + suffix
        if len(candidate) > _MAX_KEY_LEN:
            keep = _MAX_KEY_LEN - len(suffix)
            candidate = sanitized[:keep] + suffix
        return candidate
    return sanitized


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in the destination dir guarantees os.replace is atomic.
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=".tmp-",
        suffix=".json",
    )
    try:
        json.dump(payload, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    except Exception:
        # If anything went wrong, clean up the tmp file.
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(raw: str) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        # Support trailing "Z" (Zulu) just in case a writer used it.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def get(namespace: str, key: str, ttl_seconds: int) -> Optional[dict]:
    """Return the cached value if present and within TTL, else None.

    A partially-written or otherwise unreadable cache file returns None
    rather than raising — corrupt cache should never crash callers.
    """
    path = _entry_path(namespace, key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        # Truncated, partially-written, or otherwise invalid JSON.
        return None
    if not isinstance(payload, dict):
        return None
    written_at = _parse_ts(payload.get("written_at", ""))
    if written_at is None:
        return None
    age = (datetime.now(timezone.utc) - written_at).total_seconds()
    if age >= ttl_seconds:
        return None
    value = payload.get("value")
    if not isinstance(value, dict):
        return None
    return value


def set(namespace: str, key: str, value: dict) -> None:  # noqa: A001 - matches spec
    """Write ``value`` to the cache under ``(namespace, key)``."""
    if not isinstance(value, dict):
        raise TypeError("cache value must be a dict")
    path = _entry_path(namespace, key)
    payload = {"written_at": _now_iso(), "value": value}
    _atomic_write_json(path, payload)


def invalidate(namespace: str, key: str) -> None:
    """Remove the cache file for ``(namespace, key)``. No-op if missing."""
    path = _entry_path(namespace, key)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def clear(namespace: str) -> None:
    """Remove all cache files inside ``namespace``. No-op if dir missing.

    Other namespaces are untouched. The namespace dir itself is removed.
    """
    ns_dir = _namespace_dir(namespace)
    if not ns_dir.exists():
        return
    shutil.rmtree(ns_dir)
