"""E2E happy path: a complete init flow from a clean home through scaffolding.

The flow exercised here mirrors what ``/ai-quickstart`` does in production:

    1. Fresh home, no persona, no managed projects
    2. ``init.py start --archetype job`` -> run_id captured, step-1 prompt exists
    3. ``init.py record-answers --run-id X`` <<< {role, industry, ...}
       -> answers.json on disk
    4. ``init.py suggest --run-id X`` -> JSON ranked output, step-2 prompt written
    5. ``init.py accept --run-id X`` <<< {project_specs: [...]} -> fake compathy
       invoked, project dir created, marker registered, anecdote seeded
    6. ``init.py add-starting-files --project-dir X`` <<< [paths] -> files copied
    7. ``init.py status`` -> reflects 1 managed project + recent run-id

External dependencies (compathy, GitHub API, MCP registry, mcpmarket) are
mocked via the conftest fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_full_happy_path(
    e2e_home: Path,
    mock_compathy: Path,
    mock_sources: dict,
    cli_run,
    tmp_path: Path,
):
    # 1. Fresh home: no persona, no managed projects.
    assert not (e2e_home / "persona" / "persona.md").exists()
    assert not (e2e_home / "managed-projects.json").exists()

    # 2. start
    rc, out, err = cli_run(["start", "--archetype", "job"])
    assert rc == 0, err
    start_payload = json.loads(out)
    run_id = start_payload["run_id"]
    assert start_payload["archetype"] == "job"
    assert Path(start_payload["prompt_path"]).exists(), (
        "step-1 prompt file should be written by start"
    )

    # 3. record-answers
    answers = {
        "archetype": "job",
        "role": "data engineer",
        "industry": "engineering",
        "top_problems": ["pipeline reliability", "onboarding chaos"],
        "desired_outcomes": ["repeatable scaffolds"],
        "skill_tolerance": "permissive",
        "project_style": "minimal",
        "coding_languages": ["python"],
        "freeform_notes": "wants reproducible flows",
    }
    rc, out, err = cli_run(
        ["record-answers", "--run-id", run_id],
        stdin_text=json.dumps(answers),
    )
    assert rc == 0, err
    rec_payload = json.loads(out)
    assert rec_payload["ok"] is True
    answers_path = Path(rec_payload["answers_path"])
    assert answers_path.exists()
    on_disk = json.loads(answers_path.read_text(encoding="utf-8"))
    assert on_disk["role"] == "data engineer"
    assert on_disk["industry"] == "engineering"

    # 4. suggest — sources are mocked so the gather call returns canned data.
    rc, out, err = cli_run(["suggest", "--run-id", run_id])
    assert rc == 0, err
    sug = json.loads(out)
    assert "skills" in sug
    assert "project_templates" in sug
    assert "mcp_servers" in sug
    # The "engineering" industry block exists in mappings/personas.yaml so we
    # should get a non-empty curated list.
    assert isinstance(sug["skills"], list)
    # Step-2 prompt should be on disk.
    step2 = e2e_home / "runs" / run_id / "step-2-prompt.md"
    assert step2.exists(), "step-2 prompt must be written during suggest"

    # 5. accept
    project_dir = tmp_path / "projects" / "alpha-pipeline"
    accept_payload = {
        "project_specs": [
            {
                "slug": "alpha-pipeline",
                "dir": str(project_dir),
                "anecdote_seed": "Started for repeatable ETL scaffolding.",
                "skills": [
                    {
                        "name": "research-assistant",
                        "source": "github",
                        "url": "https://github.com/example/research",
                        "stars": 1234,
                    }
                ],
            }
        ]
    }
    rc, out, err = cli_run(
        ["accept", "--run-id", run_id],
        stdin_text=json.dumps(accept_payload),
    )
    assert rc == 0, err
    acc = json.loads(out)
    assert len(acc["projects"]) == 1
    proj = acc["projects"][0]
    assert proj["ok"] is True
    assert proj["compathy_initialized"] is True
    assert proj["registered"] is True

    # Project directory + scaffolded files exist.
    raw = project_dir / "context" / "raw"
    assert raw.is_dir()
    assert (raw / "anecdote.md").is_file()
    assert (raw / "skills.md").is_file()
    assert (raw / "starting-files-todo.md").is_file()

    # Anecdote contains the seed.
    anecdote = (raw / "anecdote.md").read_text(encoding="utf-8")
    assert "repeatable ETL scaffolding" in anecdote

    # Registry contains the absolute path.
    registry_path = e2e_home / "managed-projects.json"
    assert registry_path.is_file()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert str(project_dir.resolve()) in registry

    # 6. add-starting-files
    src_doc = tmp_path / "docs" / "starting-spec.md"
    src_doc.parent.mkdir(parents=True)
    src_doc.write_text("# Spec\nSeed material.\n", encoding="utf-8")
    rc, out, err = cli_run(
        ["add-starting-files", "--project-dir", str(project_dir)],
        stdin_text=json.dumps([str(src_doc)]),
    )
    assert rc == 0, err
    addf = json.loads(out)
    assert len(addf["copied"]) == 1
    copied = Path(addf["copied"][0])
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == "# Spec\nSeed material.\n"

    # 7. status reflects 1 managed project + a latest_run_id.
    rc, out, err = cli_run(["status"])
    assert rc == 0, err
    status = json.loads(out)
    assert status["managed_projects_count"] == 1
    assert status["latest_run_id"] == run_id


def test_full_flow_no_skills_still_succeeds(
    e2e_home: Path,
    mock_compathy: Path,
    mock_sources: dict,
    cli_run,
    tmp_path: Path,
):
    """Accept a project with an empty skills list — the flow must not crash."""
    rc, out, _ = cli_run(["start", "--archetype", "exploring"])
    run_id = json.loads(out)["run_id"]

    cli_run(
        ["record-answers", "--run-id", run_id],
        stdin_text=json.dumps({"archetype": "exploring", "freeform_notes": "trying things"}),
    )
    cli_run(["suggest", "--run-id", run_id])

    proj_dir = tmp_path / "p" / "tiny"
    rc, out, err = cli_run(
        ["accept", "--run-id", run_id],
        stdin_text=json.dumps({
            "project_specs": [{
                "slug": "tiny",
                "dir": str(proj_dir),
                "anecdote_seed": "tiny seed",
                "skills": [],
            }]
        }),
    )
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["projects"][0]["ok"] is True
    assert (proj_dir / "context" / "raw" / "anecdote.md").exists()
