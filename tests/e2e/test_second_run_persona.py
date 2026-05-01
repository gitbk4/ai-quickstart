"""E2E: a second init run uses the existing persona and registers a 2nd project.

Scenario:
  * Run init flow once with archetype=job/industry-engineering. After heal,
    persona.md exists at ~/.ai-quickstart/persona/persona.md.
  * Run init flow again with archetype=personal. Verify the 2nd run produces
    different curated suggestions (the curated mapping returns different
    blocks per archetype, so this is mock-deterministic).
  * Verify managed-projects.json grew to 2 entries.

Heal in v1 is deferred to a follow-up invocation, so this test exercises the
heal pipeline directly via heal.cmd_prepare_context + heal.cmd_write to
simulate what happens when a real session runs the heal step.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


def _run_init_flow(cli_run, archetype: str, industry: str,
                   slug: str, project_dir: Path) -> str:
    """Drive a complete init -> accept flow. Returns the run_id."""
    rc, out, err = cli_run(["start", "--archetype", archetype])
    assert rc == 0, err
    run_id = json.loads(out)["run_id"]

    answers = {
        "archetype": archetype,
        "role": "researcher",
        "industry": industry,
        "top_problems": [f"problem-for-{archetype}"],
        "freeform_notes": f"notes-for-{archetype}",
    }
    rc, _, err = cli_run(
        ["record-answers", "--run-id", run_id],
        stdin_text=json.dumps(answers),
    )
    assert rc == 0, err

    rc, _, err = cli_run(["suggest", "--run-id", run_id])
    assert rc == 0, err

    rc, _, err = cli_run(
        ["accept", "--run-id", run_id],
        stdin_text=json.dumps({
            "project_specs": [{
                "slug": slug,
                "dir": str(project_dir),
                "anecdote_seed": f"seeded for {slug}",
                "skills": [],
            }]
        }),
    )
    assert rc == 0, err
    return run_id


def _heal_persona(e2e_home: Path, release_lock) -> dict:
    """Run heal.cmd_prepare_context + heal.cmd_write end-to-end.

    Releases the heal lock after the write so subsequent heals don't trip
    over it.
    """
    import heal  # type: ignore
    import persona  # type: ignore

    out_pc = io.StringIO()
    err_pc = io.StringIO()
    rc = heal.cmd_prepare_context(stdout=out_pc, stderr=err_pc)
    assert rc == 0, err_pc.getvalue()

    sin = io.StringIO("Synthesized persona prose summarizing recent activity.\n")
    out_w = io.StringIO()
    err_w = io.StringIO()
    rc = heal.cmd_write(stdin=sin, stdout=out_w, stderr=err_w)
    assert rc == 0, err_w.getvalue()

    release_lock()

    parsed = persona.parse_persona(e2e_home / "persona" / "persona.md")
    return parsed["frontmatter"]


def test_second_run_persona_present_and_two_projects(
    e2e_home: Path,
    mock_compathy: Path,
    mock_sources: dict,
    cli_run,
    heal_lock_release,
    tmp_path: Path,
):
    # ---- First run ----
    proj1 = tmp_path / "projects" / "alpha"
    run1 = _run_init_flow(
        cli_run,
        archetype="job",
        industry="engineering",
        slug="alpha",
        project_dir=proj1,
    )

    # First heal: persona.md must come into existence at the canonical path.
    fm1 = _heal_persona(e2e_home, heal_lock_release)
    persona_path = e2e_home / "persona" / "persona.md"
    assert persona_path.exists(), "persona.md should exist after first heal"
    version1 = fm1["generated"]["version"]
    assert isinstance(version1, int) and version1 >= 1

    # First-run suggestions snapshot for comparison.
    suggest1_payload = (e2e_home / "runs" / run1 / "step-2-prompt.md")
    assert suggest1_payload.exists()
    snap1 = suggest1_payload.read_text(encoding="utf-8")

    # ---- Second run, different archetype ----
    proj2 = tmp_path / "projects" / "beta"
    run2 = _run_init_flow(
        cli_run,
        archetype="personal",
        industry="default",  # personal mapping uses 'default' block
        slug="beta",
        project_dir=proj2,
    )
    assert run2 != run1

    # Second heal: version should bump, persona.md still there.
    fm2 = _heal_persona(e2e_home, heal_lock_release)
    version2 = fm2["generated"]["version"]
    assert version2 > version1, (
        f"persona version should bump on second heal (was {version1}, now {version2})"
    )

    # Two managed projects.
    registry = json.loads(
        (e2e_home / "managed-projects.json").read_text(encoding="utf-8")
    )
    assert len(registry) == 2
    assert any(str(proj1.resolve()) == p for p in registry)
    assert any(str(proj2.resolve()) == p for p in registry)

    # Suggestion content differs because the archetype changed.
    suggest2_payload = (e2e_home / "runs" / run2 / "step-2-prompt.md")
    assert suggest2_payload.exists()
    snap2 = suggest2_payload.read_text(encoding="utf-8")
    assert snap1 != snap2, (
        "step-2 prompt content should differ between job/engineering and "
        "personal/default — the curated mapping returns distinct blocks"
    )
