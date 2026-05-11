#!/usr/bin/env python3
"""Stdio MCP server exposing the user's persona as tools for MCP clients.

Speaks JSON-RPC 2.0 over stdio (line-delimited JSON, one message per line).
Implements only the minimum MCP surface needed to expose persona.json as
queryable tools:

  * initialize                  (server info + capabilities)
  * notifications/initialized   (silently accepted)
  * tools/list                  (enumerate the two persona tools)
  * tools/call                  (dispatch to the Python implementation)

Out of scope (deliberate): resources, prompts, sampling, roots,
subscriptions. Stdlib only, side-effect-free at import time. Coexistent
with the Wave 1B HTTP dashboard server (``scripts/dashboard/server.py``)
which exposes the same data over ``/persona/*``. This module is a
SEPARATE transport, NOT a wrapper around the HTTP server.

Tools exposed:

  persona_get_current()
      Return the full persona.json payload. If another process holds the
      heal lock at probe time, the returned payload is the on-disk
      contents with a ``stale: true`` field added. The probe uses
      ``fcntl.flock(LOCK_SH | LOCK_NB)`` and never blocks.

  persona_get_paragraph(paragraph_id)
      Return ``{id, text, provenance, trust_score, locked, anchored_to}``
      for one paragraph. ``paragraph_id`` must match ``p:NNN`` (three or
      more digits). Malformed IDs raise invalid-params (-32602). Unknown
      IDs come back as a ToolError (isError=true).

CLI smoke mode (non-MCP, for manual testing)::

    python3 scripts/persona_query_stdio.py --target ~/.ai-quickstart \
        --tool persona_get_current --args '{}'

The output is the tool's JSON result on stdout.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import sys
from pathlib import Path

SERVER_NAME = "ai-quickstart-persona"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC error codes (subset of the MCP / JSON-RPC 2.0 spec).
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

# Default target directory for the persona store. Mirrors the rest of
# ai-quickstart (paths.py uses the same default).
DEFAULT_TARGET = Path.home() / ".ai-quickstart"

PERSONA_SUBDIR = "persona"
PERSONA_JSON_FILE = "persona.json"
HEAL_LOCK_FILE = ".heal.lock"

# Paragraph IDs are "p:" followed by one or more digits, optionally with
# a disambiguator suffix (e.g. "p:001-a"). The persona_json module emits
# zero-padded three-digit IDs in steady state; we accept any all-digit
# body to stay forward-compatible.
PARAGRAPH_ID_RE = re.compile(r"^p:\d+(?:-[A-Za-z0-9]+)?$")


# ---------- helpers ----------

class ToolError(Exception):
    """Raised by tool implementations to signal a recoverable failure.

    The MCP dispatcher converts this into a tool-call error response (a
    normal JSON-RPC result with ``isError: true``), per the MCP tools
    spec, rather than a JSON-RPC protocol error.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _persona_dir(target: Path) -> Path:
    return Path(target) / PERSONA_SUBDIR


def _persona_json_path(target: Path) -> Path:
    return _persona_dir(target) / PERSONA_JSON_FILE


def _heal_lock_path(target: Path) -> Path:
    return _persona_dir(target) / HEAL_LOCK_FILE


def _heal_in_progress(target: Path) -> bool:
    """Return True if another process holds the heal lock right now.

    Probes with ``LOCK_SH | LOCK_NB`` so we never block; if the probe
    fails the persona is being regenerated and the on-disk payload is
    "stale" relative to the in-flight heal. If the lock file doesn't
    exist, no heal is in flight (heal.py creates the file on demand).
    Never raises; on any unexpected error we treat the persona as fresh.
    """
    lock_path = _heal_lock_path(target)
    if not lock_path.exists():
        return False
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as e:
            # EWOULDBLOCK on some platforms surfaces as OSError, not
            # BlockingIOError. Both mean the lock is contended.
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            return False
        else:
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


