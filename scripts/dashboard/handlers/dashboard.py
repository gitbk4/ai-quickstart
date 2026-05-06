"""Dashboard-side HTML handlers for the Wave 1B combined http.server.

Wave 1B ships only a skeleton: ``GET /dashboard/`` returns a static HTML
shell that announces the panes Wave 3 will fill in, plus a single
end-to-end fetch against ``/persona/current`` to prove the wiring works.

Stdlib only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple


# The HTML template lives next to the handler so the package can be
# installed unmodified. Keep it under 100 lines (Wave 1B constraint).
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_TEMPLATE_INDEX = _TEMPLATE_DIR / "index.html"

# Wave 3 will fill these in — Wave 1B just has to mention every one in the
# HTML so a downstream test can assert the skeleton lists them.
FUTURE_PANES = (
    "persona-prose",
    "structured-fields",
    "diff-review",
    "activity-timeline",
    "suggestions",
)


def index(home: Path) -> Tuple[int, str]:
    """Return ``(status, html_body)`` for ``GET /dashboard/``.

    ``home`` is unused by the skeleton but kept in the signature so Wave 3
    handlers (which DO need persona state) drop in without churn.
    """
    del home  # unused in skeleton
    try:
        body = _TEMPLATE_INDEX.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover - defensive
        # If the template is missing, fall back to a minimal inline page so
        # the server never 500s on a static-asset hiccup.
        body = (
            "<!doctype html><html><body>"
            "<h1>ai-quickstart dashboard</h1>"
            "<p>Skeleton scaffold — panes coming in Wave 3.</p>"
            "<p>(template missing: {err})</p>"
            "</body></html>"
        ).format(err=str(e))
    return 200, body


__all__ = [
    "index",
    "FUTURE_PANES",
]
