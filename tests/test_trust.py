"""Tests for scripts/trust.py (Wave 2B).

Coverage map (1:1 with the Wave-2B tests-required list):

  1.  score_suggestion 5/4/3/2/1 -- one test per level.
  2.  score_suggestion missing fields -> conservative floor.
  3.  tag_persona_paragraph locked=True -> ("pinned", 5).
  4.  tag_persona_paragraph anecdote >= 80% overlap -> ("anecdote", 4).
  5.  tag_persona_paragraph prior_provenance=anecdote -> ("heal", 3).
  6.  tag_persona_paragraph prior_provenance=heal -> ("activity-inferred", 2).
  7.  tag_persona_paragraph prior_provenance=multi-hop -> ("multi-hop", 1).
  8.  calibrate_paragraph_scores updates payload.paragraphs[] in-place.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import trust  # noqa: E402  pylint: disable=wrong-import-position


# ---------- score_suggestion (1) ----------

def test_score_suggestion_curated_strong_freshness_returns_5():
    s = {
        "provenance": "curated",
        "github_stars": 250,
        "last_commit_days_ago": 14,
    }
    assert trust.score_suggestion(s) == 5


def test_score_suggestion_curated_weak_stars_returns_4():
    s = {
        "provenance": "curated",
        "github_stars": 50,  # below 100 floor
        "last_commit_days_ago": 30,
    }
    assert trust.score_suggestion(s) == 4


def test_score_suggestion_curated_weak_commit_returns_4():
    s = {
        "provenance": "curated",
        "github_stars": 500,
        "last_commit_days_ago": 200,  # 90-365 day band
    }
    assert trust.score_suggestion(s) == 4


def test_score_suggestion_live_registry_returns_3():
    assert trust.score_suggestion({"provenance": "live-registry"}) == 3
    assert trust.score_suggestion({"provenance": "scraped"}) == 3


def test_score_suggestion_inferred_returns_2():
    assert trust.score_suggestion({"provenance": "inferred"}) == 2


def test_score_suggestion_community_low_forks_returns_1():
    s = {"provenance": "community", "community_forks": 4}
    assert trust.score_suggestion(s) == 1


# ---------- score_suggestion (2): missing fields -> floor ----------

def test_score_suggestion_curated_missing_freshness_drops_to_4():
    """No stars + no commit info -> we can't verify the strong-freshness
    path, so the conservative score is 4 (curated but unverified)."""
    s = {"provenance": "curated"}
    assert trust.score_suggestion(s) == 4


def test_score_suggestion_community_missing_forks_returns_floor_1():
    s = {"provenance": "community"}  # no community_forks
    assert trust.score_suggestion(s) == 1


def test_score_suggestion_unknown_provenance_returns_floor_1():
    assert trust.score_suggestion({"provenance": "made-up"}) == 1
    assert trust.score_suggestion({}) == 1
    assert trust.score_suggestion(None) == 1  # type: ignore[arg-type]


def test_score_suggestion_curated_with_stale_commit_drops_to_3():
    """Curated entry whose project hasn't been touched in >365 days
    shouldn't ride the curated badge; we treat it like a live hit."""
    s = {
        "provenance": "curated",
        "github_stars": 500,
        "last_commit_days_ago": 700,
    }
    assert trust.score_suggestion(s) == 3


def test_score_suggestion_ignores_bool_for_int_fields():
    """Python bool is-a-int. We should NOT treat True/False as a star count."""
    s = {
        "provenance": "curated",
        "github_stars": True,  # would be 1 if treated as int
        "last_commit_days_ago": False,
    }
    assert trust.score_suggestion(s) == 4  # falls through to "curated weak"


# ---------- tag_persona_paragraph (3): locked ----------

def test_tag_persona_paragraph_locked_returns_pinned_5():
    tag, score = trust.tag_persona_paragraph(
        paragraph_text="anything",
        anecdotes=[],
        activity_log_lines=[],
        locked=True,
        prior_provenance="heal",
    )
    assert (tag, score) == ("pinned", 5)


def test_tag_persona_paragraph_locked_beats_anecdote_match():
    """locked=True must trump even a perfect anecdote token match."""
    text = "alpha beta gamma"
    anecdotes = [{"slug": "a", "content": "alpha beta gamma"}]
    tag, score = trust.tag_persona_paragraph(
        paragraph_text=text,
        anecdotes=anecdotes,
        activity_log_lines=[],
        locked=True,
        prior_provenance=None,
    )
    assert tag == "pinned"
    assert score == 5