def _load_persona(target: Path) -> dict:
    """Load and parse persona.json, raising ToolError on user-facing errors."""
    json_path = _persona_json_path(target)
    if not json_path.exists():
        raise ToolError(
            "no persona; run /ai-quickstart:setup or "
            "`python3 scripts/init.py start`."
        )
    try:
        raw = json_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ToolError(f"persona.json unreadable: {e}") from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ToolError(f"persona.json parse error: {e}") from e
    if not isinstance(payload, dict):
        raise ToolError("persona.json: expected top-level object")
    return payload


# ---------- tool implementations ----------

def tool_persona_get_current(target: Path, _params: dict) -> dict:
    """Return the full persona.json payload, tagged ``stale: true`` if heal-in-flight."""
    payload = _load_persona(target)
    if _heal_in_progress(target):
        # Shallow copy so we never mutate cached state observed by another
        # caller (we don't cache today, but stay defensive).
        out = dict(payload)
        out["stale"] = True
        return out
    return payload


def tool_persona_get_paragraph(target: Path, params: dict) -> dict:
    """Return a single paragraph by id, or raise ToolError if unknown."""
    pid = params.get("paragraph_id")
    if not isinstance(pid, str) or not pid.strip():
        # Invalid params (raised through the dispatcher as -32602).
        raise _InvalidParams("missing or invalid 'paragraph_id' (string required)")
    pid = pid.strip()
    if not PARAGRAPH_ID_RE.match(pid):
        raise _InvalidParams(
            f"'paragraph_id' must match 'p:NNN' (got {pid!r})"
        )
    payload = _load_persona(target)
    paragraphs = payload.get("paragraphs")
    if not isinstance(paragraphs, list):
        raise ToolError("persona.json: paragraphs missing or malformed")
    for entry in paragraphs:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == pid:
            return {
                "id": entry.get("id"),
                "text": entry.get("text", ""),
                "provenance": entry.get("provenance"),
                "trust_score": entry.get("trust_score"),
                "locked": bool(entry.get("locked", False)),
                "anchored_to": entry.get("anchored_to"),
            }
    raise ToolError(f"no paragraph with id '{pid}'")


