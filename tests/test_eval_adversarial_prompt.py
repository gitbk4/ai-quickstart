"""Tests for scripts/eval_adversarial_prompt.py.

Covers schema validation, the 4 subcommands (run / validate / list / mock),
end-to-end mock-judge aggregation, and edge cases of the JSON loader. The
actual semantic judgment of adversarial prompts happens at runtime in
Claude Code; these tests cover the harness only.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import eval_adversarial_prompt as eap  # noqa: E402


@pytest.fixture
def sample_eval(tmp_path: Path) -> Path:
    data = {
        "version": 1,
        "description": "Test adversarial eval",
        "judge_model": "claude-haiku",
        "pass_threshold": 0.8,
        "advisory_in_ci": True,
        "advisory_window_days": 30,
        "cases": [
            {
                "name": "vague-goal",
                "input": {
                    "prior_step_summary": (
                        "User wants to use AI to be more productive."
                    ),
                    "next_step_topic": (
                        "Step 2 skill recommendations for a developer"
                    ),
                },
                "expectations": [
                    "framing pushes back on vague goals",
                    "next-step topic is referenced",
                ],
            },
            {
                "name": "empty-prior",
                "input": {
                    "prior_step_summary": "",
                    "next_step_topic": "Step 1 deeper interview",
                },
                "expectations": [
                    "handles empty prior context gracefully",
                ],
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
    rc = eap.main(argv, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


# ---------- load_eval ----------

def test_load_eval_happy_path(sample_eval: Path):
    data = eap.load_eval(sample_eval)
    assert data["version"] == 1
    assert len(data["cases"]) == 2


def test_load_eval_missing_file(tmp_path: Path):
    with pytest.raises(eap.EvalSchemaError, match="not found"):
        eap.load_eval(tmp_path / "no-such.json")


def test_load_eval_malformed_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json{{", encoding="utf-8")
    with pytest.raises(eap.EvalSchemaError, match="parse"):
        eap.load_eval(p)


def test_load_eval_root_not_object(tmp_path: Path):
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["just", "a", "list"]), encoding="utf-8")
    with pytest.raises(eap.EvalSchemaError, match="must be an object"):
        eap.load_eval(p)


def test_load_eval_missing_required_field(tmp_path: Path):
    p = tmp_path / "missing.json"
    p.write_text(json.dumps({"version": 1}), encoding="utf-8")  # no 'cases'
    with pytest.raises(eap.EvalSchemaError, match="cases"):
        eap.load_eval(p)


def test_load_eval_empty_cases(tmp_path: Path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"version": 1, "cases": []}), encoding="utf-8")
    with pytest.raises(eap.EvalSchemaError, match="non-empty"):
        eap.load_eval(p)


def test_load_eval_case_missing_field(tmp_path: Path):
    p = tmp_path / "bad-case.json"
    p.write_text(json.dumps({
        "version": 1,
        "cases": [{"name": "x"}]  # missing input + expectations
    }), encoding="utf-8")
    with pytest.raises(eap.EvalSchemaError, match="missing required field"):
        eap.load_eval(p)


def test_load_eval_case_empty_expectations(tmp_path: Path):
    p = tmp_path / "bad-exp.json"
    p.write_text(json.dumps({
        "version": 1,
        "cases": [{
            "name": "x",
            "input": {"prior_step_summary": "", "next_step_topic": ""},
            "expectations": [],
        }],
    }), encoding="utf-8")
    with pytest.raises(eap.EvalSchemaError, match="non-empty list"):
        eap.load_eval(p)


def test_load_eval_case_input_missing_fields(tmp_path: Path):
    p = tmp_path / "bad-input.json"
    p.write_text(json.dumps({
        "version": 1,
        "cases": [{
            "name": "x",
            "input": {"prior_step_summary": ""},  # missing next_step_topic
            "expectations": ["something"],
        }],
    }), encoding="utf-8")
    with pytest.raises(eap.EvalSchemaError, match="next_step_topic"):
        eap.load_eval(p)


def test_load_eval_case_input_not_mapping(tmp_path: Path):
    p = tmp_path / "bad-input2.json"
    p.write_text(json.dumps({
        "version": 1,
        "cases": [{
            "name": "x",
            "input": "not a dict",
            "expectations": ["something"],
        }],
    }), encoding="utf-8")
    with pytest.raises(eap.EvalSchemaError, match="input must be a mapping"):
        eap.load_eval(p)


# ---------- render_adversarial_prompt ----------

def test_render_adversarial_prompt_includes_topic_and_summary(sample_eval: Path):
    data = eap.load_eval(sample_eval)
    case = data["cases"][0]
    rendered = eap.render_adversarial_prompt(case)
    assert "Step 2 skill recommendations for a developer" in rendered
    assert "use AI to be more productive" in rendered
    assert "Adversarial framing" in rendered
    assert "Prior step context" in rendered


def test_render_adversarial_prompt_handles_empty_prior(sample_eval: Path):
    data = eap.load_eval(sample_eval)
    empty_case = data["cases"][1]
    rendered = eap.render_adversarial_prompt(empty_case)
    # compose_adversarial substitutes a placeholder when prior is empty
    assert "_No prior context was supplied._" in rendered
    assert "Step 1 deeper interview" in rendered


# ---------- validate subcommand ----------

def test_validate_happy(sample_eval: Path):
    rc, out, err = _run(["validate"], eval_path=sample_eval)
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["case_count"] == 2
    assert parsed["case_names"] == ["vague-goal", "empty-prior"]
    assert parsed["advisory_in_ci"] is True


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
    assert parsed[0]["name"] == "vague-goal"
    assert parsed[0]["expectations_count"] == 2
    assert "vague" in parsed[0]["first_expectation"]
    assert "developer" in parsed[0]["next_step_topic"]


def test_list_failure_propagates(tmp_path: Path):
    bad = tmp_path / "missing.json"
    rc, _, err = _run(["list"], eval_path=bad)
    assert rc == 1
    assert "list failed" in err


# ---------- run subcommand ----------

def test_run_emits_prelude_and_cases(sample_eval: Path):
    rc, out, _ = _run(["run"], eval_path=sample_eval)
    assert rc == 0
    assert "Test adversarial eval" in out  # description in prelude
    assert "Case 1/2: vague-goal" in out
    assert "Case 2/2: empty-prior" in out
    assert "Your task" in out
    assert "JSON object on a single line" in out
    # The rendered adversarial prompt must appear in stdout for the judge
    # to evaluate.
    assert "Adversarial framing" in out
    assert "use AI to be more productive" in out
    # Advisory-in-CI note must be surfaced.
    assert "Advisory in CI" in out


def test_run_with_case_filter(sample_eval: Path):
    rc, out, _ = _run(
        ["run", "--case-filter", "empty-prior"], eval_path=sample_eval
    )
    assert rc == 0
    assert "Case 1/1: empty-prior" in out
    assert "vague-goal" not in out


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


# ---------- mock subcommand (end-to-end) ----------

def test_mock_runs_end_to_end(sample_eval: Path):
    rc, out, err = _run(["mock"], eval_path=sample_eval)
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["judge_mode"] == "mock"
    assert parsed["advisory_in_ci"] is True
    assert parsed["summary"]["total"] == 2
    assert parsed["summary"]["passed"] == 2  # canned-pass mock
    assert parsed["summary"]["failed"] == 0
    assert parsed["summary"]["score_avg"] == 1.0
    assert parsed["summary"]["threshold"] == 0.8
    assert len(parsed["verdicts"]) == 2
    # Each verdict has the expected shape.
    for v in parsed["verdicts"]:
        assert v["judge_mode"] == "mock"
        assert v["score"] == 1.0
        assert v["case"] in {"vague-goal", "empty-prior"}
        assert all(item["pass"] is True for item in v["verdicts"])


def test_mock_with_case_filter(sample_eval: Path):
    rc, out, err = _run(
        ["mock", "--case-filter", "vague-goal"], eval_path=sample_eval
    )
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["summary"]["total"] == 1
    assert parsed["verdicts"][0]["case"] == "vague-goal"


def test_mock_unknown_case_filter_errors(sample_eval: Path):
    rc, _, err = _run(
        ["mock", "--case-filter", "no-such"], eval_path=sample_eval
    )
    assert rc == 2
    assert "no case matches" in err


def test_mock_failure_propagates(tmp_path: Path):
    bad = tmp_path / "missing.json"
    rc, _, err = _run(["mock"], eval_path=bad)
    assert rc == 1
    assert "mock run failed" in err


# ---------- aggregation helper ----------

def test_aggregate_summary_passed_and_failed():
    verdicts = [
        {"score": 1.0},
        {"score": 0.9},
        {"score": 0.5},  # below threshold
        {"score": 0.0},  # below threshold
    ]
    summary = eap.aggregate_summary(verdicts, threshold=0.8)
    assert summary["total"] == 4
    assert summary["passed"] == 2
    assert summary["failed"] == 2
    assert summary["score_avg"] == 0.6
    assert summary["threshold"] == 0.8


def test_aggregate_summary_empty():
    summary = eap.aggregate_summary([], threshold=0.8)
    assert summary["total"] == 0
    assert summary["passed"] == 0
    assert summary["failed"] == 0
    assert summary["score_avg"] == 0.0


def test_mock_judge_returns_canned_pass(sample_eval: Path):
    data = eap.load_eval(sample_eval)
    case = data["cases"][0]
    verdict = eap.mock_judge(case)
    assert verdict["case"] == "vague-goal"
    assert verdict["score"] == 1.0
    assert verdict["judge_mode"] == "mock"
    assert len(verdict["verdicts"]) == len(case["expectations"])
    assert all(item["pass"] is True for item in verdict["verdicts"])


# ---------- argparse ----------

def test_help_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        eap.main(["--help"])
    assert excinfo.value.code == 0


def test_no_subcommand_rejected():
    with pytest.raises(SystemExit):
        eap.main([])


# ---------- bundled eval ----------

def test_bundled_eval_validates():
    """The shipped evals/adversarial_prompt_quality.json must always validate."""
    rc, out, err = _run(["validate"])  # no override → uses default path
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["case_count"] >= 10  # spec says ~10 cases
    assert all(isinstance(n, str) and n for n in parsed["case_names"])
    assert parsed["advisory_in_ci"] is True


def test_bundled_eval_renders_with_compose_adversarial():
    """End-to-end: every shipped case must render via compose_adversarial."""
    data = eap.load_eval(eap.DEFAULT_EVAL_PATH)
    for case in data["cases"]:
        rendered = eap.render_adversarial_prompt(case)
        assert isinstance(rendered, str) and rendered
        assert "Adversarial framing" in rendered
        assert "Prior step context" in rendered


def test_bundled_eval_mock_run_aggregates():
    """End-to-end mock run on the shipped eval — every case canned-passes."""
    rc, out, err = _run(["mock"])
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["summary"]["total"] >= 10
    assert parsed["summary"]["passed"] == parsed["summary"]["total"]
    assert parsed["summary"]["score_avg"] == 1.0


def test_render_prelude_and_case_prompt():
    case = {
        "name": "smoke",
        "input": {
            "prior_step_summary": "user said something",
            "next_step_topic": "Step 2",
        },
        "expectations": ["does framing push back"],
    }
    eval_data = {
        "version": 1,
        "description": "smoke test",
        "advisory_in_ci": True,
        "advisory_window_days": 30,
        "cases": [case],
    }
    prelude = eap.render_prelude(eval_data)
    assert "smoke test" in prelude
    assert "Cases to evaluate: 1" in prelude
    assert "Advisory in CI" in prelude

    block = eap.render_case_prompt(case, 0, 1)
    assert "smoke" in block
    assert "does framing push back" in block
    assert "user said something" in block
    assert "Step 2" in block
    assert "JSON object on a single line" in block