# ---------- tag_persona_paragraph (4): anecdote near-verbatim ----------

def test_tag_persona_paragraph_anecdote_match_returns_anecdote_4():
    """Paragraph that quotes the anecdote at >=80% token overlap."""
    paragraph = "I scaffolded compathy on the alpha project last Tuesday."
    anecdotes = [
        {
            "slug": "alpha",
            "content": "I scaffolded compathy on the alpha project last Tuesday.",
        }
    ]
    tag, score = trust.tag_persona_paragraph(
        paragraph_text=paragraph,
        anecdotes=anecdotes,
        activity_log_lines=[],
        locked=False,
        prior_provenance=None,
    )
    assert tag == "anecdote"
    assert score == 4


def test_tag_persona_paragraph_anecdote_below_threshold_falls_through():
    """A paragraph that shares only a few tokens with anecdotes
    should NOT get the anecdote tag."""
    paragraph = "Something completely different about quantum mechanics."
    anecdotes = [{"slug": "a", "content": "alpha beta gamma delta epsilon"}]
    tag, score = trust.tag_persona_paragraph(
        paragraph_text=paragraph,
        anecdotes=anecdotes,
        activity_log_lines=[],
        locked=False,
        prior_provenance=None,
    )
    assert tag != "anecdote"


# ---------- tag_persona_paragraph (5): heal-of-anecdote ----------

def test_tag_persona_paragraph_prior_anecdote_returns_heal_3():
    tag, score = trust.tag_persona_paragraph(
        paragraph_text="totally rewritten paragraph",
        anecdotes=[],
        activity_log_lines=[],
        locked=False,
        prior_provenance="anecdote",
    )
    assert (tag, score) == ("heal", 3)


# ---------- tag_persona_paragraph (6): heal-of-heal ----------

def test_tag_persona_paragraph_prior_heal_returns_activity_inferred_2():
    tag, score = trust.tag_persona_paragraph(
        paragraph_text="another rewrite",
        anecdotes=[],
        activity_log_lines=[],
        locked=False,
        prior_provenance="heal",
    )
    assert (tag, score) == ("activity-inferred", 2)


def test_tag_persona_paragraph_prior_activity_inferred_returns_activity_inferred_2():
    """Two activity-inferred-rewrites in a row stay at that level until
    they cross into multi-hop on the next chain step."""
    tag, score = trust.tag_persona_paragraph(
        paragraph_text="more drift",
        anecdotes=[],
        activity_log_lines=[],
        locked=False,
        prior_provenance="activity-inferred",
    )
    assert (tag, score) == ("activity-inferred", 2)


# ---------- tag_persona_paragraph (7): multi-hop ----------

def test_tag_persona_paragraph_prior_multi_hop_saturates_at_1():
    tag, score = trust.tag_persona_paragraph(
        paragraph_text="even more drift",
        anecdotes=[],
        activity_log_lines=[],
        locked=False,
        prior_provenance="multi-hop",
    )
    assert (tag, score) == ("multi-hop", 1)


def test_tag_persona_paragraph_chain_simulation_4_hops():
    """Walk a 4-hop heal-rewrite chain and confirm the score steadily
    decays from anecdote to multi-hop. This simulates the realistic
    case of repeated re-heals across many sessions.

    NOTE: the spec describes multi-hop as "heal-of-heal-of-heal-...",
    which we model as (anecdote -> heal -> activity-inferred ->
    multi-hop saturation). Once at multi-hop, further hops stay at 1.
    """
    chain = [None]
    chain.append(trust.tag_persona_paragraph("p1", [], [], False, "anecdote")[0])
    chain.append(trust.tag_persona_paragraph("p2", [], [], False, chain[-1])[0])
    # After 3 hops we should be at activity-inferred or multi-hop.
    last = trust.tag_persona_paragraph("p3", [], [], False, chain[-1])
    assert last[0] in {"activity-inferred", "multi-hop"}
    # Saturating: feed multi-hop back in and ensure it stays put.
    saturated = trust.tag_persona_paragraph("p4", [], [], False, "multi-hop")
    assert saturated == ("multi-hop", 1)


