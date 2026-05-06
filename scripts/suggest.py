"""Suggest module: deterministic side of Step 2.

Pattern (compathy split):
  * Python (this module) is the deterministic half. It parses the curated
    ``mappings/personas.yaml``, queries the three live sources in parallel,
    applies quality flags, and ranks results so the same input produces the
    same output every run.
  * Claude (in SKILL.md orchestration) is the synthesis half. It reads the
    Step 2 adversarial prompt produced by :func:`interview.compose_step2_context`,
    consumes the structured suggestions returned here, and turns them into
    a short list with per-item "why this user" reasons.

Public API:
  * ``load_mapping(mapping_path) -> dict``
  * ``gather(answers, mapping_path, max_workers=3, persona=None) -> dict``
  * ``apply_user_edits(suggestions, accepted, rejected) -> dict``
  * ``attach_alternatives(suggestions, persona, *, home=None) -> dict``
    Wave 2A: decorates each skill / mcp_server with an ``alternatives``
    list (1-2 items), emits one ``suggestion.surfaced`` telemetry event
    summarising the count rendered. Non-blocking: errors land on stderr,
    suggestions dict is returned unchanged on failure.

Determinism: ``gather`` ranks every output list by
``(source_tier_priority, -stars, has_warnings, name)``. The same inputs and
the same source responses will always produce the same output ordering. The
parallel-fetch step uses ``concurrent.futures.ThreadPoolExecutor`` purely
for latency; results are reordered after the fact.

Stdlib only. Python 3.9+. No PyYAML — there is a small flat-YAML parser
inline (~70 lines) that handles the specific shape of ``personas.yaml`` and
fails loudly on anything else.
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Sibling-import pattern matching heal.py / interview.py.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))
from sources import github, mcp_registry, mcpmarket  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOW_QUALITY_STAR_THRESHOLD = 100
SCHEMA_VERSION = 1

# Source tier priority for deterministic ranking. Lower number ranks first.
# Curated GitHub-cited skills are the most trustworthy in v1, then registry,
# then mcpmarket scrape. Tied priorities fall through to (stars, warnings, name).
_TIER_PRIORITY = {
    "github": 0,
    "mcp-registry": 1,
    "mcpmarket": 2,
    "curated": 3,
}


# ---------------------------------------------------------------------------
# YAML parser (specific to personas.yaml)
# ---------------------------------------------------------------------------
#
# Supported subset:
#   key: scalar
#   key: [a, b, c]            (flat list of scalars on one line)
#   key:                      (block-mapping start)
#     subkey: ...
#   - key: scalar             (list-item dict, common shape under claude_skills)
#     other_key: ...
#
# The parser tracks indentation as the ``depth`` of each line and dispatches
# accordingly. It is deliberately strict: anything outside this subset
# raises ValueError with a line number so a curated mapping bug fails loudly.


def _parse_scalar(v: str) -> Any:
    s = v.strip()
    if s == "":
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _split_top_commas(s: str) -> List[str]:
    out: List[str] = []
    cur: List[str] = []
    quote: Optional[str] = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            cur.append(ch)
            continue
        if ch == ",":
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur).strip())
    return out


def _parse_inline_value(val: str, lineno: int) -> Any:
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        if "[" in inner or "]" in inner:
            raise ValueError(f"line {lineno}: nested lists not supported")
        return [_parse_scalar(p) for p in _split_top_commas(inner) if p.strip()]
    return _parse_scalar(val)


def _indent_of(line: str) -> int:
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            # Treat tabs as 2 spaces for the purpose of structural depth.
            n += 2
        else:
            break
    return n


def _parse_yaml(text: str) -> Dict[str, Any]:
    """Parse the supported subset. Returns the top-level mapping."""
    raw_lines = text.splitlines()
    # Strip blanks and comment-only lines while preserving line numbers for errors.
    lines: List[Tuple[int, int, str]] = []  # (lineno, indent, content)
    for i, ln in enumerate(raw_lines, 1):
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip trailing inline comments only when they are clearly comments
        # (preceded by whitespace + #). We do not handle # inside quoted strings.
        body = ln
        # Find a " #" that isn't inside quotes.
        in_q: Optional[str] = None
        cut = -1
        for j, ch in enumerate(body):
            if in_q:
                if ch == in_q:
                    in_q = None
                continue
            if ch in ('"', "'"):
                in_q = ch
                continue
            if ch == "#" and j > 0 and body[j - 1] in (" ", "\t"):
                cut = j
                break
        if cut >= 0:
            body = body[:cut].rstrip()
        if body.strip() == "":
            continue
        lines.append((i, _indent_of(body), body.rstrip()))

    pos = [0]  # mutable index so helpers can advance it

    def parse_block_mapping(depth: int) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        while pos[0] < len(lines):
            lineno, indent, content = lines[pos[0]]
            if indent < depth:
                return out
            if indent > depth:
                raise ValueError(
                    f"line {lineno}: unexpected indentation (got {indent}, want {depth})"
                )
            stripped = content.lstrip()
            if stripped.startswith("- "):
                # A list item where a mapping key was expected -> end of this map.
                return out
            if ":" not in stripped:
                raise ValueError(f"line {lineno}: missing ':' separator")
            key, _, val = stripped.partition(":")
            key = key.strip()
            val_stripped = val.strip()
            pos[0] += 1
            if val_stripped == "":
                # Look ahead for child indent.
                child = _peek_indent()
                if child is None or child <= depth:
                    out[key] = {}
                    continue
                # Could be a list of items or a nested mapping.
                if _peek_starts_with_dash():
                    out[key] = parse_block_list(child)
                else:
                    out[key] = parse_block_mapping(child)
            else:
                out[key] = _parse_inline_value(val_stripped, lineno)
        return out

    def parse_block_list(depth: int) -> List[Any]:
        out: List[Any] = []
        while pos[0] < len(lines):
            lineno, indent, content = lines[pos[0]]
            if indent < depth:
                return out
            if indent > depth:
                raise ValueError(
                    f"line {lineno}: unexpected indentation in list (got {indent}, want {depth})"
                )
            stripped = content.lstrip()
            if not stripped.startswith("- "):
                return out
            after = stripped[2:].strip()
            pos[0] += 1
            if ":" in after and not (after.startswith("[") and after.endswith("]")):
                # First key of a dict-shaped list item, on the same line as the dash.
                key, _, val = after.partition(":")
                key = key.strip()
                val_stripped = val.strip()
                item: Dict[str, Any] = {}
                if val_stripped == "":
                    child = _peek_indent()
                    if child is not None and child > depth and not _peek_starts_with_dash():
                        item[key] = parse_block_mapping(child)
                    else:
                        item[key] = ""
                else:
                    item[key] = _parse_inline_value(val_stripped, lineno)
                # Continue collecting subsequent indented mapping keys at item-body depth.
                # The body lives at depth + 2 by convention (two-space indent).
                body_depth = depth + 2
                if _peek_indent() == body_depth and not _peek_starts_with_dash():
                    rest = parse_block_mapping(body_depth)
                    for k, v in rest.items():
                        item[k] = v
                out.append(item)
            elif after == "":
                # Bare dash with mapping body on subsequent indented lines.
                body_depth = depth + 2
                if _peek_indent() == body_depth:
                    if _peek_starts_with_dash():
                        out.append(parse_block_list(body_depth))
                    else:
                        out.append(parse_block_mapping(body_depth))
                else:
                    out.append(None)
            else:
                # Scalar list item.
                out.append(_parse_inline_value(after, lineno))
        return out

    def _peek_indent() -> Optional[int]:
        if pos[0] >= len(lines):
            return None
        return lines[pos[0]][1]

    def _peek_starts_with_dash() -> bool:
        if pos[0] >= len(lines):
            return False
        return lines[pos[0]][2].lstrip().startswith("- ")

    if not lines:
        return {}
    top = parse_block_mapping(lines[0][1])
    if pos[0] != len(lines):
        leftover = lines[pos[0]]
        raise ValueError(f"line {leftover[0]}: unparsed content remains")
    return top


# ---------------------------------------------------------------------------
# Mapping load
# ---------------------------------------------------------------------------


def load_mapping(mapping_path: Path) -> Dict[str, Any]:
    """Read and validate ``personas.yaml``.

    Returns the parsed dict. Raises ``FileNotFoundError`` if the path does
    not exist, ``ValueError`` if the schema_version is missing or wrong, and
    propagates any parse error from the YAML subset parser.
    """
    mapping_path = Path(mapping_path)
    if not mapping_path.exists():
        raise FileNotFoundError(f"mapping file not found: {mapping_path}")
    text = mapping_path.read_text(encoding="utf-8")
    parsed = _parse_yaml(text)
    if not isinstance(parsed, dict):
        raise ValueError("mapping root must be a mapping")
    sv = parsed.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise ValueError(
            f"mapping schema_version must be {SCHEMA_VERSION}, got {sv!r}"
        )
    if not isinstance(parsed.get("archetypes"), dict):
        raise ValueError("mapping must contain a top-level 'archetypes' mapping")
    return parsed


# ---------------------------------------------------------------------------
# Source dispatch
# ---------------------------------------------------------------------------


def _select_archetype_block(
    mapping: Dict[str, Any], archetype: str, industry: Optional[str]
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Look up ``mapping['archetypes'][archetype][industry-key]`` with fallback.

    Tries (in order): ``industry-{industry}`` slug if industry is given,
    then ``default``. Returns ``(block, warnings)``.
    """
    warnings: List[str] = []
    archetypes = mapping.get("archetypes", {})
    if archetype not in archetypes:
        warnings.append(
            f"archetype '{archetype}' not found in mapping; no curated suggestions"
        )
        return None, warnings
    arch_block = archetypes[archetype]
    if not isinstance(arch_block, dict):
        warnings.append(f"archetype '{archetype}' block is not a mapping")
        return None, warnings

    if industry:
        slug = f"industry-{industry.strip().lower().replace(' ', '-')}"
        if slug in arch_block and isinstance(arch_block[slug], dict):
            return arch_block[slug], warnings

    if "default" in arch_block and isinstance(arch_block["default"], dict):
        if industry:
            warnings.append(
                f"industry '{industry}' not in archetype '{archetype}'; "
                "falling back to default block"
            )
        return arch_block["default"], warnings

    warnings.append(
        f"no industry block for archetype '{archetype}' (looked for "
        f"'industry-{industry}' and 'default')"
    )
    return None, warnings


