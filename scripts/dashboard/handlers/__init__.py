"""Per-route handlers for the Wave 1B combined http.server.

Each module under ``handlers`` exports plain functions returning
``(status_code, body)`` tuples. The server module owns the wire framing
(serialization, headers, telemetry) so handlers stay easy to unit-test.
"""
from __future__ import annotations

__all__ = ["persona", "dashboard"]
