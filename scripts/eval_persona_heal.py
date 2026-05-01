#!/usr/bin/env python3
"""Persona-heal eval harness — Claude-as-judge mode (no API key required).

The eval YAML at evals/persona_heal_quality.yaml lists cases. Each case has:
  * a fixture (current_persona, anecdotes, activity_summary)
  * a list of natural-language expectations ("prose mentions user's role",
    "stays under 400 words", "preserves stated top_problems", ...)

This harness is the DETERMINISTIC SIDE: load the YAML, validate schema,
print one structured prompt block per case to stdout. Claude (running the
SKILL.md `/ai-quickstart eval` orchestration) reads stdout, for each case:
  1. Synthesizes a candidate persona prose from the fixture inputs (acts as
     the heal LLM).
  2. Evaluates the candidate against each expectation (acts as the judge).
  3. Reports pass/fail per expectation.

This split keeps the eval runner free of API keys and CI-friendly for
schema-level checks. The actual semantic judgment happens inside the
Claude Code session at runtime.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
# JSON is the canonical format (stdlib-native; richer than what our flat-YAML
# parser handles). The original YAML in this repo was converted to JSON at
# build time; regenerate via PyYAML if you ever edit the source.
DEFAULT_EVAL_PATH = REPO_ROOT / "evals" / "persona_heal_quality.json"

REQUIRED_FIELDS = ("version", "cases")
REQUIRED_CASE_FIELDS = ("name", "input", "expectations")


class EvalSchemaError(ValueError):
    """Raised when the eval YAML is malformed or missing required fields."""


def load_eval(path: Path) -> Dict[str, Any]:
    """Load + validate the eval YAML. Returns the parsed dict.

    Schema (v1):
      version: 1
      description: "..."
      judge_model: claude-haiku   # advisory only; judge runs in Claude Code
      pass_threshold: 0.8
      cases:
        - name: <str>
          input:
            current_persona: { frontmatter: {...}, prose: "..." }
            anecdotes: [{slug, content}, ...]
            activity_summary: { ... }
          expectations:
            - "prose mentions user's role"
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
            raise EvalSchemaError(f"eval YAML missing required field: {f}")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalSchemaError("eval YAML 'cases' must be a non-empty list")
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise EvalSchemaError(f"case {i} is not a mapping")
        for f in REQUIRED_CASE_FIELDS:
            if f not in case:
                raise EvalSchemaError(f"case {i} ({case.get('name', '?')}) missing required field: {f}")
        if not isinstance(case["expectations"], list) or not case["expectations"]:
            raise EvalSchemaError(
                f"case {i} ({case['name']}) expectations must be a non-empty list"
            )
    return data


def render_case_prompt(case: Dict[str, Any], case_index: int, total: int) -> str:
    """Render one case as a markdown block Claude can act on.

    Format:
      # Case N/T: <name>
      ## Inputs
      <pretty-printed JSON>
      ## Expectations
      - <each expectation>
      ## Your task
      1. Synthesize candidate persona prose...
      2. Evaluate against each expectation...
      3. Emit a JSON verdict for this case.
    """
    name = case.get("name", f"case-{case_index}")
    inputs = case.get("input", {})
    expectations = case.get("expectations", [])

    lines = [
        f"# Case {case_index + 1}/{total}: {name}",
        "",
        "## Inputs",
        "",
        "```json",
        json.dumps(inputs, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Expectations",
        "",
    ]
    for exp in expectations:
        lines.append(f"- {exp}")
    lines.extend([
        "",
        "## Your task (Claude as both heal-LLM and judge)",
        "",
        "1. Read the inputs above. Treat them as if the heal pipeline just",
        "   handed them to you: the user's current persona, recent anecdotes,",
        "   and activity summary.",
        "2. Write a candidate replacement prose section (200-400 words) for",
        "   the persona. Stay grounded in the inputs; do not hallucinate.",
        "3. For each expectation listed above, judge whether your candidate",
        "   prose satisfies it. Be honest; partial credit is OK.",
        "4. Emit ONE JSON object on a single line with this shape:",
        "",
        "   ```json",
        "   {",
        f'     "case": "{name}",',
        '     "candidate_prose": "...",',
        '     "verdicts": [{"expectation": "...", "pass": true|false, "note": "..."}],',
        '     "score": 0.0-1.0',
        "   }",
        "   ```",
        "",
        "5. Then move to the next case. After all cases, emit a final",
        "   summary JSON: `{\"summary\": {\"total\": N, \"passed\": M, \"score_avg\": X}}`",
        "",
        "---",
        "",
    ])
    return "\n".join(lines)


def render_prelude(eval_data: Dict[str, Any]) -> str:
    """Top-of-output instructions for Claude orchestrating the eval session."""
    description = eval_data.get("description", "Persona heal quality eval")
    threshold = eval_data.get("pass_threshold", 0.8)
    n_cases = len(eval_data["cases"])
    return "\n".join([
        f"# {description}",
        "",
        f"Eval version: {eval_data.get('version', 1)}",
        f"Cases to evaluate: {n_cases}",
        f"Pass threshold (per case score): {threshold}",
        "",
        "You are running in **Claude-as-judge** mode. No external API needed.",
        "Below are individual cases. For each, follow the task instructions",
        "exactly: synthesize a candidate, judge it against the expectations,",
        "emit one JSON verdict line per case, then a final summary.",
        "",
        "---",
        "",
    ])


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
    total = len(cases)

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
    """Validate the YAML schema. Print a one-line summary on success."""
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
            "first_expectation": (c["expectations"][0] if c.get("expectations") else ""),
        })
    out.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval-persona-heal",
        description="Persona-heal eval harness (Claude-as-judge mode).",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    p_run = sub.add_parser(
        "run", help="Emit the full eval prompt for Claude to act on."
    )
    p_run.add_argument("--eval", default=None,
                       help=f"Path to eval YAML (default: {DEFAULT_EVAL_PATH}).")
    p_run.add_argument("--case-filter", default=None,
                       help="Run only the case with this exact name.")

    p_val = sub.add_parser("validate", help="Validate eval YAML schema only.")
    p_val.add_argument("--eval", default=None)

    p_list = sub.add_parser("list", help="List case names + expectation counts.")
    p_list.add_argument("--eval", default=None)

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
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
