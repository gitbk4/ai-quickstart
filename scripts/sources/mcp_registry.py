"""Wrapper around the Anthropic ``mcp-registry`` MCP tool.

The skill cannot directly call MCP tools from a Python script — those live
inside the Claude Code runtime. We approximate the call by shelling out to
the ``claude`` CLI when present (the CLI exposes ``mcp call`` for invoking
registered MCP tools). When the CLI is missing or returns malformed output,
we degrade to an empty result set with a warning so that
``scripts/suggest.py`` can render curated suggestions without crashing.

Results are cached under namespace ``mcp_registry`` with a 24-hour TTL via
:mod:`sources.cache`.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import List, Optional

from . import cache

CACHE_NAMESPACE = "mcp_registry"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
CLI_TIMEOUT = 20  # seconds

# The MCP registry tool name used by the Claude environment. Kept here as a
# single place to update if the upstream tool name changes.
MCP_TOOL_NAME = "mcp__mcp-registry__search_mcp_registry"


def search(keywords: List[str], limit: int = 20, force_refresh: bool = False) -> dict:
    """Search the MCP registry by ``keywords``.

    Returns a dict shaped::

        {
          "results": [ ... ],
          "source": "mcp-registry",
          "warnings": [ ... ],
        }

    ``results`` is a list of registry entries (passed through from the CLI
    output) capped at ``limit``. ``warnings`` is a list of human-readable
    strings; an empty list means no degradation occurred.

    The function never raises. Any failure mode (CLI missing, CLI errors,
    malformed JSON, timeout) yields ``results=[]`` plus an explanatory
    warning.
    """
    if not isinstance(keywords, (list, tuple)):
        keywords = [str(keywords)]
    keywords = [str(k) for k in keywords if k]

    cache_key = _cache_key(keywords, limit)
    if not force_refresh:
        cached = cache.get(CACHE_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

    cli_path = _find_claude_cli()
    if cli_path is None:
        return _empty(
            warning="claude CLI not found on PATH; MCP registry results unavailable",
        )

    proc_result = _invoke_cli(cli_path, keywords, limit)
    if "error" in proc_result:
        return _empty(warning=proc_result["error"])

    results = _extract_results(proc_result.get("payload"), limit)
    if results is None:
        return _empty(warning="claude CLI returned malformed mcp-registry payload")

    out = {
        "results": results,
        "source": "mcp-registry",
        "warnings": [],
    }
    try:
        cache.set(CACHE_NAMESPACE, cache_key, out)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _cache_key(keywords: List[str], limit: int) -> str:
    return f"q={'+'.join(sorted(keywords))}|n={limit}"


def _find_claude_cli() -> Optional[str]:
    """Return the path to the ``claude`` CLI, or None."""
    return shutil.which("claude")


def _invoke_cli(cli_path: str, keywords: List[str], limit: int) -> dict:
    """Run ``claude mcp call <tool> <json-args>`` and return the parsed payload.

    Returns ``{"payload": <parsed-json>}`` on success, or ``{"error": str}``
    on any failure.
    """
    args_json = json.dumps({"keywords": keywords, "limit": limit})
    cmd = [cli_path, "mcp", "call", MCP_TOOL_NAME, args_json]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return {"error": "claude CLI disappeared between detection and invocation"}
    except subprocess.TimeoutExpired:
        return {"error": "claude CLI timed out invoking mcp-registry"}
    except OSError as exc:
        return {"error": f"claude CLI failed to start: {exc}"}

    if completed.returncode != 0:
        snippet = (completed.stderr or completed.stdout or "").strip()[:200]
        return {"error": f"claude CLI exited {completed.returncode}: {snippet}"}

    stdout = completed.stdout or ""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return {"error": "claude CLI returned non-JSON output"}
    return {"payload": payload}


def _extract_results(payload, limit: int) -> Optional[List[dict]]:
    """Pull a list of result dicts out of the CLI payload.

    The CLI may shape the payload as either ``[ ... ]`` (raw list) or
    ``{"results": [ ... ]}``. Anything else is treated as malformed and we
    return None so the caller can surface a warning.
    """
    if payload is None:
        return None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        candidate = payload.get("results")
        if not isinstance(candidate, list):
            return None
        items = candidate
    else:
        return None
    # Drop any non-dict entries; clamp to ``limit``.
    cleaned = [item for item in items if isinstance(item, dict)]
    return cleaned[:limit]


def _empty(warning: str) -> dict:
    return {
        "results": [],
        "source": "mcp-registry",
        "warnings": [warning],
    }
