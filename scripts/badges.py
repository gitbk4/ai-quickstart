#!/usr/bin/env python3
"""Shared trust-badge / fit-score / provenance-tag renderer (Wave 2B).

Per v2-cathedral.md "Eng Review Decisions" #9: this module is the single
source of truth for trust-badge ANSI/HTML output. Both the dashboard
suggestions pane (Proposal 3, Wave 3) and the existing Step 2 terminal
output (cascading-kill mitigation) read from here so we never drift two
parallel implementations.

Public renderers:

    render_trust_badge_terminal(score: int) -> str
    render_trust_badge_html(score: int) -> str
    render_fit_score_stars_terminal(score_0_to_1: float) -> str
    render_fit_score_stars_html(score_0_to_1: float) -> str
    render_provenance_badge_terminal(provenance: str) -> str
    render_provenance_badge_html(provenance: str) -> str

ANSI sequences honour ``NO_COLOR`` (https://no-color.org). When that
env var is set (to ANY non-empty value), the terminal renderers fall
back to plain text.

Stdlib only.
"""
from __future__ import annotations

import html
import os
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Public table -- name, terminal color, html color, glyph for each level.
# ---------------------------------------------------------------------------
#
# ANSI choices: 32 (green), 36 (cyan), 33 (yellow), 35 (magenta), 37 (white).
# We picked CYAN for level 4 instead of "teal" because there is no portable
# "teal" 8-color ANSI; cyan reads as teal-ish on every terminal we tested
# (iTerm2, Terminal.app, gnome-terminal, Windows Terminal).
#
# HTML choices favour AA contrast against a #fff background:
#   #0a0  (5, verified)  -- forest green
#   #080  (4, curated)   -- slightly darker green; hue distinct from 5
#   #a80  (3, live)      -- amber
#   #c40  (2, inferred)  -- burnt orange
#   #888  (1, community) -- neutral gray for "low-signal but present"
#
# Glyphs: filled circle for the strong levels (5/4/3 -- the ones we
# trust enough to surface confidently); hollow circle for the weak ones
# (2/1 -- the inferred / community floor) so a colorblind user can still
# tell strong from weak at a glance.
TRUST_LEVELS: Dict[int, Dict[str, str]] = {
    5: {"name": "verified", "color_terminal": "32", "color_html": "#0a0", "emoji": "●"},
    4: {"name": "curated", "color_terminal": "36", "color_html": "#080", "emoji": "●"},
    3: {"name": "live", "color_terminal": "33", "color_html": "#a80", "emoji": "●"},
    2: {"name": "inferred", "color_terminal": "35", "color_html": "#c40", "emoji": "○"},
    1: {"name": "community", "color_terminal": "37", "color_html": "#888", "emoji": "○"},
}


# Provenance tag -> short human label. Used by both terminal and HTML
# renderers. Persona paragraph provenance is a parallel taxonomy to the
# suggestion-side TRUST_LEVELS table; we render it as a small bracket-
# wrapped tag (``[pinned]``, ``[anecdote]``, ...).
PROVENANCE_LABELS: Dict[str, str] = {
    "pinned": "pinned",
    "anecdote": "anecdote",
    "heal": "heal",
    "activity-inferred": "activity-inferred",
    "multi-hop": "multi-hop",
}


# ANSI color per provenance tag (terminal). Maps onto the same palette
# as TRUST_LEVELS so the user perceives consistency between the two
# surfaces -- pinned is the "verified-green" of paragraphs, multi-hop is
# the "community-gray" of paragraphs.
_PROVENANCE_TERMINAL_COLOR: Dict[str, str] = {
    "pinned": "32",
    "anecdote": "36",
    "heal": "33",
    "activity-inferred": "35",
    "multi-hop": "37",
}


# HTML color per provenance tag (mirror of the terminal palette).
_PROVENANCE_HTML_COLOR: Dict[str, str] = {
    "pinned": "#0a0",
    "anecdote": "#080",
    "heal": "#a80",
    "activity-inferred": "#c40",
    "multi-hop": "#888",
}


def _no_color_active() -> bool:
    """Return True if NO_COLOR is set to any non-empty value.

    Per https://no-color.org/, the presence of the env var (even as
    empty string) should disable color. We follow the strict spec: any
    value (including ``""``) opts out.
    """
    return "NO_COLOR" in os.environ


def _coerce_score(score: Any) -> int:
    """Clamp ``score`` to the valid 1-5 range; default to 1 on garbage."""
    try:
        n = int(score)
    except (TypeError, ValueError):
        return 1
    if n < 1:
        return 1
    if n > 5:
        return 5
    return n


# ---------------------------------------------------------------------------
# Trust-badge renderers.
# ---------------------------------------------------------------------------