# ---------- tag_persona_paragraph: activity-inferred fallback path ----------

def test_tag_persona_paragraph_activity_project_mention_inferred():
    """Paragraph mentions a project from activity entries with no prior
    provenance -> activity-inferred (2). This is the "we know which
    project this came from but not how it got into the persona" case."""
    activity = [
        {"event": "skill", "skill": "compathy", "cwd": "/Users/x/projects/risk-models"},
    ]
    tag, score = trust.tag_persona_paragraph(
        paragraph_text="risk-models is the canonical example here",
        anecdotes=[],
        activity_log_lines=activity,
        locked=False,
        prior_provenance=None,
    )
    assert (tag, score) == ("activity-inferred", 2)


# ---------- calibrate_paragraph_scores (8) ----------

def test_calibrate_paragraph_scores_updates_in_place(tmp_path: Path):
    anecdotes_dir = tmp_path / "anecdotes"
    anecdotes_dir.mkdir()
    (anecdotes_dir / "alpha.md").write_text(
        "I scaffolded compathy on the alpha project last Tuesday.",
        encoding="utf-8",
    )
    activity_path = tmp_path / "activity.jsonl"
    activity_path.write_text(
        json.dumps({"event": "skill", "skill": "compathy", "cwd": "/x/risk-models"}) + "\n",
        encoding="utf-8",
    )

    payload = {
        "schema_version": 1,
        "paragraphs": [
            {
                "id": "p:001",
                "text": "I scaffolded compathy on the alpha project last Tuesday.",
                "provenance": "heal",
                "trust_score": 3,
                "locked": False,
            },
            {
                "id": "p:002",
                "text": "Some unrelated paragraph",
                "provenance": "heal",
                "trust_score": 3,
                "locked": True,
            },
            {
                "id": "p:003",
                "text": "another rewrite cycle",
                "provenance": "heal",
                "trust_score": 3,
                "locked": False,
            },
        ],
    }

    out = trust.calibrate_paragraph_scores(payload, anecdotes_dir, activity_path)
    # Same object returned (mutated).
    assert out is payload
    paragraphs = out["paragraphs"]

    # p:001 quotes the anecdote -> anecdote/4.
    assert paragraphs[0]["provenance"] == "anecdote"
    assert paragraphs[0]["trust_score"] == 4

    # p:002 is locked -> pinned/5 regardless of text.
    assert paragraphs[1]["provenance"] == "pinned"
    assert paragraphs[1]["trust_score"] == 5

    # p:003 prior_provenance=heal, no anecdote match -> activity-inferred/2.
    assert paragraphs[2]["provenance"] == "activity-inferred"
    assert paragraphs[2]["trust_score"] == 2


def test_calibrate_paragraph_scores_handles_missing_dirs(tmp_path: Path):
    """Calibration must not raise when anecdotes_dir / activity_path are absent."""
    payload = {
        "paragraphs": [
            {"id": "p:001", "text": "hi", "provenance": "heal", "locked": False},
        ]
    }
    missing_anec = tmp_path / "nope-anecdotes"
    missing_act = tmp_path / "nope.jsonl"
    out = trust.calibrate_paragraph_scores(payload, missing_anec, missing_act)
    # Defaults to heal/3 (no anecdote, no activity, prior=heal -> activity-inferred/2).
    p = out["paragraphs"][0]
    assert p["provenance"] in {"heal", "activity-inferred"}
    assert p["trust_score"] in {2, 3}


def test_calibrate_paragraph_scores_tolerates_malformed_payload():
    """Payload with no paragraphs list / not-a-dict shouldn't raise."""
    assert trust.calibrate_paragraph_scores({}, None, None) == {}
    # Not-a-dict pass-through.
    assert trust.calibrate_paragraph_scores([], None, None) == []  # type: ignore[arg-type]


def test_calibrate_paragraph_scores_skips_non_dict_paragraphs():
    payload = {
        "paragraphs": [
            "not-a-dict",
            {"id": "p:001", "text": "hi", "provenance": "anecdote", "locked": False},
        ]
    }
    out = trust.calibrate_paragraph_scores(payload, None, None)
    # Only the dict entry was rewritten.
    assert out["paragraphs"][1]["provenance"] == "heal"
    assert out["paragraphs"][1]["trust_score"] == 3
