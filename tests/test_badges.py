"""Tests for scripts/badges.py (Wave 2B shared renderer).

Coverage map:

  9.  render_trust_badge_terminal produces ANSI sequences for color.
  10. render_trust_badge_terminal honors NO_COLOR env var.
  11. render_trust_badge_html produces well-formed HTML span.
  12. render_fit_score_stars_terminal 5 buckets:
        0.0 -> ★☆☆☆☆, 0.25 -> ★★☆☆☆, 0.5 -> ★★★☆☆,
        0.75 -> ★★★★☆, 1.0 -> ★★★★★.
  13. render_provenance_badge_terminal emits expected text per category.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import badges  # noqa: E402  pylint: disable=wrong-import-position


# ---------- (9) render_trust_badge_terminal ANSI ----------

def test_trust_badge_terminal_emits_ansi(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = badges.render_trust_badge_terminal(5)
    assert "\x1b[32m" in out  # ANSI start
    assert "\x1b[0m" in out  # ANSI reset
    assert "verified" in out


def test_trust_badge_terminal_each_level_has_distinct_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    seen = set()
    for level in (1, 2, 3, 4, 5):
        out = badges.render_trust_badge_terminal(level)
        # First ANSI escape in the output
        prefix = out.split("m", 1)[0] + "m"
        assert prefix.startswith("\x1b[")
        seen.add(prefix)
    assert len(seen) == 5  # 5 distinct ANSI prefixes


def test_trust_badge_terminal_clamps_out_of_range(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    # 99 -> clamp to 5; -1 -> clamp to 1.
    high = badges.render_trust_badge_terminal(99)
    low = badges.render_trust_badge_terminal(-1)
    assert "verified" in high
    assert "community" in low


def test_trust_badge_terminal_handles_garbage(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = badges.render_trust_badge_terminal("not-a-number")  # type: ignore[arg-type]
    # Garbage falls to the floor (1, community).
    assert "community" in out


# ---------- (10) NO_COLOR honored ----------

def test_trust_badge_terminal_honors_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = badges.render_trust_badge_terminal(5)
    assert "\x1b[" not in out
    assert "verified" in out


def test_trust_badge_terminal_honors_empty_no_color(monkeypatch):
    """Per https://no-color.org/, ANY value (including '') opts out."""
    monkeypatch.setenv("NO_COLOR", "")
    out = badges.render_trust_badge_terminal(3)
    assert "\x1b[" not in out


# ---------- (11) HTML span well-formed ----------

def test_trust_badge_html_well_formed():
    html = badges.render_trust_badge_html(5)
    assert html.startswith("<span")
    assert html.endswith("</span>")
    assert 'class="trust-5"' in html
    assert "color:#0a0" in html
    assert "verified" in html


def test_trust_badge_html_each_level():
    for level in (1, 2, 3, 4, 5):
        html = badges.render_trust_badge_html(level)
        assert f'class="trust-{level}"' in html
        assert "</span>" in html


# ---------- (12) Fit-score stars 5 buckets ----------

@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, "★☆☆☆☆"),
        (0.25, "★★☆☆☆"),
        (0.5, "★★★☆☆"),
        (0.75, "★★★★☆"),
        (1.0, "★★★★★"),
    ],
)
def test_fit_score_stars_terminal_buckets(score, expected):
    assert badges.render_fit_score_stars_terminal(score) == expected


def test_fit_score_stars_terminal_clamps_below_zero():
    assert badges.render_fit_score_stars_terminal(-0.5) == "★☆☆☆☆"


def test_fit_score_stars_terminal_clamps_above_one():
    assert badges.render_fit_score_stars_terminal(2.0) == "★★★★★"


def test_fit_score_stars_terminal_handles_garbage():
    assert badges.render_fit_score_stars_terminal("nope") == "★☆☆☆☆"  # type: ignore[arg-type]


def test_fit_score_stars_html_wraps_in_span():
    html = badges.render_fit_score_stars_html(0.5)
    assert html.startswith('<span class="fit-stars">')
    assert html.endswith("</span>")
    # 3 filled, 2 empty
    assert html.count("★") == 3
    assert html.count("☆") == 2


# ---------- (13) Provenance badge ----------

@pytest.mark.parametrize(
    "tag,expected_label",
    [
        ("pinned", "[pinned]"),
        ("anecdote", "[anecdote]"),
        ("heal", "[heal]"),
        ("activity-inferred", "[activity-inferred]"),
        ("multi-hop", "[multi-hop]"),
    ],
)
def test_provenance_badge_terminal_no_color(monkeypatch, tag, expected_label):
    monkeypatch.setenv("NO_COLOR", "1")
    assert badges.render_provenance_badge_terminal(tag) == expected_label


def test_provenance_badge_terminal_with_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = badges.render_provenance_badge_terminal("pinned")
    assert "\x1b[" in out
    assert "[pinned]" in out


def test_provenance_badge_terminal_unknown_tag(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = badges.render_provenance_badge_terminal("not-a-tag")
    # Unknown tags render plain (no ANSI) so they don't pretend to be valid.
    assert "\x1b[" not in out
    assert "[unknown]" in out


def test_provenance_badge_html_wraps_in_span():
    html = badges.render_provenance_badge_html("pinned")
    assert '<span class="prov-pinned"' in html
    assert "color:#0a0" in html
    assert "[pinned]" in html


def test_provenance_badge_html_escapes_unknown():
    """Unknown provenance falls back without leaking the input string raw."""
    html = badges.render_provenance_badge_html('"><script>')
    assert "<script>" not in html  # not directly injected
    # Unknown maps to "unknown"
    assert "[unknown]" in html