def render_trust_badge_terminal(score: int) -> str:
    """Return a 1-line ANSI-colored trust badge.

    Format: ``<glyph> <name>`` -- e.g. ``● verified``. NO_COLOR -> plain.
    """
    n = _coerce_score(score)
    spec = TRUST_LEVELS[n]
    glyph = spec["emoji"]
    name = spec["name"]
    if _no_color_active():
        return f"{glyph} {name}"
    color = spec["color_terminal"]
    return f"\x1b[{color}m{glyph} {name}\x1b[0m"


def render_trust_badge_html(score: int) -> str:
    """Return a ``<span class="trust-N" style="color:...">...`` element.

    Caller is responsible for the surrounding context; the span itself
    is self-contained and HTML-escape-safe (the name is from a fixed
    table, but we still escape defensively).
    """
    n = _coerce_score(score)
    spec = TRUST_LEVELS[n]
    name = html.escape(spec["name"])
    glyph = html.escape(spec["emoji"])
    color = html.escape(spec["color_html"])
    return (
        f'<span class="trust-{n}" style="color:{color}">'
        f'{glyph} {name}'
        f"</span>"
    )


# ---------------------------------------------------------------------------
# Fit-score stars -- 5 buckets from a 0.0-1.0 float.
# ---------------------------------------------------------------------------

# 5 buckets -- 0.0-0.2, 0.2-0.4, ..., 0.8-1.0. Inputs outside [0,1] are
# clamped. A score of 0 renders as 1 filled star (★☆☆☆☆) per the spec
# table: 0.0 -> ★☆☆☆☆.
_STAR_FILLED = "★"
_STAR_EMPTY = "☆"


def _fit_to_filled(score_0_to_1: Any) -> int:
    """Map a 0.0-1.0 float to the count of filled stars in a 5-star bar.

    Lower-inclusive bucket boundaries at multiples of 0.2:
      [0.00, 0.20) -> 1 filled (★☆☆☆☆)
      [0.20, 0.40) -> 2
      [0.40, 0.60) -> 3
      [0.60, 0.80) -> 4
      [0.80, 1.00] -> 5

    Implementation: ``filled = clamp(floor(score*5) + 1, 1, 5)``. The +1
    floor means a 0.0 input still renders one filled star (downstream UI
    relies on never seeing zero stars). This unifies the bucketing used
    by both the alternatives engine's fit score (lane-2A) and the trust
    badge renderer.
    """
    try:
        f = float(score_0_to_1)
    except (TypeError, ValueError):
        f = 0.0
    if f != f:  # NaN
        f = 0.0
    if f < 0.0:
        f = 0.0
    if f > 1.0:
        f = 1.0
    import math
    n = math.floor(f * 5) + 1
    if n < 1:
        n = 1
    if n > 5:
        n = 5
    return n


def render_fit_score_stars_terminal(score_0_to_1: float) -> str:
    """Render 5-star fit score as plain unicode (no color)."""
    filled = _fit_to_filled(score_0_to_1)
    return _STAR_FILLED * filled + _STAR_EMPTY * (5 - filled)


def render_fit_score_stars_html(score_0_to_1: float) -> str:
    """Render 5-star fit score as ``<span class="fit-stars">`` HTML."""
    filled = _fit_to_filled(score_0_to_1)
    stars = _STAR_FILLED * filled + _STAR_EMPTY * (5 - filled)
    return f'<span class="fit-stars">{html.escape(stars)}</span>'


# ---------------------------------------------------------------------------
# Provenance badge renderers (paragraph-level).
# ---------------------------------------------------------------------------


def _coerce_provenance(provenance: Any) -> str:
    """Return a known provenance tag or ``unknown``."""
    if isinstance(provenance, str):
        s = provenance.strip().lower()
        if s in PROVENANCE_LABELS:
            return s
    return "unknown"


def render_provenance_badge_terminal(provenance: str) -> str:
    """Render a paragraph provenance as a colored ``[tag]`` token.

    NO_COLOR -> plain ``[tag]``.
    """
    tag = _coerce_provenance(provenance)
    label = PROVENANCE_LABELS.get(tag, tag)
    text = f"[{label}]"
    if _no_color_active() or tag == "unknown":
        return text
    color = _PROVENANCE_TERMINAL_COLOR[tag]
    return f"\x1b[{color}m{text}\x1b[0m"


def render_provenance_badge_html(provenance: str) -> str:
    """Render a paragraph provenance as ``<span class="prov-X">``."""
    tag = _coerce_provenance(provenance)
    label = PROVENANCE_LABELS.get(tag, tag)
    color = _PROVENANCE_HTML_COLOR.get(tag, "#888")
    return (
        f'<span class="prov-{html.escape(tag)}" style="color:{html.escape(color)}">'
        f"[{html.escape(label)}]"
        f"</span>"
    )


__all__ = [
    "TRUST_LEVELS",
    "PROVENANCE_LABELS",
    "render_trust_badge_terminal",
    "render_trust_badge_html",
    "render_fit_score_stars_terminal",
    "render_fit_score_stars_html",
    "render_provenance_badge_terminal",
    "render_provenance_badge_html",
]
