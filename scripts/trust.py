#!/usr/bin/env python3
"""Trust + provenance scoring (Wave 2B of v2).

This module provides deterministic 1-5 trust scoring for both
suggestions (curated/live/inferred/community sources) and persona
paragraphs (pinned/anecdote/heal/activity-inferred/multi-hop). Per
v2-cathedral.md "Defined Terms" -> "Trust score (1-5)" the score is
deterministic at *render* time -- no LLM call inside this module.

The provenance *tag* for a persona paragraph is partly LLM-driven (the
heal pipeline labels its rewritten paragraphs ``"heal"``); the 1-5
scoring of an existing tag and the calibration logic that walks the
heal-rewrite chain to detect ``multi-hop`` are pure functions of the
inputs we already have on disk.

Public API:

    score_suggestion(suggestion: dict) -> int
    tag_persona_paragraph(paragraph_text, anecdotes, activity_log_lines,
                          locked, prior_provenance) -> Tuple[str, int]
    calibrate_paragraph_scores(persona_json, anecdotes_dir, activity_path) -> dict

Stdlib only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Suggestion provenance / freshness rules.
# ---------------------------------------------------------------------------

# Curated thresholds (from v2-cathedral.md "Defined Terms" -> Trust score):
#   5: curated + GitHub stars > 100 + last commit < 90 days
#   4: curated but freshness weaker (<= 100 stars OR last commit 90-365 days)
#   3: live registry / scraped + persona-tag match
#   2: LLM-inferred from interview prose, no registry hit
#   1: community-shared template < 10 forks
_CURATED_PROVENANCES = {"curated"}
_LIVE_PROVENANCES = {"live-registry", "scraped"}
_INFERRED_PROVENANCES = {"inferred"}
_COMMUNITY_PROVENANCES = {"community"}

_CURATED_STARS_FLOOR = 100
_CURATED_FRESH_DAYS = 90
_CURATED_STALE_DAYS = 365
_COMMUNITY_FORKS_FLOOR = 10


def score_suggestion(suggestion: Dict[str, Any]) -> int:
    """Return a 1-5 trust score for a single suggestion.

    Suggestion shape (only the fields below are read; extras are ignored)::

        {
          "provenance": "curated"|"live-registry"|"scraped"|"inferred"|"community",
          "github_stars": int|None,
          "last_commit_days_ago": int|None,
          "community_forks": int|None,
        }

    Missing fields produce a CONSERVATIVE score: a curated entry with no
    freshness signal at all drops to 4 (we can't verify the fresh path),
    and a community entry with no fork count drops to 1 (the floor).
    Anything we can't classify at all returns the floor score 1.
    """
    if not isinstance(suggestion, dict):
        return 1
    raw_prov = suggestion.get("provenance")
    provenance = raw_prov.strip().lower() if isinstance(raw_prov, str) else ""

    if provenance in _CURATED_PROVENANCES:
        stars = suggestion.get("github_stars")
        days = suggestion.get("last_commit_days_ago")
        stars_ok = isinstance(stars, int) and not isinstance(stars, bool) and stars > _CURATED_STARS_FLOOR
        days_fresh = (
            isinstance(days, int)
            and not isinstance(days, bool)
            and 0 <= days < _CURATED_FRESH_DAYS
        )
        days_known_stale = (
            isinstance(days, int)
            and not isinstance(days, bool)
            and days >= _CURATED_STALE_DAYS
        )
        # Score 5 needs BOTH freshness signals strong (stars > 100 AND commit < 90d).
        if stars_ok and days_fresh:
            return 5
        # Drop hard if the entry is plainly stale (commit > 365d). Curated +
        # ancient = treat like a live-registry hit (3) -- don't reward
        # curation alone for an obviously dead project.
        if days_known_stale:
            return 3
        # Otherwise we're at "curated but weaker freshness" = 4.
        return 4

    if provenance in _LIVE_PROVENANCES:
        return 3

    if provenance in _INFERRED_PROVENANCES:
        return 2

    if provenance in _COMMUNITY_PROVENANCES:
        forks = suggestion.get("community_forks")
        if (
            isinstance(forks, int)
            and not isinstance(forks, bool)
            and forks >= _COMMUNITY_FORKS_FLOOR
        ):
            # Community template with healthy forks = treat like live-registry (3).
            return 3
        return 1

    # Unrecognized / missing provenance: conservative floor.
    return 1


# ---------------------------------------------------------------------------
# Persona-paragraph provenance tagging.
# ---------------------------------------------------------------------------

# Threshold for the "near-verbatim quote of an anecdote" rule: a paragraph
# whose tokens overlap an anecdote at >= 80% gets the (anecdote, 4) tag.
_ANECDOTE_OVERLAP_THRESHOLD = 0.80

# Token regex: keep only word-shaped runs. Lowercased before matching.
# Stripped of punctuation so paraphrases that change punctuation alone
# still match (e.g. anecdote ends with "." but paragraph rephrases as "!").
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Tiny stopword list: drop these so "the/a/of/and/..." filler doesn't
# inflate Jaccard overlap on dissimilar paragraphs. Intentionally
# narrow -- we want token overlap to mean "shared content," not "shared
# English."
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
        "you",
    ]
)


# Score table for persona-paragraph provenance tags. The TAG returned by
# tag_persona_paragraph drives the score; calibrate_paragraph_scores is
# responsible for walking heal-rewrite chains so the right tag lands here.
_PARAGRAPH_PROVENANCE_SCORE = {
    "pinned": 5,
    "anecdote": 4,
    "heal": 3,
    "activity-inferred": 2,
    "multi-hop": 1,
}


def _tokens(text: str) -> set:
    """Lowercased word-token set with stopwords removed."""
    if not text:
        return set()
    raw = _TOKEN_RE.findall(text.lower())
    return {t for t in raw if t and t not in _STOPWORDS}


def _max_anecdote_overlap(paragraph_tokens: set, anecdotes: List[Dict[str, Any]]) -> float:
    """Return the highest token-overlap ratio between paragraph and any anecdote.

    Overlap ratio for (paragraph, anecdote): ``|P ∩ A| / |P|``. We use
    the paragraph as the denominator (not Jaccard's union) because the
    spec is "paragraph contains a near-verbatim quote of an anecdote" --
    we want the paragraph to be MOSTLY anecdote tokens. Anecdote bodies
    are typically much longer than a single paragraph quote, so a Jaccard
    union washes out a real quote.
    """
    if not paragraph_tokens:
        return 0.0
    best = 0.0
    for entry in anecdotes:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content:
            continue
        anec_tokens = _tokens(content)
        if not anec_tokens:
            continue
        shared = paragraph_tokens & anec_tokens
        if not shared:
            continue
        ratio = len(shared) / len(paragraph_tokens)
        if ratio > best:
            best = ratio
    return best


def tag_persona_paragraph(
    paragraph_text: str,
    anecdotes: List[Dict[str, Any]],
    activity_log_lines: List[Dict[str, Any]],
    locked: bool,
    prior_provenance: Optional[str],
) -> Tuple[str, int]:
    """Compute (provenance_tag, trust_score) for a persona paragraph.

    Tag set: ``pinned`` | ``anecdote`` | ``heal`` | ``activity-inferred`` | ``multi-hop``.

    Rules (deterministic, no LLM):

      * ``locked=True`` -> (pinned, 5). Trumps everything else.
      * paragraph tokens overlap any anecdote at >= 80% -> (anecdote, 4).
      * prior_provenance == "anecdote" -> (heal, 3). One heal-rewrite away.
      * prior_provenance in {"heal", "activity-inferred"} -> (activity-inferred, 2).
        This is the "heal-rewrite of a heal-rewrite" branch -- two hops from
        any anchored anecdote.
      * prior_provenance == "multi-hop" -> (multi-hop, 1). Saturates: once
        a paragraph hits multi-hop, further rewrites stay at 1.
      * fall-through (no prior_provenance, no anecdote match): (heal, 3) is
        the conservative default, matching the Wave 1A persona_json default.

    ``activity_log_lines`` is currently consulted only as a fallback signal
    if no anecdote overlap fires AND no prior_provenance is meaningful --
    paragraphs that mention a project name from recent activity entries
    are nudged toward ``activity-inferred`` instead of ``heal``. This is
    a soft signal; the deterministic rules above always win.
    """
    if locked:
        return ("pinned", _PARAGRAPH_PROVENANCE_SCORE["pinned"])

    paragraph_tokens = _tokens(paragraph_text)

    # Anecdote near-verbatim match always wins over prior_provenance --
    # a heal that re-quotes an anecdote should get the higher score back.
    if anecdotes:
        ratio = _max_anecdote_overlap(paragraph_tokens, anecdotes)
        if ratio >= _ANECDOTE_OVERLAP_THRESHOLD:
            return ("anecdote", _PARAGRAPH_PROVENANCE_SCORE["anecdote"])

    # Walk the heal-rewrite chain.
    if isinstance(prior_provenance, str):
        prov = prior_provenance.strip().lower()
        if prov == "pinned":
            # A previously-pinned paragraph that's no longer locked drops to
            # the heal-of-pinned position (3): it had high trust, the user
            # unlocked it, and it survived a rewrite cycle.
            return ("heal", _PARAGRAPH_PROVENANCE_SCORE["heal"])
        if prov == "anecdote":
            return ("heal", _PARAGRAPH_PROVENANCE_SCORE["heal"])
        if prov in ("heal", "activity-inferred"):
            return ("activity-inferred", _PARAGRAPH_PROVENANCE_SCORE["activity-inferred"])
        if prov == "multi-hop":
            return ("multi-hop", _PARAGRAPH_PROVENANCE_SCORE["multi-hop"])

    # No prior provenance / no anecdote overlap. If the paragraph mentions
    # a project name from the activity log, score it as activity-inferred;
    # otherwise default to heal (matches Wave 1A's conservative default).
    if _matches_activity_project(paragraph_tokens, activity_log_lines):
        return ("activity-inferred", _PARAGRAPH_PROVENANCE_SCORE["activity-inferred"])
    return ("heal", _PARAGRAPH_PROVENANCE_SCORE["heal"])


def _matches_activity_project(
    paragraph_tokens: set, activity_log_lines: List[Dict[str, Any]]
) -> bool:
    """Return True if the paragraph mentions a project from activity entries.

    Cheap heuristic: collect the basenames of any ``cwd`` field across
    activity entries, and check whether the paragraph tokens intersect.
    Non-string / malformed entries are skipped.
    """
    if not paragraph_tokens or not activity_log_lines:
        return False
    project_tokens: set = set()
    for entry in activity_log_lines:
        if not isinstance(entry, dict):
            continue
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            slug = cwd.rstrip("/").rsplit("/", 1)[-1]
            for tok in _TOKEN_RE.findall(slug.lower()):
                if tok and tok not in _STOPWORDS:
                    project_tokens.add(tok)
        skill = entry.get("skill")
        if isinstance(skill, str) and skill:
            for tok in _TOKEN_RE.findall(skill.lower()):
                if tok and tok not in _STOPWORDS:
                    project_tokens.add(tok)
    if not project_tokens:
        return False
    return bool(paragraph_tokens & project_tokens)


# ---------------------------------------------------------------------------
# calibrate_paragraph_scores: drives the persona_json payload in-place.
# ---------------------------------------------------------------------------


def _read_anecdotes(anecdotes_dir: Optional[Path]) -> List[Dict[str, Any]]:
    """Best-effort read of anecdote .md files under ``anecdotes_dir``.

    Each entry: ``{"slug": <stem>, "content": <file text>}``. Missing dir
    or unreadable files just yield an empty list -- calibration is
    best-effort and never aborts the heal.
    """
    if anecdotes_dir is None:
        return []
    p = Path(anecdotes_dir)
    if not p.exists() or not p.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    try:
        files = sorted(p.glob("*.md"))
    except OSError:
        return []
    for f in files:
        try:
            out.append({"slug": f.stem, "content": f.read_text(encoding="utf-8")})
        except OSError:
            continue
    return out


def _read_activity_lines(activity_path: Optional[Path]) -> List[Dict[str, Any]]:
    """Best-effort read of activity.jsonl. Malformed lines are skipped."""
    if activity_path is None:
        return []
    p = Path(activity_path)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def calibrate_paragraph_scores(
    persona_json_payload: Dict[str, Any],
    anecdotes_dir: Optional[Path],
    activity_path: Optional[Path],
) -> Dict[str, Any]:
    """Update each paragraph's ``provenance`` and ``trust_score`` in-place.

    For every paragraph in ``persona_json_payload["paragraphs"]`` we call
    :func:`tag_persona_paragraph` with:

      * the paragraph's text
      * the anecdotes loaded from ``anecdotes_dir`` (deterministic load order)
      * the activity log entries loaded from ``activity_path``
      * the paragraph's existing ``locked`` flag
      * the paragraph's CURRENT provenance as ``prior_provenance`` -- this
        is what walks the heal-rewrite chain across calls (heal -> activity-
        inferred -> multi-hop).

    Returns the same payload object (mutated) so the caller can chain.
    """
    if not isinstance(persona_json_payload, dict):
        return persona_json_payload

    paragraphs = persona_json_payload.get("paragraphs")
    if not isinstance(paragraphs, list):
        return persona_json_payload

    anecdotes = _read_anecdotes(anecdotes_dir)
    activity = _read_activity_lines(activity_path)

    for entry in paragraphs:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text") if isinstance(entry.get("text"), str) else ""
        locked = bool(entry.get("locked", False))
        prior = entry.get("provenance") if isinstance(entry.get("provenance"), str) else None
        tag, score = tag_persona_paragraph(
            paragraph_text=text,
            anecdotes=anecdotes,
            activity_log_lines=activity,
            locked=locked,
            prior_provenance=prior,
        )
        entry["provenance"] = tag
        entry["trust_score"] = score

    return persona_json_payload


__all__ = [
    "score_suggestion",
    "tag_persona_paragraph",
    "calibrate_paragraph_scores",
]
