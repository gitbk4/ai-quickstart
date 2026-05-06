"""ai-quickstart Wave 1B: combined http.server hosting persona + dashboard.

This package contains:
  * ``server`` — the stdlib ``ThreadingHTTPServer`` daemon. Two URL roots:
    ``/persona/*`` (MCP-consumable persona_query) and ``/dashboard/*``
    (Wave 3 dashboard skeleton).
  * ``handlers/persona`` — read-side implementations of the persona endpoints,
    consumed by the server's ``do_GET`` dispatch.
  * ``handlers/dashboard`` — placeholder index/pane handlers; Wave 3 fills
    in the rich panes.

Stdlib only. No Flask, no FastAPI, no npm.
"""
from __future__ import annotations

__all__ = [
    "server",
    "handlers",
]
