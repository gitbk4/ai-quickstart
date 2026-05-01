"""E2E: ``init.py status`` reflects an empty home and a populated home.

Two scenarios:
  * Empty home: managed_projects_count==0, latest_run_id is None,
    persona.exists is False.
  * After a full init flow + manual heal: managed_projects_count==1,
    latest_run_id is set, persona.exists is True with version >= 2.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


def test_status_empty_home(e2e_home: Path, cli_run):
    rc, out, err = cli_run(["status"])
    assert rc == 0, err
    s = json.loads(out)
    assert s["ai_quickstart_home"] == str(e2e_home)
    assert s["managed_projects_count"] == 0
    assert s["latest_run_id"] is None
    assert s["persona"]["exists"] is False


def test_status_after_full_flow(
    e2e_home: Path,
    mock_compathy: Path,
    mock_sources: dict,
    cli_run,
    heal_lock_release,
    tmp_path: Path,
):
    # ---- run a complete init flow ----
    rc, out, _ = cli_run(["start", "--archetype", "job"])
    run_id = json.loads(out)["run_id"]

    cli_run(
        ["record-answers", "--run-id", run_id],
        stdin_text=json.dumps({
            "archetype": "job",
            "role": "engineer",
            "industry": "engineering",
        }),
    )
    cli_run(["suggest", "--run-id", run_id])

    proj_dir = tmp_path / "projects" / "alpha"
    cli_run(
        ["accept", "--run-id", run_id],
        stdin_text=json.dumps({
            "project_specs": [{
                "slug": "alpha",
                "dir": str(proj_dir),
                "anecdote_seed": "seeded for status test",
                "skills": [],
            }]
        }),
    )

    # ---- run heal so persona.md exists with version >= 2 ----
    import heal  # type: ignore

    out_pc = io.StringIO()
    err_pc = io.StringIO()
    rc = heal.cmd_prepare_context(stdout=out_pc, stderr=err_pc)
    assert rc == 0, err_pc.getvalue()

    out_w = io.StringIO()
    err_w = io.StringIO()
    sin = io.StringIO("Synthesized prose summarizing recent activity.\n")
    rc = heal.cmd_write(stdin=sin, stdout=out_w, stderr=err_w)
    assert rc == 0, err_w.getvalue()
    heal_lock_release()

    # ---- status now reflects everything ----
    rc, out, err = cli_run(["status"])
    assert rc == 0, err
    s = json.loads(out)
    assert s["managed_projects_count"] == 1
    assert s["latest_run_id"] == run_id
    assert s["persona"]["exists"] is True
    # On first heal, version becomes 2 (default starts at 1, write_persona bumps).
    # On the second persona-touching write inside heal it can go higher; verify >=2.
    assert s["persona"]["version"] >= 2
