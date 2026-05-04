"""Tests for scripts/eval_deeper_interview.py.

Covers schema validation, the 3 subcommands (run / validate / list), and
edge cases of the JSON loader. The actual semantic judgment of deeper-
interview output happens at runtime in Claude Code; these tests cover
the harness only.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import eval_deeper_interview as edi  # noqa: E402


@pytest.fixture
def sample_eval(tmp_path: Path) -> Path:
    data = {
        "version": 1,
        "description": "Test eval",
        "judge_model": "claude-haiku",
        "pass_threshold": 0.8,
        "cases": [
            {
                "name": "happy-path",
                "input": {
                    "archetype": "job",
                    "entry_answers": {
                        "archetype": "job",
                        "role": "engineer",
                        "industry": "fintech",
                        "top_problems": ["ship faster"],
                        "desired_outcomes": ["weekly release"],
                    },
                },
                "expectations": [
                    "interview probes the vague 'ship faster' framing",
                    "interview stays under 600 words",
                ],
            },
            {
                "name": "edge-case",
                "input": {"archetype": "exploring", "entry_answers": {}},
                "expectations": ["interview handles missing entry answers gracefully"],
            },
        ],
    }
    p = tmp_path / "eval.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _run(argv, eval_path=None):
    out = io.StringIO()
    err = io.StringIO()
    if eval_path is not None:
        argv = list(argv) + ["--eval", str(eval_path)]
    rc = edi.main(argv, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


# ---------- load_eval ----------

def test_load_eval_happy_path(sample_eval: Path):
    data = edi.load_eval(sample_eval)
    assert data["version"] == 1
    assert len(data["cases"]) == 2


def test_load_eval_missing_file(tmp_path: Path):
    with pytest.raises(edi.EvalSchemaError, match="not found"):
        edi.load_eval(tmp_path / "no-such.json")


def test_load_eval_malformed_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json{{", encoding="utf-8")
    with pytest.raises(edi.EvalSchemaError, match="parse"):
        edi.load_eval(p)


def test_load_eval_root_not_object(tmp_path: Path):
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["just", "a", "list"]), encoding="utf-8")
    with pytest.raises(edi.EvalSchemaError, match="must be an object"):
        edi.load_eval(p)


def test_load_eval_missing_required_field(tmp_path: Path):
    p = tmp_path / "missing.json"
    p.write_text(json.dumps({"version": 1}), encoding="utf-8")  # no 'cases'
    with pytest.raises(edi.EvalSchemaError, match="cases"):
        edi.load_eval(p)


def test_load_eval_empty_cases(tmp_path: Path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"version": 1, "cases": []}), encoding="utf-8")
    with pytest.raises(edi.EvalSchemaError, match="non-empty"):
        edi.load_eval(p)


def test_load_eval_case_missing_field(tmp_path: Path):
    p = tmp_path / "bad-case.json"
    p.write_text(json.dumps({
        "version": 1,
        "cases": [{"name": "x"}]  # missing input + expectations
    }), encoding="utf-8")
    with pytest.raises(edi.EvalSchemaError, match="missing required field"):
        edi.load_eval(p)


def test_load_eval_case_empty_expectations(tmp_path: Path):
    p = tmp_path / "bad-exp.json"
    p.write_text(json.dumps({
        "version": 1,
        "cases": [{"name": "x", "input": {}, "expectations": []}]
    }), encoding="utf-8")
    with pytest.raises(edi.EvalSchemaError, match="non-empty list"):
        edi.load_eval(p)


# ---------- validate subcommand ----------

def test_validate_happy(sample_eval: Path):
    rc, out, err = _run(["validate"], eval_path=sample_eval)
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["case_count"] == 2
    assert parsed["case_names"] == ["happy-path", "edge-case"]


def test_validate_failure_exits_one(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("invalid", encoding="utf-8")
    rc, _, err = _run(["validate"], eval_path=bad)
    assert rc == 1
    assert "validation failed" in err


# ---------- list subcommand ----------

def test_list_returns_case_summary(sample_eval: Path):
    rc, out, _ = _run(["list"], eval_path=sample_eval)
    assert rc == 0
    parsed = json.loads(out)
    assert len(parsed) == 2
    assert parsed[0]["name"] == "happy-path"
    assert parsed[0]["expectations_count"] == 2
    assert "ship faster" in parsed[0]["first_expectation"]


def test_list_failure_propagates(tmp_path: Path):
    bad = tmp_path / "missing.json"
    rc, _, err = _run(["list"], eval_path=bad)
    assert rc == 1
    assert "list failed" in err


# ---------- run subcommand ----------

def test_run_emits_prelude_and_cases(sample_eval: Path):
    rc, out, _ = _run(["run"], eval_path=sample_eval)
    assert rc == 0
    assert "Test eval" in out  # description in prelude
    assert "Case 1/2: happy-path" in out
    assert "Case 2/2: edge-case" in out
    assert "Your task" in out
    assert "JSON object on a single line" in out


def test_run_prelude_documents_advisory_and_isolation(sample_eval: Path):
    rc, out, _ = _run(["run"], eval_path=sample_eval)
    assert rc == 0
    # Advisory-in-CI policy must be visible in the prelude.
    assert "advisory" in out.lower()
    # Web-search / interview-pipeline isolation must be called out so the
    # judge does not invoke real network calls or write to ~/.ai-quickstart.
    assert "isolation" in out.lower() or "do not call" in out.lower() or "do NOT call" in out


def test_run_with_case_filter(sample_eval: Path):
    rc, out, _ = _run(
        ["run", "--case-filter", "edge-case"], eval_path=sample_eval
    )
    assert rc == 0
    assert "Case 1/1: edge-case" in out
    assert "happy-path" not in out


def test_run_with_unknown_case_filter_errors(sample_eval: Path):
    rc, _, err = _run(
        ["run", "--case-filter", "no-such-case"], eval_path=sample_eval
    )
    assert rc == 2
    assert "no case matches" in err


def test_run_failure_exits_two(tmp_path: Path):
    bad = tmp_path / "missing.json"
    rc, _, err = _run(["run"], eval_path=bad)
    assert rc == 2
    assert "eval load failed" in err


# ---------- argparse ----------

def test_help_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        edi.main(["--help"])
    assert excinfo.value.code == 0


def test_no_subcommand_rejected():
    with pytest.raises(SystemExit):
        edi.main([])


# ---------- bundled eval ----------

def test_bundled_eval_validates():
    """The shipped evals/deeper_interview_quality.json must always validate."""
    rc, out, err = _run(["validate"])  # no override → uses default path
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["case_count"] >= 10  # spec asks for ~10 cases across archetypes
    assert all(isinstance(n, str) and n for n in parsed["case_names"])


def test_bundled_eval_covers_archetype_variety():
    """Spec calls out archetype variety: developer/researcher/PM/nonprofit/creator/etc."""
    data = edi.load_eval(edi.DEFAULT_EVAL_PATH)
    archetypes = {c["input"].get("archetype") for c in data["cases"]}
    # Must include at least job + personal + exploring across the suite.
    assert "job" in archetypes
    assert "personal" in archetypes
    assert "exploring" in archetypes


def test_render_prelude_and_case_prompt():
    case = {
        "name": "smoke",
        "input": {"archetype": "job", "entry_answers": {"role": "smoke"}},
        "expectations": ["does smoke equal smoke"],
    }
    eval_data = {"version": 1, "description": "smoke test", "cases": [case]}
    prelude = edi.render_prelude(eval_data)
    assert "smoke test" in prelude
    assert "Cases to evaluate: 1" in prelude

    block = edi.render_case_prompt(case, 0, 1)
    assert "smoke" in block
    assert "does smoke equal smoke" in block
    assert '"archetype": "job"' in block
    assert "JSON object on a single line" in block