def _fetch_skill_freshness(skill: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch live freshness for a single curated skill entry.

    Source priority:
      - ``github`` field present -> ``sources.github.fetch_repo``
      - ``mcpmarket_search`` field present -> ``sources.mcpmarket.search``
      - else -> curated-only (no live data)
    """
    out = dict(skill)
    out.setdefault("warnings", [])
    out["source_tier"] = "curated"

    gh = skill.get("github")
    if isinstance(gh, str) and "/" in gh:
        owner, _, repo = gh.partition("/")
        try:
            data = github.fetch_repo(owner.strip(), repo.strip())
        except Exception as exc:  # noqa: BLE001 - source must never crash gather
            out["warnings"].append(f"github fetch raised {type(exc).__name__}: {exc}")
            return out
        if "error" in data:
            out["warnings"].append(f"github: {data['error']}")
            return out
        out["stars"] = int(data.get("stars", 0))
        out["forks"] = int(data.get("forks", 0))
        out["last_commit_iso"] = data.get("last_commit_iso")
        out["source_tier"] = "github"
        if data.get("warning_low_quality") or out["stars"] < LOW_QUALITY_STAR_THRESHOLD:
            out["warning_low_quality"] = True
        return out

    market_query = skill.get("mcpmarket_search")
    if isinstance(market_query, str) and market_query.strip():
        try:
            data = mcpmarket.search(market_query.strip())
        except Exception as exc:  # noqa: BLE001
            out["warnings"].append(f"mcpmarket search raised {type(exc).__name__}: {exc}")
            return out
        for w in data.get("warnings", []):
            out["warnings"].append(f"mcpmarket: {w}")
        results = data.get("results", [])
        out["mcpmarket_hits"] = len(results)
        out["source_tier"] = "mcpmarket"
        if not results:
            out["warning_low_quality"] = True
        return out

    return out


def _fetch_mcp_server_freshness(server: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch live freshness for a curated MCP server entry."""
    out = dict(server)
    out.setdefault("warnings", [])
    out["source_tier"] = "curated"

    keywords = server.get("search_keywords") or []
    if not isinstance(keywords, list):
        keywords = [str(keywords)]
    if not keywords:
        return out
    try:
        data = mcp_registry.search([str(k) for k in keywords])
    except Exception as exc:  # noqa: BLE001
        out["warnings"].append(f"mcp-registry search raised {type(exc).__name__}: {exc}")
        return out

    for w in data.get("warnings", []):
        out["warnings"].append(f"mcp-registry: {w}")
    results = data.get("results", [])
    desired_id = server.get("id")
    if desired_id and isinstance(results, list):
        # Filter for entries whose ``id`` (or ``name``) matches.
        matches = [
            r for r in results
            if isinstance(r, dict) and (
                r.get("id") == desired_id or r.get("name") == desired_id
            )
        ]
        out["registry_match"] = bool(matches)
        if matches:
            out["registry_entry"] = matches[0]
            out["source_tier"] = "mcp-registry"
        else:
            out["warnings"].append(
                f"mcp-registry: no entry matched id '{desired_id}'"
            )
            out["warning_low_quality"] = True
    else:
        out["registry_hits"] = len(results) if isinstance(results, list) else 0
        if results:
            out["source_tier"] = "mcp-registry"
    return out


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _rank_key(item: Dict[str, Any]) -> Tuple[int, int, int, str]:
    """Build the deterministic sort key for a suggestion item.

    Tuple shape: ``(tier_priority, -stars, has_warnings, name)``. Lower is
    better; Python sorts tuples lexicographically so we negate stars to
    flip the natural sort.
    """
    tier = item.get("source_tier", "curated")
    tier_priority = _TIER_PRIORITY.get(tier, 99)
    stars = item.get("stars")
    try:
        stars_int = int(stars) if stars is not None else 0
    except (TypeError, ValueError):
        stars_int = 0
    has_warnings = 1 if item.get("warnings") or item.get("warning_low_quality") else 0
    name = str(item.get("name") or item.get("id") or "")
    return (tier_priority, -stars_int, has_warnings, name)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def gather(
    answers: Dict[str, Any],
    mapping_path: Path,
    max_workers: int = 3,
    persona: Optional[Dict[str, Any]] = None,
    *,
    alternatives_yaml_path: Optional[Path] = None,
    telemetry_home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Look up a curated block and enrich it with live freshness data.

    Returns a dict with keys:
      - ``project_templates`` (list[str]): copied from the curated block
      - ``skills`` (list[dict]): each enriched with stars / warnings /
        source_tier, sorted deterministically
      - ``mcp_servers`` (list[dict]): each enriched with registry-match
        info, sorted deterministically
      - ``warnings`` (list[str]): aggregate human-readable warnings

    Never raises. Source-level failures append to ``warnings`` and the
    item still appears in the output (so SKILL.md can render it as a
    curated-only suggestion).
    """
    if not isinstance(answers, dict):
        raise TypeError("answers must be a dict")
    archetype = answers.get("archetype")
    if archetype not in ("job", "personal", "exploring"):
        return {
            "project_templates": [],
            "skills": [],
            "mcp_servers": [],
            "warnings": [
                f"invalid or missing archetype: {archetype!r}; "
                "no suggestions produced"
            ],
        }

    industry = answers.get("industry")
    industry_str = industry if isinstance(industry, str) else None

    try:
        mapping = load_mapping(Path(mapping_path))
    except (FileNotFoundError, ValueError) as exc:
        return {
            "project_templates": [],
            "skills": [],
            "mcp_servers": [],
            "warnings": [f"mapping load failed: {exc}"],
        }

    block, warnings = _select_archetype_block(mapping, archetype, industry_str)
    if block is None:
        return {
            "project_templates": [],
            "skills": [],
            "mcp_servers": [],
            "warnings": warnings,
        }

    project_templates = block.get("project_templates") or []
    if not isinstance(project_templates, list):
        warnings.append("project_templates was not a list; ignoring")
        project_templates = []

    raw_skills = block.get("claude_skills") or []
    raw_servers = block.get("mcp_servers") or []
    if not isinstance(raw_skills, list):
        warnings.append("claude_skills was not a list; ignoring")
        raw_skills = []
    if not isinstance(raw_servers, list):
        warnings.append("mcp_servers was not a list; ignoring")
        raw_servers = []

    # Filter out non-dict items defensively.
    skill_items = [s for s in raw_skills if isinstance(s, dict)]
    server_items = [s for s in raw_servers if isinstance(s, dict)]

    enriched_skills: List[Dict[str, Any]] = []
    enriched_servers: List[Dict[str, Any]] = []

    if skill_items or server_items:
        # Use a single executor for both lists so max_workers caps total
        # parallelism (matches PLAN.md's "ThreadPoolExecutor max_workers=3").
        workers = max(1, int(max_workers))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            skill_futures = [
                ex.submit(_safe_fetch_skill, s) for s in skill_items
            ]
            server_futures = [
                ex.submit(_safe_fetch_server, s) for s in server_items
            ]
            for fut, original in zip(skill_futures, skill_items):
                enriched_skills.append(_collect(fut, original, kind="skill"))
            for fut, original in zip(server_futures, server_items):
                enriched_servers.append(_collect(fut, original, kind="mcp_server"))

    # Aggregate per-item warnings into the top-level warnings list so
    # SKILL.md can show a single banner section.
    for item in enriched_skills + enriched_servers:
        for w in item.get("warnings", []) or []:
            label = item.get("name") or item.get("id") or "<unnamed>"
            warnings.append(f"{label}: {w}")

    enriched_skills.sort(key=_rank_key)
    enriched_servers.sort(key=_rank_key)

    result = {
        "project_templates": list(project_templates),
        "skills": enriched_skills,
        "mcp_servers": enriched_servers,
        "warnings": warnings,
    }

    # Wave 2A: decorate suggestions with alternatives + emit telemetry. This is
    # non-blocking: any failure logs to stderr and the suggestions dict is
    # returned unchanged (the deterministic curated/registry data is the floor).
    return attach_alternatives(
        result,
        persona,
        alternatives_yaml_path=alternatives_yaml_path,
        telemetry_home=telemetry_home,
    )


def _safe_fetch_skill(skill: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _fetch_skill_freshness(skill)
    except Exception as exc:  # noqa: BLE001 - never raise from the executor
        out = dict(skill)
        out.setdefault("warnings", []).append(
            f"unexpected {type(exc).__name__} in skill fetch: {exc}"
        )
        out["source_tier"] = "curated"
        return out


def _safe_fetch_server(server: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _fetch_mcp_server_freshness(server)
    except Exception as exc:  # noqa: BLE001
        out = dict(server)
        out.setdefault("warnings", []).append(
            f"unexpected {type(exc).__name__} in server fetch: {exc}"
        )
        out["source_tier"] = "curated"
        return out


def _collect(
    future: "concurrent.futures.Future[Dict[str, Any]]",
    original: Dict[str, Any],
    kind: str,
) -> Dict[str, Any]:
    """Resolve a fetch future, falling back to the curated entry on failure."""
    try:
        return future.result()
    except Exception as exc:  # noqa: BLE001
        out = dict(original)
        out.setdefault("warnings", []).append(
            f"{kind} fetch raised {type(exc).__name__}: {exc}"
        )
        out["source_tier"] = "curated"
        return out


# ---------------------------------------------------------------------------
# Wave 2A: alternatives + telemetry hook
# ---------------------------------------------------------------------------


def attach_alternatives(
    suggestions: Dict[str, Any],
    persona: Optional[Dict[str, Any]],
    *,
    alternatives_yaml_path: Optional[Path] = None,
    telemetry_home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Decorate each skill / mcp_server entry with up to 2 alternatives.

    Wave 2A render hook. The returned dict is the same shape as ``suggestions``
    plus an ``alternatives`` list on each skill / mcp_server entry. Each
    alternative carries ``{kind, name, url, why, fit_score, why_for_you}``
    (per ``alternatives.pair_with_suggestion``).

    Telemetry: emits one ``suggestion.surfaced`` event with the
    ``count`` of alternatives rendered across all suggestions. Wrapped in
    try/except — telemetry failure NEVER breaks suggestions.

    Non-blocking: if ``mappings/alternatives.yaml`` is missing, malformed, or
    schema-version mismatched, ``alternatives.load_alternatives`` already
    returns ``{}`` with a stderr warning, and each entry will simply have
    an empty ``alternatives: []`` list.
    """
    if not isinstance(suggestions, dict):
        return suggestions  # defensive — should never happen via gather()

    out = dict(suggestions)
    total = 0

    try:
        # Lazy import keeps the suggest module loadable even before Wave 2A
        # files land (e.g. on partial worktree checkouts during testing).
        import alternatives as _alts  # type: ignore  # noqa: PLC0415

        for key in ("skills", "mcp_servers"):
            items = out.get(key)
            if not isinstance(items, list):
                continue
            decorated: List[Dict[str, Any]] = []
            for entry in items:
                if not isinstance(entry, dict):
                    decorated.append(entry)
                    continue
                try:
                    alts_for_entry = _alts.pair_with_suggestion(
                        entry, persona, yaml_path=alternatives_yaml_path
                    )
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(
                        f"ai-quickstart suggest: pair_with_suggestion failed: {exc}\n"
                    )
                    alts_for_entry = []
                new_entry = dict(entry)
                new_entry["alternatives"] = alts_for_entry
                decorated.append(new_entry)
                total += len(alts_for_entry)
            out[key] = decorated
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"ai-quickstart suggest: alternatives attach skipped: {exc}\n"
        )
        # Return the original suggestions unchanged.
        return suggestions

    # Telemetry: one event summarising the alternatives count rendered.
    if total > 0:
        try:
            import telemetry as _telemetry  # type: ignore  # noqa: PLC0415

            home = telemetry_home if telemetry_home is not None else (
                Path.home() / ".ai-quickstart"
            )
            _telemetry.log_event(
                home, "suggestion.surfaced", {"count": int(total)}
            )
        except Exception as exc:  # noqa: BLE001
            # Telemetry failure is intentionally silent at non-debug verbosity.
            sys.stderr.write(
                f"ai-quickstart suggest: telemetry log_event skipped: {exc}\n"
            )

    return out


# ---------------------------------------------------------------------------
# User edits
# ---------------------------------------------------------------------------


def apply_user_edits(
    suggestions: Dict[str, Any],
    accepted: Optional[List[str]] = None,
    rejected: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Filter suggestions by user accept/reject choices.

    ``accepted`` and ``rejected`` are lists of identifiers. An item matches
    if its ``name`` or ``id`` field equals one of the listed strings.

    Semantics:
      * If ``accepted`` is non-empty, ONLY items whose identifier is in that
        list are kept (allow-list mode).
      * Otherwise, items whose identifier is in ``rejected`` are dropped.
      * ``project_templates`` is filtered by string equality against the
        same ``accepted`` / ``rejected`` lists.
      * ``warnings`` is preserved verbatim.

    Returns a new dict; the input is not mutated.
    """
    accepted = accepted or []
    rejected = rejected or []
    accepted_set = set(accepted)
    rejected_set = set(rejected)

    def _identify(item: Dict[str, Any]) -> str:
        return str(item.get("name") or item.get("id") or "")

    def _filter_list(items: List[Any], is_dict: bool) -> List[Any]:
        out: List[Any] = []
        for item in items:
            ident = _identify(item) if is_dict else str(item)
            if accepted_set:
                if ident in accepted_set:
                    out.append(item)
            else:
                if ident not in rejected_set:
                    out.append(item)
        return out

    return {
        "project_templates": _filter_list(
            list(suggestions.get("project_templates", [])), is_dict=False
        ),
        "skills": _filter_list(
            list(suggestions.get("skills", [])), is_dict=True
        ),
        "mcp_servers": _filter_list(
            list(suggestions.get("mcp_servers", [])), is_dict=True
        ),
        "warnings": list(suggestions.get("warnings", [])),
    }


__all__ = [
    "load_mapping",
    "gather",
    "attach_alternatives",
    "apply_user_edits",
    "SCHEMA_VERSION",
    "LOW_QUALITY_STAR_THRESHOLD",
]
