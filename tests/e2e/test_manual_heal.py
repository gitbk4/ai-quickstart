"""E2E: manual /ai-quickstart heal pipeline.

Scenario:
  * Set up a home with activity.jsonl + 2 anecdotes + an existing persona.
  * Invoke ``heal.cmd_prepare_context`` -> JSON with current state.
  * Pipe new prose into ``heal.cmd_write`` -> persona.md written, .bak created,
    diff to stderr.
  * Verify version bumped, prose changed, anecdotes preserved.

The pipeline shape mirrors the production one in SKILL.md::

    python3 scripts/heal.py prepare-context | <Claude> | python3 scripts/heal.py write
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import sys
from pathlib import Path

import pytest


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_manual_heal_full_pipeline(e2e_home: Path, heal_lock_release, monkeypatch):
    import heal  # type: ignore
    import persona  # type: ignore

    # ---- seed home ----
    persona_dir = e2e_home / "persona"
    anecdotes_dir = persona_dir / "anecdotes"
    anecdotes_dir.mkdir(parents=True)

    # Seed an existing persona with version=2 so we can prove version bumps.
    fm = persona.default_persona()
    fm["identity"]["role"] = "ML engineer"
    fm["identity"]["industry"] = "fintech"
    fm["identity"]["archetype"] = "job"
    fm["activity"]["project_count"] = 2
    persona_path = persona_dir / "persona.md"
    persona.write_persona(persona_path, fm, "old prose paragraph.\n")
    pre_version = persona.parse_persona(persona_path)["frontmatter"]["generated"]["version"]

    # Two anecdotes.
    persona.append_anecdote(anecdotes_dir, "alpha", "tried compathy scaffold.\n")
    persona.append_anecdote(anecdotes_dir, "beta", "wired mcpmarket lookup.\n")

    # An activity.jsonl with two events in the current week.
    activity = persona_dir / "activity.jsonl"
    lines = [
        json.dumps({"ts": _now_iso(), "event": "skill", "skill": "compathy",
                    "cwd": "/p/alpha"}),
        json.dumps({"ts": _now_iso(), "event": "edit", "file": "x.py",
                    "cwd": "/p/alpha"}),
    ]
    activity.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- prepare-context: emits JSON, holds the heal lock ----
    out_pc = io.StringIO()
    err_pc = io.StringIO()
    rc = heal.cmd_prepare_context(stdout=out_pc, stderr=err_pc)
    assert rc == 0, err_pc.getvalue()
    ctx = json.loads(out_pc.getvalue())
    assert ctx["lock_acquired"] is True
    # Both anecdotes round-tripped.
    slugs = sorted(a["slug"] for a in ctx["anecdotes"])
    assert slugs == ["alpha", "beta"]
    # Both activity events read.
    assert len(ctx["activity_recent"]) == 2
    # Frontmatter matches what we seeded.
    assert ctx["current_frontmatter"]["identity"]["role"] == "ML engineer"

    # ---- write: pipe new prose, expect diff to stderr ----
    new_prose = (
        "Updated prose: the user has scaffolded two projects (alpha, beta) "
        "and is iterating on ML platform tooling.\n"
    )
    sin = io.StringIO(new_prose)
    out_w = io.StringIO()
    err_w = io.StringIO()
    rc = heal.cmd_write(stdin=sin, stdout=out_w, stderr=err_w)
    assert rc == 0, err_w.getvalue()
    heal_lock_release()

    # The .bak was written before the new write.
    bak_path = persona_dir / "persona.md.bak"
    assert bak_path.is_file(), ".bak should be created before persona.md is overwritten"
    bak_content = bak_path.read_text(encoding="utf-8")
    assert "old prose paragraph" in bak_content

    # Persona prose now reflects the new text.
    after = persona.parse_persona(persona_path)
    assert "alpha, beta" in after["prose"]
    assert "old prose paragraph" not in after["prose"]

    # Version bumped.
    new_version = after["frontmatter"]["generated"]["version"]
    assert new_version > pre_version, (
        f"version should bump (was {pre_version}, now {new_version})"
    )

    # A unified diff was printed to stderr.
    err_text = err_w.getvalue()
    assert "persona.md (before)" in err_text or "persona.md (after)" in err_text, (
        f"expected unified diff markers in stderr, got: {err_text!r}"
    )

    # Anecdotes still on disk and untouched.
    assert (anecdotes_dir / "alpha.md").is_file()
    assert (anecdotes_dir / "beta.md").is_file()
    assert "tried compathy scaffold" in (anecdotes_dir / "alpha.md").read_text(encoding="utf-8")