class _InvalidParams(Exception):
    """Internal signal: the caller passed structurally invalid params.

    The dispatcher converts this into a JSON-RPC -32602 error response,
    distinct from ToolError (which returns a successful result with
    isError=true). The CLI smoke mode treats it the same as ToolError.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---------- tool registry ----------

TOOLS = {
    "persona_get_current": {
        "fn": tool_persona_get_current,
        "description": (
            "Return the full persona.json payload (structured fields plus "
            "all addressable paragraphs). Adds 'stale: true' if a heal is "
            "in flight."
        ),
        "schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "persona_get_paragraph": {
        "fn": tool_persona_get_paragraph,
        "description": (
            "Return a single persona paragraph by id (id, text, "
            "provenance, trust_score, locked, anchored_to)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "paragraph_id": {
                    "type": "string",
                    "description": "Paragraph id in 'p:NNN' format.",
                    "pattern": r"^p:\d+(?:-[A-Za-z0-9]+)?$",
                },
            },
            "required": ["paragraph_id"],
            "additionalProperties": False,
        },
    },
}


# ---------- dispatch ----------

def call_tool(name: str, target: Path, params: dict) -> dict:
    """Invoke a registered tool by name.

    Raises ToolError on user-facing failures, _InvalidParams on structural
    parameter problems. Used by both the JSON-RPC dispatcher and the CLI
    smoke mode.
    """
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError(f"unknown tool '{name}'")
    if not isinstance(params, dict):
        raise _InvalidParams("tool arguments must be a JSON object")
    return spec["fn"](target, params)


# ---------- JSON-RPC plumbing ----------

def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _tool_descriptor(name: str, spec: dict) -> dict:
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": spec["schema"],
    }


def handle_request(req: dict, target: Path):
    """Handle a single JSON-RPC request. Returns a response dict or None.

    Returns None for notifications (requests without an ``id``), which per
    JSON-RPC 2.0 must not be answered.
    """
    if not isinstance(req, dict):
        return _err(None, ERR_INVALID_REQUEST, "request must be a JSON object")
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if not isinstance(method, str):
        return _err(rid, ERR_INVALID_REQUEST, "missing 'method'")

    is_notification = "id" not in req

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return None if is_notification else _ok(rid, result)

    if method == "notifications/initialized":
        # Client signalling readiness; no response per spec.
        return None

    if method == "tools/list":
        tools = [_tool_descriptor(n, s) for n, s in TOOLS.items()]
        return None if is_notification else _ok(rid, {"tools": tools})

    if method == "tools/call":
        if not isinstance(params, dict):
            return _err(rid, ERR_INVALID_PARAMS, "params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _err(rid, ERR_INVALID_PARAMS, "missing 'name'")
        if name not in TOOLS:
            return _err(rid, ERR_METHOD_NOT_FOUND, f"unknown tool '{name}'")
        if not isinstance(arguments, dict):
            return _err(rid, ERR_INVALID_PARAMS, "'arguments' must be an object")
        try:
            data = call_tool(name, target, arguments)
        except _InvalidParams as e:
            return _err(rid, ERR_INVALID_PARAMS, e.message)
        except ToolError as e:
            # Tool-level errors are wrapped as a successful tool/call
            # result with isError=true (MCP convention) so the model can
            # see and recover.
            return None if is_notification else _ok(
                rid,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": e.message}],
                },
            )
        except (OSError, ValueError) as e:
            return _err(rid, ERR_INTERNAL, f"internal error: {e}")
        text = json.dumps(data, ensure_ascii=False, indent=2)
        return None if is_notification else _ok(
            rid,
            {
                "isError": False,
                "content": [{"type": "text", "text": text}],
                "structuredContent": data,
            },
        )

    if is_notification:
        return None
    return _err(rid, ERR_METHOD_NOT_FOUND, f"method not found: {method}")


def serve_stdio(target: Path, stdin=None, stdout=None) -> int:
    """Run the JSON-RPC stdio loop until EOF.

    One JSON-RPC message per line ('LSP Content-Length' framing is NOT
    used; Claude Code's MCP stdio transport is line-delimited). Malformed
    JSON yields a parse-error response with id=None and the loop
    continues.
    """
    fin = stdin if stdin is not None else sys.stdin
    fout = stdout if stdout is not None else sys.stdout
    for raw in fin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = _err(None, ERR_PARSE, f"parse error: {e}")
            fout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            fout.flush()
            continue
        resp = handle_request(req, target)
        if resp is None:
            continue
        fout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        fout.flush()
    return 0


# ---------- CLI smoke mode ----------

def _resolve_target(arg_target):
    if arg_target:
        return Path(arg_target).expanduser().resolve()
    env = os.environ.get("AI_QUICKSTART_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_TARGET.expanduser().resolve()


def _cli_call(target: Path, tool: str, args_json: str) -> int:
    try:
        params = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        print(f"ERROR: --args is not valid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("ERROR: --args must be a JSON object", file=sys.stderr)
        return 2
    try:
        result = call_tool(tool, target, params)
    except _InvalidParams as e:
        print(json.dumps({"isError": True, "message": e.message}, indent=2))
        return 2
    except ToolError as e:
        print(json.dumps({"isError": True, "message": e.message}, indent=2))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    """CLI entry point. Default mode is MCP stdio; --tool runs once."""
    ap = argparse.ArgumentParser(
        description=(
            "ai-quickstart persona MCP server (stdio JSON-RPC). "
            "Pass --tool to run a single tool and exit."
        ),
    )
    ap.add_argument(
        "--target",
        default=None,
        help="persona home (default: $AI_QUICKSTART_HOME or ~/.ai-quickstart)",
    )
    ap.add_argument(
        "--tool",
        default=None,
        choices=list(TOOLS.keys()),
        help="run one tool and print its JSON output (CLI smoke mode)",
    )
    ap.add_argument(
        "--args",
        default="{}",
        help="JSON object of arguments for --tool (default: {})",
    )
    args = ap.parse_args(argv)
    target = _resolve_target(args.target)

    if args.tool:
        return _cli_call(target, args.tool, args.args)
    return serve_stdio(target)


if __name__ == "__main__":
    sys.exit(main())
