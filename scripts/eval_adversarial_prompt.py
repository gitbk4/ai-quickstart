#!/usr/bin/env python3
"""Adversarial prompt eval harness — Claude-as-judge mode (no API key required).

The eval JSON at evals/adversarial_prompt_quality.json lists cases. Each
case has:
  * inputs (`prior_step_summary` + `next_step_topic`) that get fed to
    ``prompts.compose_adversarial`` to render the actual adversarial
    prompt body.
  * a list of natural-language expectations the rendered prompt must
    satisfy ("framing pushes back on vague goals", "preserves prior
    summary verbatim", "references next-step topic", ...).

This harness is the DETERMINISTIC SIDE: load the JSON, validate schema,
render every case's adversarial prompt via :func:`prompts.compose_adversarial`,
and print one structured prompt block per case to stdout. Claude (running
the SKILL.md ``/ai-quickstart eval`` orchestration) reads stdout and acts
as the judge — for each case it:
  1. Reads the rendered adversarial prompt.
  2. Evaluates it against each expectation.
  3. Reports pass/fail per expectation.

This split keeps the eval runner free of API keys and CI-friendly for
schema-level checks. The actual semantic judgment happens inside the
Claude Code session at runtime. The eval is **advisory in CI** for the
first 30 days (per PLAN.md eval-coverage diagram); failures should not
block merges during the advisory window.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVAL_PATH = REPO_ROOT / "evals" / "adversarial_prompt_quality.json"

# Make sibling scripts importable when this module is run directly.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import prompts  # noqa: E402  (sibling module)

REQUIRED_FIELDS = ("version", "cases")
REQUIRED_CASE_FIELDS = ("name", "input", "expectations")
REQUIRED_INPUT_FIELDS = ("prior_step_summary", "next_step_topic")


class EvalSchemaError(ValueError):
    """Raised when the eval JSON is malformed or missing required fields."""


def load_eval(path: Path) -> Dict[str, Any]:
    """Load + validate the eval JSON. Returns the parsed dict.

    Schema (v1):
      version: 1
      description: "..."
      judge_model: claude-haiku   # advisory only; judge runs in Claude Code
      pass_threshold: 0.8
      advisory_in_ci: true
      advisory_window_days: 30
      cases:
        - name: <str>
          input:
            prior_step_summary: "..."
            next_step_topic: "..."
          expectations:
            - "framing pushes back on vague goals"
            - "..."
    """
    if not path.is_file():
        raise EvalSchemaError(f"eval file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalSchemaError(f"failed to parse eval JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EvalSchemaError("eval JSON root must be an object")
    for f in REQUIRED_FIELDS:
        if f not in data:
            raise EvalSchemaError(f"eval JSON missing required field: {f}")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalSchemaError("eval JSON 'cases' must be a non-empty list")
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise EvalSchemaError(f"case {i} is not a mapping")
        for f in REQUIRED_CASE_FIELDS:
            if f not in case:
                raise EvalSchemaError(
                    f"case {i} ({case.get('name', '?')}) missing required field: {f}"
                )
        if not isinstance(case["expectations"], list) or not case["expectations"]:
            raise EvalSchemaError(
                f"case {i} ({case['name']}) expectations must be a non-empty list"
            )
        inp = case["input"]
        if not isinstance(inp, dict):
            raise EvalSchemaError(
                f"case {i} ({case['name']}) input must be a mapping"
            )
        for f in REQUIRED_INPUT_FIELDS:
            if f not in inp:
                raise EvalSchemaError(
                    f"case {i} ({case['name']}) input missing required field: {f}"
                )
    return data


def render_adversarial_prompt(case: Dict[str, Any]) -> str:
    """Render the adversarial prompt body for ``case`` via prompts.compose_adversarial.

    This is the artifact the judge actually evaluates — it's what the user's
    next-step LLM would see in production.
    """
    inputs = case["input"]
    return prompts.compose_adversarial(
        prior_step_summary=inputs.get("prior_step_summary", ""),
        next_step_topic=inputs.get("next_step_topic", ""),
    )


def render_case_prompt(case: Dict[str, Any], case_index: int, total: int) -> str:
    """Render one case as a markdown block Claude can act on.

    Format:
      # Case N/T: <name>
      ## Adversarial prompt (rendered)
      <fenced markdown of the actual prompt>
      ## Expectations
      - <each expectation>
      ## Your task
      1. Judge each expectation against the rendered prompt above.
      2. Emit a JSON verdict for this case.
    """
    name = case.get("name", f"case-{case_index}")
    expectations = case.get("expectations", [])
    rendered = render_adversarial_prompt(case)

    lines = [
        f"# Case {case_index + 1}/{total}: {name}",
        "",
        "## Inputs",
        "",
        "```json",
        json.dumps(case.get("input", {}), indent=2, ensure_ascii=False),
        "```",
        "",
        "## Adversarial prompt (rendered via prompts.compose_adversarial)",
        "",
        "```markdown",
        rendered.rstrip(),
        "```",
        "",
        "## Expectations",
        "",
    ]
    for exp in expectations:
        lines.append(f"- {exp}")
    lines.extend([
        "",
        "## Your task (Claude as judge)",
        "",
        "1. Read the rendered adversarial prompt above. This is the actual",
        "   artifact ai-quickstart writes to disk; the next-step LLM will",
        "   see this verbatim.",
        "2. For each expectation listed above, judge whether the rendered",
        "   prompt satisfies it. Be honest; partial credit is OK.",
        "3. Emit ONE JSON object on a single line with this shape:",
        "",
        "   ```json",
        "   {",
        f'     "case": "{name}",',
        '     "rendered_prompt_excerpt": "...",',
        '     "verdicts": [{"expectation": "...", "pass": true|false, "note": "..."}],',
        '     "score": 0.0-1.0',
        "   }",
        "   ```",
        "",
        "4. Then move to the next case. After all cases, emit a final",
        "   summary JSON: `{\"summary\": {\"total\": N, \"passed\": M, \"score_avg\": X}}`",
        "",
        "---",
        "",
    ])
    return "\n".join(lines)


def render_prelude(eval_data: Dict[str, Any]) -> str:
    """Top-of-output instructions for Claude orchestrating the eval session."""
    description = eval_data.get(
        "description", "Adversarial prompt quality eval"
    )
    threshold = eval_data.get("pass_threshold", 0.8)
    n_cases = len(eval_data["cases"])
    advisory = eval_data.get("advisory_in_ci", True)
    window = eval_data.get("advisory_window_days", 30)
    advisory_line = (
        f"Advisory in CI: {advisory} (window: {window} days). "
        "Failures during the advisory window do NOT block merges."
    )
    return "\n".join([
        f"# {description}",
        "",
        f"Eval version: {eval_data.get('version', 1)}",
        f"Cases to evaluate: {n_cases}",
        f"Pass threshold (per case score): {threshold}",
        advisory_line,
        "",
        "You are running in **Claude-as-judge** mode. No external API needed.",
        "Below are individual cases. For each, the harness has already",
        "rendered the adversarial prompt body via prompts.compose_adversarial.",
        "Your job is to judge whether the rendered prompt satisfies each",
        "stated expectation, emit one JSON verdict line per case, then a",
        "final summary.",
        "",
        "---",
        "",
    ])


# ---------------------------------------------------------------------------
# Mock-judge mode (used for unit tests + when no API key is present)
# ---------------------------------------------------------------------------


def mock_judge(case: Dict[str, Any]) -> Dict[str, Any]:
    """Return a canned passing verdict for one case.

    Used in CI / unit tests where no judge LLM is available. The verdict is
    structured exactly like a real judge's output so downstream aggregation
    code is exercised.
    """
    expectations = case.get("expectations", [])
    verdicts = [
        {
            "expectation": exp,
            "pass": True,
            "note": "mock-judge: canned pass (no real judgment performed)",
        }
        for exp in expectations
    ]
    return {
        "case": case.get("name", "?"),
        "rendered_prompt_excerpt": render_adversarial_prompt(case)[:200],
        "verdicts": verdicts,
        "score": 1.0,
        "judge_mode": "mock",
    }


def aggregate_summary(verdicts: List[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    """Aggregate per-case verdicts into a summary dict.

    Returns: total, passed (cases with score >= threshold), score_avg,
    advisory note.
    """
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v.get("score", 0.0) >= threshold)
    score_avg = (
        sum(v.get("score", 0.0) for v in verdicts) / total if total else 0.0
    )
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "score_avg": round(score_avg, 4),
        "threshold": threshold,
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, stdout=None, stderr=None) -> int:
    """Render the full eval prompt to stdout."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    eval_path = Path(args.eval) if args.eval else DEFAULT_EVAL_PATH
    try:
        data = load_eval(eval_path)
    except EvalSchemaError as exc:
        err.write(f"eval load failed: {exc}\n")
        return 2
    cases = data["cases"]

    if args.case_filter:
        cases = [c for c in cases if c.get("name") == args.case_filter]
        if not cases:
            err.write(f"no case matches name '{args.case_filter}'\n")
            return 2

    total = len(cases)

    out.write(render_prelude(data))
    for i, case in enumerate(cases):
        out.write(render_case_prompt(case, i, total))
    out.flush()
    return 0


