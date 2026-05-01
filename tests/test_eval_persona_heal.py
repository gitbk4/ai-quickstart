"""Tests for scripts/eval_persona_heal.py.

Covers schema validation, the 3 subcommands (run / validate / list), and
edge cases of the JSON loader. The actual semantic judgment of persona
prose happens at runtime in Claude Code; these tests cover the harness.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import eval_persona_heal as evh  # noqa: E402


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
                    "current_persona": {
                        "identity": {"role": "engineer", "archetype": "job"},
                    },
                    "anecdotes": [{"slug": "alpha", "content": "did stuff"}],
                    "activity_summary": {"weeks": {}},
                },
                "expectations": [
                    "prose mentions role",
                    "prose stays under 400 words",
                ],
            },
            {
                "name": "edge-case",
                "input": {"current_persona": {}, "anecdotes": [], "activity_summary": {}},
                "expectations": ["prose handles zero anecdotes gracefully"],
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
    rc = evh.main(argv, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


# ---------- load_eval ----------

def test_load_eval_happy_path(sample_eval: Path):
    data = evh.load_eval(sample_eval)
    assert data["version"] == 1
    assert len(data["cases"]) == 2


def test_load_eval_missing_file(tmp_path: Path):
    with pytest.raises(evh.EvalSchemaError, match="not found"):
        evh.load_eval(tmp_path / "no-such.json")


def test_load_eval_malformed_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json{{", encoding="utf-8")
    with pytest.raises(evh.EvalSchemaError, match="parse"):
        evh.load_eval(p)


def test_load_eval_root_not_object(tmp_path: Path):
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["just", "a", "list"]), encoding="utf-8")
    with pytest.raises(evh.EvalSchemaError, match="must be an object"):
        evh.load_eval(p)


def test_load_eval_missing_required_field(tmp_path: Path):
    p = tmp_path / "missing.json"
    p.write_text(json.dumps({"version": 1}), encoding="utf-8")  # no 'cases'
    with pytest.raises(evh.EvalSchemaError, match="cases"):
        evh.load_eval(p)


def test_load_eval_empty_cases(tmp_path: Path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"version": 1, "cases": []}), encoding="utf-8")
    with pytest.raises(evh.EvalSchemaError, match="non-empty"):
        evh.load_eval(p)


def test_load_eval_case_missing_field(tmp_path: Path):
    p = tmp_path / "bad-case.json"
    p.write_text(json.dumps({
        "version": 1,
        "cases": [{"name": "x"}]  # missing input + expectations
    }), encoding="utf-8")
    with pytest.raises(evh.EvalSchemaError, match="missing required field"):
        evh.load_eval(p)


def test_load_eval_case_empty_expectations(tmp_path: Path):
    p = tmp_path / "bad-exp.json"
    p.write_text(json.dumps({
        "version": 1,
        "cases": [{"name": "x", "input": {}, "expectations": []}]
    }), encoding="utf-8")
    with pytest.raises(evh.EvalSchemaError, match="non-empty list"):
        evh.load_eval(p)


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
    assert "role" in parsed[0]["first_expectation"]


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
        evh.main(["--help"])
    assert excinfo.value.code == 0


def test_no_subcommand_rejected():
    with pytest.raises(SystemExit):
        evh.main([])


# ---------- bundled eval ----------

def test_bundled_eval_validates():
    """The shipped evals/persona_heal_quality.json must always validate."""
    rc, out, err = _run(["validate"])  # no override → uses default path
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["case_count"] >= 10  # spec says 10+ cases
    assert all(isinstance(n, str) and n for n in parsed["case_names"])


def test_render_prelude_and_case_prompt():
    case = {
        "name": "smoke",
        "input": {"foo": "bar"},
        "expectations": ["does foo equal bar"],
    }
    eval_data = {"version": 1, "description": "smoke test", "cases": [case]}
    prelude = evh.render_prelude(eval_data)
    assert "smoke test" in prelude
    assert "Cases to evaluate: 1" in prelude

    block = evh.render_case_prompt(case, 0, 1)
    assert "smoke" in block
    assert "does foo equal bar" in block
    assert '"foo": "bar"' in block
    assert "JSON object on a single line" in block
