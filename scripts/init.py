#!/usr/bin/env python3
"""Init: CLI orchestrator for ai-quickstart.

Deterministic side of the 3-step user flow. The LLM (Claude in SKILL.md
orchestration) drives the conversational interview and synthesis; this
module wires together the supporting modules:

  * start            — open a session, write step-1 adversarial prompt
  * record-answers   — persist captured interview answers (JSON on stdin)
  * suggest          — load curated mapping, gather + rank, write step-2 prompt
  * prepare-scope-review — compose Phase 2.5 plan doc for /plan-ceo-review
  * accept           — scaffold each accepted project (JSON on stdin)
  * add-starting-files — copy user-specified files into <project>/context/raw/
  * status           — summary of managed projects, latest run, persona, hooks

All file IO is atomic. Subcommands exit 0 on success, non-zero on failure
with a clear stderr message. Stdout is reserved for machine-readable JSON.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# Sibling imports via bare-name pattern (matches heal.py — avoids PEP 420
# namespace-package collisions when tests put scripts/ on sys.path).
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

import interview  # type: ignore  # noqa: E402
import suggest  # type: ignore  # noqa: E402
import scaffold  # type: ignore  # noqa: E402
import scope_review  # type: ignore  # noqa: E402
import persona  # type: ignore  # noqa: E402
import hooks_install  # type: ignore  # noqa: E402
import paths as paths_mod  # type: ignore  # noqa: E402
import eval_persona_heal  # type: ignore  # noqa: E402
import next_project as next_project_mod  # type: ignore  # noqa: E402


VALID_ARCHETYPES = ("job", "personal", "exploring")
DEFAULT_MAPPING_PATH = Path(__file__).resolve().parent.parent / "mappings" / "personas.yaml"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _home_root() -> Path:
    env = os.environ.get("AI_QUICKSTART_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".ai-quickstart"


def _read_stdin_json(stdin) -> Any:
    text = stdin.read()
    if not text.strip():
        raise ValueError("expected JSON on stdin, got empty input")
    return json.loads(text)


def _emit_json(data: Any, stdout) -> None:
    stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    stdout.flush()


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy src to dst atomically via tmp+rename. Raises on any IO failure."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        delete=False, dir=str(dst.parent), prefix=".tmp-", suffix=dst.suffix
    )
    tmp.close()
    try:
        shutil.copyfile(str(src), tmp.name)
        os.replace(tmp.name, str(dst))
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _is_safe_source_file(path: Path) -> bool:
    """Reject paths that don't exist, are dirs, are symlinks, or are unreadable.

    Caller must pass an UN-RESOLVED path so the symlink check works (resolve()
    follows symlinks; is_symlink() on the resolved path always returns False).
    """
    if path.is_symlink():
        return False
    if not path.exists():
        return False
    if not path.is_file():
        return False
    try:
        with path.open("rb") as f:
            f.read(1)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# subcommand: start
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace, stdout=None, stderr=None) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    archetype = args.archetype
    if archetype not in VALID_ARCHETYPES:
        err.write(
            f"unknown archetype '{archetype}'; expected one of {VALID_ARCHETYPES}\n"
        )
        return 2
    try:
        result = interview.start_session(archetype)
    except ValueError as exc:
        err.write(f"start failed: {exc}\n")
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        err.write(f"start failed: {exc}\n")
        return 1
    _emit_json(result, out)
    return 0


# ---------------------------------------------------------------------------
# subcommand: record-answers
# ---------------------------------------------------------------------------

def cmd_record_answers(
    args: argparse.Namespace, stdin=None, stdout=None, stderr=None
) -> int:
    sin = stdin or sys.stdin
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    if not args.run_id:
        err.write("--run-id required\n")
        return 2
    try:
        answers = _read_stdin_json(sin)
    except (ValueError, json.JSONDecodeError) as exc:
        err.write(f"record-answers failed: invalid JSON on stdin: {exc}\n")
        return 2
    if not isinstance(answers, dict):
        err.write("record-answers failed: stdin must be a JSON object\n")
        return 2
    try:
        path = interview.record_answers(args.run_id, answers)
    except Exception as exc:
        err.write(f"record-answers failed: {exc}\n")
        return 1
    _emit_json({"ok": True, "run_id": args.run_id, "answers_path": str(path)}, out)
    return 0


# ---------------------------------------------------------------------------
# subcommand: suggest
# ---------------------------------------------------------------------------

def cmd_suggest(
    args: argparse.Namespace, stdout=None, stderr=None
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    if not args.run_id:
        err.write("--run-id required\n")
        return 2

    answers = interview.read_answers(args.run_id)
    if answers is None:
        err.write(
            f"suggest failed: no answers recorded for run_id={args.run_id}; "
            "run 'record-answers' first\n"
        )
        return 2

    mapping_path = Path(args.mapping) if args.mapping else DEFAULT_MAPPING_PATH
    # Validate mapping loads cleanly first; emits a clearer error than gather would.
    try:
        suggest.load_mapping(mapping_path)
    except Exception as exc:
        err.write(f"suggest failed: cannot load mapping at {mapping_path}: {exc}\n")
        return 1

    try:
        result = suggest.gather(answers, mapping_path)
    except Exception as exc:
        err.write(f"suggest failed during gather: {exc}\n")
        return 1

    # Compose + persist the step-2 adversarial prompt for Claude to read.
    try:
        interview.compose_step2_context(args.run_id, answers, result)
    except Exception as exc:
        err.write(f"warning: couldn't write step-2 prompt: {exc}\n")
        # non-fatal — the suggestions themselves are still useful

    _emit_json(result, out)
    return 0


# ---------------------------------------------------------------------------
# subcommand: prepare-scope-review
# ---------------------------------------------------------------------------

def cmd_prepare_scope_review(
    args: argparse.Namespace, stdout=None, stderr=None
) -> int:
    """Phase 2.5 hookup: compose a plan doc for the gstack /plan-ceo-review skill.

    Reads the answers persisted under ``run_id``, regathers suggestions
    (so the doc reflects current curated + freshness data), composes the
    project-shaped plan via :mod:`scope_review`, and prints
    ``{plan_path, prompt_path, project_slug}`` JSON to stdout.

    Exit codes:
      * 0 — plan written
      * 2 — missing/invalid args, missing answers, or empty suggestions for
        the requested slug
      * 1 — unexpected error (mapping load, IO failure)
    """
    out = stdout or sys.stdout
    err = stderr or sys.stderr

    if not args.run_id:
        err.write("prepare-scope-review failed: --run-id required\n")
        return 2
    slug = (args.project_slug or "").strip()
    if not slug:
        err.write("prepare-scope-review failed: --project-slug required\n")
        return 2

    answers = interview.read_answers(args.run_id)
    if answers is None:
        err.write(
            "prepare-scope-review failed: no answers recorded for "
            f"run_id={args.run_id}; run 'record-answers' first\n"
        )
        return 2

    mapping_path = Path(args.mapping) if args.mapping else DEFAULT_MAPPING_PATH
    try:
        suggestions = suggest.gather(answers, mapping_path)
    except Exception as exc:
        err.write(f"prepare-scope-review failed during gather: {exc}\n")
        return 1

    # Build a project_spec around the requested slug. We don't require the
    # slug to be a curated template — the user may have invented one — but
    # we surface the curated template list so the reviewer can see other
    # options the suggestion engine considered.
    templates = suggestions.get("project_templates") or []
    matched_template = slug if slug in templates else None
    project_spec: Dict[str, Any] = {
        "slug": slug,
        "project_template": matched_template or slug,
    }

    try:
        plan_path = scope_review.prepare(
            run_id=args.run_id,
            project_spec=project_spec,
            answers=answers,
            suggestions=suggestions,
        )
    except (ValueError, TypeError) as exc:
        err.write(f"prepare-scope-review failed: {exc}\n")
        return 2
    except Exception as exc:
        err.write(f"prepare-scope-review failed writing plan: {exc}\n")
        return 1

    # Build the invocation prompt and persist it next to the plan so Claude
    # can paste it verbatim into the /plan-ceo-review Skill tool call.
    try:
        prompt_text = scope_review.prepare_invocation_prompt(plan_path, slug)
    except Exception as exc:
        err.write(
            f"prepare-scope-review failed building invocation prompt: {exc}\n"
        )
        return 1

    prompt_path = plan_path.with_name("scope-review-invocation-prompt.md")
    tmp = prompt_path.with_suffix(prompt_path.suffix + ".tmp")
    try:
        tmp.write_text(prompt_text, encoding="utf-8")
        os.replace(tmp, prompt_path)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        err.write(f"prepare-scope-review failed writing prompt: {exc}\n")
        return 1

    _emit_json(
        {
            "plan_path": str(plan_path),
            "prompt_path": str(prompt_path),
            "project_slug": slug,
        },
        out,
    )
    return 0


# ---------------------------------------------------------------------------
# subcommand: accept
# ---------------------------------------------------------------------------

def cmd_accept(
    args: argparse.Namespace, stdin=None, stdout=None, stderr=None
) -> int:
    sin = stdin or sys.stdin
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    if not args.run_id:
        err.write("--run-id required\n")
        return 2
    try:
        payload = _read_stdin_json(sin)
    except (ValueError, json.JSONDecodeError) as exc:
        err.write(f"accept failed: invalid JSON on stdin: {exc}\n")
        return 2
    if not isinstance(payload, dict):
        err.write("accept failed: stdin must be a JSON object\n")
        return 2

    project_specs = payload.get("project_specs", [])
    if not isinstance(project_specs, list):
        err.write("accept failed: 'project_specs' must be a list\n")
        return 2

    results: List[Dict[str, Any]] = []
    any_failed = False
    for spec in project_specs:
        if not isinstance(spec, dict):
            results.append({"ok": False, "error": "spec is not an object", "spec": spec})
            any_failed = True
            continue
        slug = spec.get("slug")
        project_dir = spec.get("dir")
        anecdote_seed = spec.get("anecdote_seed", "")
        skills = spec.get("skills", [])
        if not slug or not project_dir:
            results.append(
                {"ok": False, "error": "spec missing 'slug' or 'dir'", "spec": spec}
            )
            any_failed = True
            continue
        try:
            res = scaffold.scaffold_project(
                project_slug=slug,
                project_dir=Path(project_dir),
                suggested_skills=skills,
                anecdote_seed=anecdote_seed,
                dry_run=args.dry_run,
            )
            results.append({"ok": True, **res})
        except Exception as exc:
            results.append(
                {"ok": False, "slug": slug, "dir": project_dir, "error": str(exc)}
            )
            any_failed = True

    _emit_json({"run_id": args.run_id, "projects": results}, out)
    return 1 if any_failed else 0


# ---------------------------------------------------------------------------
# subcommand: add-starting-files
# ---------------------------------------------------------------------------

def cmd_add_starting_files(
    args: argparse.Namespace, stdin=None, stdout=None, stderr=None
) -> int:
    sin = stdin or sys.stdin
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    project_dir = Path(args.project_dir).expanduser()
    if not project_dir.is_dir():
        err.write(f"add-starting-files failed: {project_dir} is not a directory\n")
        return 2
    raw_dir = project_dir / "context" / "raw"
    if not raw_dir.is_dir():
        err.write(
            f"add-starting-files failed: {raw_dir} doesn't exist; was the "
            "project scaffolded with compathy?\n"
        )
        return 2

    try:
        files = _read_stdin_json(sin)
    except (ValueError, json.JSONDecodeError) as exc:
        err.write(f"add-starting-files failed: invalid JSON on stdin: {exc}\n")
        return 2
    if not isinstance(files, list):
        err.write("add-starting-files failed: stdin must be a JSON list of paths\n")
        return 2

    copied: List[str] = []
    skipped: List[Dict[str, str]] = []
    for entry in files:
        if not isinstance(entry, str):
            skipped.append({"path": str(entry), "reason": "not a string"})
            continue
        src = Path(entry).expanduser()
        # Check symlink before resolving (resolve follows links).
        if not _is_safe_source_file(src):
            reason = (
                "symlink rejected" if src.is_symlink()
                else "not a regular readable file"
            )
            skipped.append({"path": str(src), "reason": reason})
            continue
        dst = raw_dir / src.name
        try:
            _atomic_copy(src, dst)
            copied.append(str(dst))
        except OSError as exc:
            skipped.append({"path": str(src), "reason": f"copy failed: {exc}"})

    _emit_json({"copied": copied, "skipped": skipped}, out)
    return 0 if not skipped else 0  # partial success still exits 0; report has details


# ---------------------------------------------------------------------------
# subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace, stdout=None, stderr=None) -> int:
    out = stdout or sys.stdout
    home = _home_root()
    runs_dir = home / "runs"
    persona_path = home / "persona" / "persona.md"
    managed_path = home / "managed-projects.json"

    # latest run-id by mtime
    latest_run = None
    if runs_dir.is_dir():
        run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
        if run_dirs:
            latest_run = max(run_dirs, key=lambda p: p.stat().st_mtime).name

    # persona summary
    persona_info: Dict[str, Any] = {"exists": persona_path.is_file()}
    if persona_info["exists"]:
        try:
            parsed = persona.parse_persona(persona_path)
            gen = parsed["frontmatter"].get("generated", {})
            persona_info["version"] = gen.get("version")
            persona_info["updated_at"] = gen.get("updated_at")
            persona_info["anecdote_count"] = gen.get("anecdote_count")
        except Exception as exc:  # pragma: no cover - defensive
            persona_info["parse_error"] = str(exc)

    # managed projects count
    managed_count = 0
    if managed_path.is_file():
        try:
            data = json.loads(managed_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                managed_count = len(data)
        except (OSError, ValueError):
            managed_count = -1  # corrupt registry signal

    summary = {
        "ai_quickstart_home": str(home),
        "managed_projects_count": managed_count,
        "latest_run_id": latest_run,
        "persona": persona_info,
        "hooks_installed": hooks_install.is_installed(),
    }
    _emit_json(summary, out)
    return 0


# ---------------------------------------------------------------------------
# subcommand: next-project
# ---------------------------------------------------------------------------

def _default_persona_path() -> Path:
    return _home_root() / "persona" / "persona.md"


def cmd_next_project(args: argparse.Namespace, stdout=None, stderr=None) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    persona_path = (
        Path(args.persona).expanduser() if args.persona else _default_persona_path()
    )
    mapping_path = Path(args.mapping) if args.mapping else DEFAULT_MAPPING_PATH
    if not persona_path.exists():
        err.write(
            f"next-project failed: persona file not found at {persona_path}; "
            "run /ai-quickstart first to create one\n"
        )
        return 2
    try:
        result = next_project_mod.recommend(
            persona_path=persona_path,
            mapping_path=mapping_path,
            top_n=args.top,
        )
    except FileNotFoundError as exc:
        err.write(f"next-project failed: {exc}\n")
        return 2
    except (ValueError, OSError) as exc:
        err.write(f"next-project failed: {exc}\n")
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        err.write(f"next-project failed: {exc}\n")
        return 1
    _emit_json(result, out)
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-quickstart",
        description="ai-quickstart CLI orchestrator (3-step interview/suggest/scaffold flow).",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    p_start = sub.add_parser("start", help="Open a session and write the step-1 prompt.")
    p_start.add_argument("--archetype", required=True, choices=VALID_ARCHETYPES)

    p_rec = sub.add_parser(
        "record-answers", help="Persist interview answers (JSON on stdin)."
    )
    p_rec.add_argument("--run-id", required=True)

    p_sug = sub.add_parser(
        "suggest",
        help="Load curated mapping + live freshness; write step-2 prompt; emit JSON.",
    )
    p_sug.add_argument("--run-id", required=True)
    p_sug.add_argument(
        "--mapping",
        default=None,
        help=f"Path to mappings YAML (default: {DEFAULT_MAPPING_PATH}).",
    )

    p_psr = sub.add_parser(
        "prepare-scope-review",
        help=(
            "Phase 2.5: compose a plan doc for the gstack /plan-ceo-review "
            "skill and emit {plan_path, prompt_path, project_slug} JSON."
        ),
    )
    p_psr.add_argument("--run-id", required=True)
    p_psr.add_argument("--project-slug", required=True)
    p_psr.add_argument(
        "--mapping",
        default=None,
        help=f"Path to mappings YAML (default: {DEFAULT_MAPPING_PATH}).",
    )

    p_acc = sub.add_parser(
        "accept",
        help="Scaffold each accepted project (JSON {project_specs:[...]} on stdin).",
    )
    p_acc.add_argument("--run-id", required=True)
    p_acc.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan actions without writing.",
    )

    p_add = sub.add_parser(
        "add-starting-files",
        help="Copy files into <project>/context/raw/ (JSON list on stdin).",
    )
    p_add.add_argument("--project-dir", required=True)

    sub.add_parser(
        "status", help="Print a JSON summary of ai-quickstart state."
    )

    p_eval = sub.add_parser(
        "eval",
        help="Emit the persona-heal eval prompt for Claude-as-judge mode.",
    )
    p_eval.add_argument("--eval-file", default=None,
                        help="Path to eval JSON (default: bundled).")
    p_eval.add_argument("--case-filter", default=None,
                        help="Run only the case with this exact name.")

    p_next = sub.add_parser(
        "next-project",
        help="Recommend the user's NEXT project from persona + curated mapping.",
    )
    p_next.add_argument(
        "--top", type=int, default=5,
        help="Maximum number of recommendations to return (default: 5).",
    )
    p_next.add_argument(
        "--mapping", default=None,
        help=f"Path to mappings YAML (default: {DEFAULT_MAPPING_PATH}).",
    )
    p_next.add_argument(
        "--persona", default=None,
        help="Path to persona.md (default: $AI_QUICKSTART_HOME/persona/persona.md).",
    )

    return p


def main(argv: Optional[List[str]] = None, stdin=None, stdout=None, stderr=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "start":
        return cmd_start(args, stdout=stdout, stderr=stderr)
    if args.cmd == "record-answers":
        return cmd_record_answers(args, stdin=stdin, stdout=stdout, stderr=stderr)
    if args.cmd == "suggest":
        return cmd_suggest(args, stdout=stdout, stderr=stderr)
    if args.cmd == "prepare-scope-review":
        return cmd_prepare_scope_review(args, stdout=stdout, stderr=stderr)
    if args.cmd == "accept":
        return cmd_accept(args, stdin=stdin, stdout=stdout, stderr=stderr)
    if args.cmd == "add-starting-files":
        return cmd_add_starting_files(args, stdin=stdin, stdout=stdout, stderr=stderr)
    if args.cmd == "status":
        return cmd_status(args, stdout=stdout, stderr=stderr)
    if args.cmd == "eval":
        # Delegate to eval_persona_heal.run with arg shape it expects.
        delegated_argv = ["run"]
        if args.eval_file:
            delegated_argv += ["--eval", args.eval_file]
        if args.case_filter:
            delegated_argv += ["--case-filter", args.case_filter]
        return eval_persona_heal.main(delegated_argv, stdout=stdout, stderr=stderr)
    if args.cmd == "next-project":
        return cmd_next_project(args, stdout=stdout, stderr=stderr)
    return 2  # pragma: no cover - argparse rejects unknowns first


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