def cmd_validate(args: argparse.Namespace, stdout=None, stderr=None) -> int:
    """Validate the JSON schema. Print a one-line summary on success."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    eval_path = Path(args.eval) if args.eval else DEFAULT_EVAL_PATH
    try:
        data = load_eval(eval_path)
    except EvalSchemaError as exc:
        err.write(f"validation failed: {exc}\n")
        return 1
    n = len(data["cases"])
    out.write(json.dumps({
        "ok": True,
        "path": str(eval_path),
        "version": data.get("version"),
        "case_count": n,
        "case_names": [c["name"] for c in data["cases"]],
        "advisory_in_ci": data.get("advisory_in_ci", True),
    }, ensure_ascii=False) + "\n")
    return 0


def cmd_list(args: argparse.Namespace, stdout=None, stderr=None) -> int:
    """List cases (name + first expectation) for quick inspection."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    eval_path = Path(args.eval) if args.eval else DEFAULT_EVAL_PATH
    try:
        data = load_eval(eval_path)
    except EvalSchemaError as exc:
        err.write(f"list failed: {exc}\n")
        return 1
    rows = []
    for c in data["cases"]:
        rows.append({
            "name": c["name"],
            "expectations_count": len(c.get("expectations", [])),
            "first_expectation": (
                c["expectations"][0] if c.get("expectations") else ""
            ),
            "next_step_topic": c.get("input", {}).get("next_step_topic", ""),
        })
    out.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return 0


