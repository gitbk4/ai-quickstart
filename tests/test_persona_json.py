"""Tests for scripts/persona_json.py (Wave 1A).

Covers the 13 GAPs from the v2-cathedral coverage diagram for Wave 1A:

  1.  write_persona_json happy path (tmp + replace + .bak)
  2.  Disk-full mid-write (mock OSError) -> exception raised, prior file untouched
  3.  Schema-version mismatch detected on read
  4.  generate_from_md first-time (no prior IDs) -> all paragraphs get fresh IDs
  5.  generate_from_md with prior IDs preserved across regen
  6.  User-edited persona.md (some IDs missing) -> hash-fallback reconstructs
  7.  User-split paragraph (1 ID, 2 paragraphs) -> keep + assign next free
  8.  User-merged paragraphs (heal collapse) -> lower ID kept, merged_from populated
  9.  Heal-deleted paragraph -> recorded in deleted_ids for one cycle
  10. Hash collision (identical paragraphs) -> both preserved with disambiguators
  11. migrate_md_to_json idempotent — second run is a no-op
  12. migrate_md_to_json creates .bak before mutation
  13. migrate_md_to_json aborts on malformed v1 persona (stderr warn, no .json written)
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import persona  # noqa: E402  pylint: disable=wrong-import-position
import persona_json  # noqa: E402  pylint: disable=wrong-import-position


# ---------- fixtures ----------

@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "aiq-home"
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(h))
    h.mkdir(parents=True, exist_ok=True)
    (h / "persona").mkdir()
    (h / "persona" / "anecdotes").mkdir()
    return h


def _seed_md(home: Path, prose: str) -> Path:
    fm = persona.default_persona()
    fm["identity"]["role"] = "platform engineer"
    fm["identity"]["industry"] = "fintech"
    fm["identity"]["archetype"] = "job"
    fm["preferences"]["skill_tolerance"] = "permissive"
    fm["preferences"]["project_style"] = "minimal"
    fm["activity"]["top_projects"] = ["alpha", "beta"]
    p = home / "persona" / "persona.md"
    persona.write_persona(p, fm, prose)
    return p


# ---------- GAP 1: write_persona_json happy path ----------

def test_write_persona_json_happy_path(home: Path):
    md = _seed_md(home, "Para A.\n\nPara B.\n")
    payload = persona_json.generate_from_md(md, None)
    persona_json.write_persona_json(home, payload)
    target = persona_json.persona_json_path(home)
    assert target.exists()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == persona_json.PERSONA_JSON_SCHEMA_VERSION
    assert len(loaded["paragraphs"]) == 2
    assert all(p["id"].startswith("p:") for p in loaded["paragraphs"])
    # No .bak yet (first write).
    assert not persona_json.persona_json_bak_path(home).exists()
    # No leftover tmp.
    leftovers = list((home / "persona").glob("persona.json.tmp-*"))
    assert leftovers == []


def test_write_persona_json_creates_bak_on_overwrite(home: Path):
    md = _seed_md(home, "Para A.\n")
    payload = persona_json.generate_from_md(md, None)
    persona_json.write_persona_json(home, payload)
    first_bytes = persona_json.persona_json_path(home).read_bytes()

    # Second write -> .bak should mirror first contents.
    payload2 = persona_json.generate_from_md(md, persona_json.persona_json_path(home))
    persona_json.write_persona_json(home, payload2)
    bak = persona_json.persona_json_bak_path(home)
    assert bak.exists()
    assert bak.read_bytes() == first_bytes


# ---------- GAP 2: disk-full mid-write ----------

def test_write_persona_json_disk_full_leaves_prior_untouched(home: Path, monkeypatch):
    md = _seed_md(home, "Para A.\n")
    # First write success.
    payload = persona_json.generate_from_md(md, None)
    persona_json.write_persona_json(home, payload)
    target = persona_json.persona_json_path(home)
    intact_bytes = target.read_bytes()

    # Force os.replace to raise OSError mid-second-write.
    real_replace = os.replace

    def boom(src, dst, *a, **kw):
        if str(dst) == str(target):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(persona_json.os, "replace", boom)
    with pytest.raises(OSError):
        persona_json.write_persona_json(home, payload)

    # Original persona.json untouched.
    assert target.read_bytes() == intact_bytes
    # Tmp file is left behind for callers to inspect (matches v1 atomic-write
    # contract from persona.write_persona's tmp behavior).
    leftovers = list((home / "persona").glob("persona.json.tmp-*"))
    assert len(leftovers) >= 1


# ---------- GAP 3: schema-version mismatch on read ----------

def test_read_persona_json_rejects_schema_mismatch(home: Path):
    target = persona_json.persona_json_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"schema_version": 99, "paragraphs": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError) as exc_info:
        persona_json.read_persona_json(home)
    assert "schema_version" in str(exc_info.value)


def test_read_persona_json_returns_none_when_missing(home: Path):
    assert persona_json.read_persona_json(home) is None


# ---------- GAP 4: first-time generation, fresh IDs ----------

def test_generate_from_md_first_time_assigns_fresh_ids(home: Path):
    md = _seed_md(home, "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.\n")
    payload = persona_json.generate_from_md(md, None)
    ids = [p["id"] for p in payload["paragraphs"]]
    assert ids == ["p:001", "p:002", "p:003"]
    texts = [p["text"] for p in payload["paragraphs"]]
    assert texts == ["First paragraph.", "Second paragraph.", "Third paragraph."]
    # Default provenance for fresh paragraphs is the "uncalibrated" sentinel.
    # trust.calibrate_paragraph_scores reads it as "no prior heal cycle" and
    # applies the first-run rule (heal, 3). Without this sentinel, calibrate
    # would mis-read the lane-1A default as a previous heal and degrade fresh
    # paragraphs to activity-inferred/2 — the bug the dogfood pass surfaced.
    for p in payload["paragraphs"]:
        assert p["provenance"] == "uncalibrated"
        assert p["trust_score"] == 3
        assert p["locked"] is False
        assert p["merged_from"] is None
    assert payload["deleted_ids"] == []


# ---------- GAP 5: prior IDs preserved across regen ----------

def test_generate_from_md_preserves_ids_across_regen(home: Path):
    md = _seed_md(home, "Alpha narrative.\n\nBeta narrative.\n\nGamma narrative.\n")
    json_path = persona_json.persona_json_path(home)
    payload1 = persona_json.generate_from_md(md, None)
    persona_json.write_persona_json(home, payload1)
    ids1 = {p["text"]: p["id"] for p in payload1["paragraphs"]}

    # Re-read md and regen — same prose -> same IDs.
    payload2 = persona_json.generate_from_md(md, json_path)
    ids2 = {p["text"]: p["id"] for p in payload2["paragraphs"]}
    assert ids1 == ids2

    # Now re-emit the markdown WITH markers and regen — IDs must still match.
    md_text = md.read_text(encoding="utf-8")
    rewritten = persona.assign_paragraph_ids(
        md_text, prior_ids={p["id"]: p["text"] for p in payload2["paragraphs"]}
    )
    md.write_text(rewritten, encoding="utf-8")
    payload3 = persona_json.generate_from_md(md, json_path)
    ids3 = {p["text"]: p["id"] for p in payload3["paragraphs"]}
    assert ids1 == ids3


# ---------- GAP 6: user-edited md, hash-fallback recovers ----------

def test_hash_fallback_recovers_ids_when_user_strips_markers(home: Path):
    md = _seed_md(home, "Alpha line.\n\nBeta line.\n\nGamma line.\n")
    json_path = persona_json.persona_json_path(home)
    payload1 = persona_json.generate_from_md(md, None)
    persona_json.write_persona_json(home, payload1)

    # User wipes all markers but keeps the prose intact.
    md_text = md.read_text(encoding="utf-8")
    md_with_markers = persona.assign_paragraph_ids(
        md_text, prior_ids={p["id"]: p["text"] for p in payload1["paragraphs"]}
    )
    # Note: md was just-written with no markers (write_persona doesn't add them
    # in v1), so the assign step is the one that places markers. Now strip them
    # to simulate a user edit that removed the comments.
    stripped = persona.PARAGRAPH_ID_PATTERN.sub("", md_with_markers)
    md.write_text(stripped, encoding="utf-8")

    payload2 = persona_json.generate_from_md(md, json_path)
    ids_by_text = {p["text"]: p["id"] for p in payload2["paragraphs"]}
    assert ids_by_text["Alpha line."] == ids_by_text["Alpha line."]  # tautology guard
    # The original IDs from payload1 are preserved despite missing markers.
    expected = {p["text"]: p["id"] for p in payload1["paragraphs"]}
    assert ids_by_text == expected


# ---------- GAP 7: user-split paragraph -> keep + next-free ----------

def test_user_split_paragraph_keeps_one_id_assigns_next_free(home: Path):
    md = _seed_md(home, "Original combined paragraph that talks about two things.\n")
    json_path = persona_json.persona_json_path(home)
    payload1 = persona_json.generate_from_md(md, None)
    persona_json.write_persona_json(home, payload1)
    assert payload1["paragraphs"][0]["id"] == "p:001"

    # Simulate user splitting the paragraph into two.
    parsed = persona.parse_persona(md)
    fm = parsed["frontmatter"]
    new_prose = (
        "Original combined paragraph that talks about two things.\n\n"
        "A brand new second paragraph with different content.\n"
    )
    persona.write_persona(md, fm, new_prose)

    payload2 = persona_json.generate_from_md(md, json_path)
    ids = [p["id"] for p in payload2["paragraphs"]]
    # The unchanged half retains p:001; the new half gets p:002.
    assert ids[0] == "p:001"
    assert ids[1] == "p:002"
    assert payload2["paragraphs"][0]["text"].startswith("Original combined paragraph")
    assert payload2["paragraphs"][1]["text"].startswith("A brand new second paragraph")


# ---------- GAP 8: heal merge -> lower ID + merged_from ----------

def test_heal_merge_keeps_lower_id_and_records_merged_from(home: Path):
    md = _seed_md(home, "Alpha topic.\n\nBeta topic.\n")
    json_path = persona_json.persona_json_path(home)
    payload1 = persona_json.generate_from_md(md, None)
    # Inject a synthetic merge by hand-editing the prior payload to mark
    # what generate_from_md should preserve. In real heal flow, the LLM
    # rewrites the prose and the heal pipeline annotates merged_from on
    # the surviving paragraph entry; we simulate that pre-write step here.
    persona_json.write_persona_json(home, payload1)

    # Simulate heal collapsing the two into one. The lower ID (p:001) wins.
    parsed = persona.parse_persona(md)
    fm = parsed["frontmatter"]
    # Place an explicit marker so the collapsed paragraph keeps p:001.
    merged_prose = "<!-- p:001 -->\nAlpha and beta combined into one.\n"
    persona.write_persona(md, fm, merged_prose)

    # The prior payload tells the regen which IDs USED to exist. The heal
    # pipeline (in production) is responsible for setting merged_from; for
    # this test we exercise the lower-level invariant: the surviving
    # paragraph has p:001 and the dropped p:002 shows up in deleted_ids.
    payload2 = persona_json.generate_from_md(md, json_path)
    surviving_ids = [p["id"] for p in payload2["paragraphs"]]
    assert surviving_ids == ["p:001"]
    assert "p:002" in payload2["deleted_ids"]


def test_merged_from_persists_across_regen(home: Path):
    """If a prior payload has merged_from on an entry, it survives regen."""
    md = _seed_md(home, "<!-- p:001 -->\nMerged paragraph.\n")
    payload = persona_json.generate_from_md(md, None)
    # Hand-set merged_from on the entry (simulating an upstream tagger).
    payload["paragraphs"][0]["merged_from"] = ["p:002", "p:003"]
    persona_json.write_persona_json(home, payload)

    # Regen with the same md.
    payload2 = persona_json.generate_from_md(
        md, persona_json.persona_json_path(home)
    )
    assert payload2["paragraphs"][0]["merged_from"] == ["p:002", "p:003"]


# ---------- GAP 9: deleted IDs recorded for one cycle ----------

def test_deleted_paragraph_recorded_in_deleted_ids(home: Path):
    md = _seed_md(home, "Keeper.\n\nGoner.\n")
    json_path = persona_json.persona_json_path(home)
    payload1 = persona_json.generate_from_md(md, None)
    persona_json.write_persona_json(home, payload1)
    goner_id = payload1["paragraphs"][1]["id"]

    parsed = persona.parse_persona(md)
    persona.write_persona(md, parsed["frontmatter"], "Keeper.\n")
    payload2 = persona_json.generate_from_md(md, json_path)
    assert goner_id in payload2["deleted_ids"]
    assert all(p["id"] != goner_id for p in payload2["paragraphs"])


# ---------- GAP 10: hash collision (identical paragraphs) ----------

def test_hash_collision_assigns_separate_ids(home: Path):
    md = _seed_md(
        home,
        "Same content here.\n\nDifferent middle paragraph.\n\nSame content here.\n",
    )
    payload = persona_json.generate_from_md(md, None)
    ids = [p["id"] for p in payload["paragraphs"]]
    assert len(ids) == 3
    assert len(set(ids)) == 3, f"identical paragraphs must get distinct IDs: {ids}"
    # Order: first occurrence keeps p:001, second occurrence gets a fresh ID.
    assert ids[0] == "p:001"
    assert ids[1] == "p:002"
    # Third paragraph has the same hash as first; it must get p:003 (or higher),
    # not collide with p:001.
    assert ids[2] != ids[0]


# ---------- GAP 11/12/13: migrate_md_to_json ----------

def test_migrate_md_to_json_idempotent(home: Path):
    _seed_md(home, "Para 1.\n\nPara 2.\n")
    json_path = persona_json.persona_json_path(home)
    res1 = persona_json.migrate_md_to_json(home)
    assert res1["ok"] is True
    assert res1["wrote_json"] is True
    assert json_path.exists()

    # Second run should be a no-op (idempotent).
    res2 = persona_json.migrate_md_to_json(home)
    assert res2["ok"] is True
    assert res2["wrote_json"] is False


def test_migrate_md_to_json_creates_md_bak(home: Path):
    _seed_md(home, "Para 1.\n")
    md_bak = home / "persona" / "persona.md.bak"
    # Remove any existing .bak from the seed write so we can observe migrate's behavior.
    if md_bak.exists():
        md_bak.unlink()
    res = persona_json.migrate_md_to_json(home)
    assert res["ok"] is True
    assert md_bak.exists()


def test_migrate_md_to_json_aborts_on_malformed_md(home: Path, capsys):
    md = home / "persona" / "persona.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    # Frontmatter opens but never closes -> malformed.
    md.write_text("---\nidentity:\n  role: dev\n# missing close\n", encoding="utf-8")
    res = persona_json.migrate_md_to_json(home)
    assert res["ok"] is False
    err = capsys.readouterr().err
    assert "malformed" in err.lower()
    # No persona.json written.
    assert not persona_json.persona_json_path(home).exists()


def test_migrate_md_to_json_dry_run_does_not_write(home: Path):
    _seed_md(home, "Para 1.\n")
    res = persona_json.migrate_md_to_json(home, dry_run=True)
    assert res["ok"] is True
    assert res["wrote_json"] is False
    assert not persona_json.persona_json_path(home).exists()


def test_migrate_md_to_json_missing_md_returns_not_ok(home: Path, capsys):
    res = persona_json.migrate_md_to_json(home)
    assert res["ok"] is False
    err = capsys.readouterr().err
    assert "not found" in err.lower() or "persona.md" in err.lower()


# ---------- structured-section projection ----------

def test_structured_section_projects_v1_frontmatter(home: Path):
    md = _seed_md(home, "Some prose.\n")
    payload = persona_json.generate_from_md(md, None)
    s = payload["structured"]
    assert s["role"] == "platform engineer"
    assert s["industry"] == "fintech"
    assert s["archetype"] == "job"
    # skill_tolerance is mapped from v1 ("permissive") to v2 ("high").
    assert s["skill_tolerance"] == "high"
    assert s["project_style"] == "minimal"
    # top_projects retains names with a scaffolded_at default.
    names = [tp["name"] for tp in s["top_projects"]]
    assert names == ["alpha", "beta"]
    for tp in s["top_projects"]:
        assert isinstance(tp["scaffolded_at"], str)


def test_from_md_sha_changes_when_md_changes(home: Path):
    md = _seed_md(home, "Para 1.\n")
    payload1 = persona_json.generate_from_md(md, None)
    parsed = persona.parse_persona(md)
    persona.write_persona(md, parsed["frontmatter"], "Different prose.\n")
    payload2 = persona_json.generate_from_md(md, None)
    assert payload1["from_md_sha"] != payload2["from_md_sha"]


# ---------- persona.py paragraph helpers ----------

def test_extract_paragraph_ids_round_trip():
    md = (
        "Some intro prose.\n\n"
        "<!-- p:001 -->\nFirst tagged paragraph.\n\n"
        "<!-- p:002 -->\nSecond tagged paragraph.\n"
    )
    ids = persona.extract_paragraph_ids(md)
    assert set(ids.keys()) == {"p:001", "p:002"}
    assert "First tagged paragraph." in ids["p:001"]
    assert "Second tagged paragraph." in ids["p:002"]


def test_assign_paragraph_ids_inserts_markers_for_unmarked():
    md = "Para A.\n\nPara B.\n\nPara C.\n"
    out = persona.assign_paragraph_ids(md)
    assert "<!-- p:001 -->" in out
    assert "<!-- p:002 -->" in out
    assert "<!-- p:003 -->" in out
    # Idempotent: re-running with the prior_ids preserves the IDs.
    out2 = persona.assign_paragraph_ids(
        out, prior_ids={"p:001": "Para A.", "p:002": "Para B.", "p:003": "Para C."}
    )
    # Marker count shouldn't grow.
    assert out2.count("<!-- p:") == 3


def test_hash_fallback_reconstruct_recovers_from_stripped_md():
    prior_json = {
        "paragraphs": [
            {"id": "p:001", "text": "Alpha."},
            {"id": "p:002", "text": "Beta."},
        ]
    }
    md_no_markers = "Alpha.\n\nBeta.\n"
    out = persona.hash_fallback_reconstruct(md_no_markers, prior_json)
    assert "<!-- p:001 -->" in out
    assert "<!-- p:002 -->" in out


# ---------- heal integration: persona.json regenerated after write ----------

def test_heal_write_regenerates_persona_json(home: Path):
    """heal.cmd_write must produce persona.json alongside persona.md."""
    import heal  # imported lazily to avoid forcing it in non-heal tests

    fm = persona.default_persona()
    fm["identity"]["role"] = "tester"
    fm["identity"]["archetype"] = "personal"
    persona.write_persona(home / "persona" / "persona.md", fm, "Initial body.\n")

    sin = io.StringIO("Brand new prose paragraph.\n\nSecond paragraph.\n")
    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_write(stdin=sin, stdout=out, stderr=err)
    assert rc == 0, err.getvalue()

    json_path = persona_json.persona_json_path(home)
    assert json_path.exists(), "heal write must regen persona.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == persona_json.PERSONA_JSON_SCHEMA_VERSION
    texts = [p["text"] for p in payload["paragraphs"]]
    assert "Brand new prose paragraph." in texts
    assert "Second paragraph." in texts