def cmd_mock(args: argparse.Namespace, stdout=None, stderr=None) -> int:
    """Run the eval end-to-end with the mock judge.

    Useful for CI smoke tests: exercises load → render → judge → aggregate
    without needing a real API key. Prints a JSON summary table on stdout.
    """
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    eval_path = Path(args.eval) if args.eval else DEFAULT_EVAL_PATH
    try:
        data = load_eval(eval_path)
    except EvalSchemaError as exc:
        err.write(f"mock run failed: {exc}\n")
        return 1
    threshold = float(data.get("pass_threshold", 0.8))
    cases = data["cases"]
    if args.case_filter:
        cases = [c for c in cases if c.get("name") == args.case_filter]
        if not cases:
            err.write(f"no case matches name '{args.case_filter}'\n")
            return 2

    verdicts = [mock_judge(c) for c in cases]
    summary = aggregate_summary(verdicts, threshold)
    payload = {
        "eval": str(eval_path),
        "judge_mode": "mock",
        "advisory_in_ci": data.get("advisory_in_ci", True),
        "advisory_window_days": data.get("advisory_window_days", 30),
        "summary": summary,
        "verdicts": verdicts,
    }
    out.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval-adversarial-prompt",
        description="Adversarial-prompt eval harness (Claude-as-judge mode).",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    p_run = sub.add_parser(
        "run", help="Emit the full eval prompt for Claude to act on."
    )
    p_run.add_argument(
        "--eval", default=None,
        help=f"Path to eval JSON (default: {DEFAULT_EVAL_PATH}).",
    )
    p_run.add_argument(
        "--case-filter", default=None,
        help="Run only the case with this exact name.",
    )

    p_val = sub.add_parser("validate", help="Validate eval JSON schema only.")
    p_val.add_argument("--eval", default=None)

    p_list = sub.add_parser("list", help="List case names + expectation counts.")
    p_list.add_argument("--eval", default=None)

    p_mock = sub.add_parser(
        "mock",
        help="Run end-to-end with the canned mock judge (CI-friendly smoke).",
    )
    p_mock.add_argument("--eval", default=None)
    p_mock.add_argument("--case-filter", default=None)

    return p


def main(argv: Optional[List[str]] = None, stdout=None, stderr=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args, stdout=stdout, stderr=stderr)
    if args.cmd == "validate":
        return cmd_validate(args, stdout=stdout, stderr=stderr)
    if args.cmd == "list":
        return cmd_list(args, stdout=stdout, stderr=stderr)
    if args.cmd == "mock":
        return cmd_mock(args, stdout=stdout, stderr=stderr)
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
