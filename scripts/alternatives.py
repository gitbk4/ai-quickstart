"""ai-quickstart Wave 2A: alternatives engine.

Public surface (see v2-cathedral.md "Scope Decisions" Proposal 2 row):

  * ``load_alternatives(yaml_path)`` — parse + cache mappings/alternatives.yaml.
  * ``pair_with_suggestion(suggestion, persona)`` — produce 1-2 alternatives
    paired with a suggestion, each annotated with fit_score + why_for_you.
  * ``compute_fit_score(suggestion_or_alt, persona)`` — deterministic 0.0-1.0
    Jaccard-with-archetype/industry-bonus score. NEVER calls an LLM.
  * ``render_why_for_you(alt, suggestion, persona)`` — single-line, deterministic
    reason string referencing a persona paragraph by ID when possible.
  * ``stars_inline(score)`` — temporary local 1-5 star renderer. Wave 2B's
    ``scripts/badges.py`` will ship ``render_fit_score_stars`` and a follow-up
    commit will swap this import-then-call for the shared helper.

Performance budget (v2-cathedral.md "Performance Budgets" table):

  * ``alternatives.yaml`` parse: <50ms cold, cached after first load.

Stdlib only. Reuses the flat-YAML parser from ``suggest.py``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Sibling-import pattern matching suggest.py / heal.py.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

# We deliberately reuse suggest.py's parser to keep parser-bug fixes single-source.
import suggest as _suggest  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALTERNATIVES_SCHEMA_VERSION = 1

ALLOWED_KINDS = ("saas", "oss", "claude_skill", "mcp_server", "agent_platform")

# Default ~/.ai-quickstart/ — only used if a caller wants a hint; the loader
# itself takes an explicit path to keep tests deterministic.
HOME_DEFAULT = Path.home() / ".ai-quickstart"

# Default mapping path lives in the repo, not in HOME_DEFAULT — alternatives
# data is curated and shipped with the skill.
_DEFAULT_YAML_PATH = (
    Path(__file__).resolve().parents[1] / "mappings" / "alternatives.yaml"
)

# Module-level memoization: parse once per process per yaml path.
# Key is the resolved absolute path string; value is the parsed dict.
_CACHE: Dict[str, Dict[str, Any]] = {}

# Length cap for render_why_for_you output (per the public-surface spec).
_WHY_FOR_YOU_MAX_CHARS = 140

# Persona paragraph ID pattern: "p:NNN" with at least 1 digit.
_PARAGRAPH_ID_RE = re.compile(r"\bp:\d+\b")


# ---------------------------------------------------------------------------
# load_alternatives
# ---------------------------------------------------------------------------


def load_alternatives(yaml_path: Optional[Path] = None) -> Dict[str, Any]:
    """Parse mappings/alternatives.yaml and return its ``alternatives`` dict.

    Behavior:
      * Cached at module level after first successful parse — second and
        subsequent calls with the same resolved path are O(1).
      * Malformed YAML, missing file, or schema-version mismatch -> return
        ``{}`` and emit a one-line stderr warning. NEVER raises.
      * The returned dict is the ``alternatives`` sub-mapping (tag -> kinds);
        callers don't see the schema_version wrapper.

    Returns: ``{tag: {kind: [{name,url,why}, ...]}}``. Empty dict on failure.
    """
    path = Path(yaml_path) if yaml_path is not None else _DEFAULT_YAML_PATH
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)

    cached = _CACHE.get(resolved)
    if cached is not None:
        return cached

    if not path.exists():
        sys.stderr.write(
            f"ai-quickstart alternatives: file not found at {path}; skipping\n"
        )
        _CACHE[resolved] = {}
        return _CACHE[resolved]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"ai-quickstart alternatives: read failed at {path}: {exc}\n"
        )
        _CACHE[resolved] = {}
        return _CACHE[resolved]

    try:
        parsed = _suggest._parse_yaml(text)  # noqa: SLF001 — intentional reuse
    except Exception as exc:  # noqa: BLE001 — never crash callers
        sys.stderr.write(
            f"ai-quickstart alternatives: parse failed at {path}: {exc}\n"
        )
        _CACHE[resolved] = {}
        return _CACHE[resolved]

    if not isinstance(parsed, dict):
        sys.stderr.write(
            f"ai-quickstart alternatives: root is not a mapping at {path}\n"
        )
        _CACHE[resolved] = {}
        return _CACHE[resolved]

    sv = parsed.get("schema_version")
    if sv != ALTERNATIVES_SCHEMA_VERSION:
        sys.stderr.write(
            "ai-quickstart alternatives: schema_version mismatch "
            f"(got {sv!r}, expected {ALTERNATIVES_SCHEMA_VERSION}); skipping\n"
        )
        _CACHE[resolved] = {}
        return _CACHE[resolved]

    alts = parsed.get("alternatives")
    if not isinstance(alts, dict):
        sys.stderr.write(
            f"ai-quickstart alternatives: 'alternatives' missing or not a mapping\n"
        )
        _CACHE[resolved] = {}
        return _CACHE[resolved]

    _CACHE[resolved] = alts
    return alts


def _clear_cache_for_tests() -> None:
    """Test-only helper: clear the module-level memoization."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Tag extraction — lightweight, used by both pair_with_suggestion and
# compute_fit_score.
# ---------------------------------------------------------------------------


def _suggestion_tag(suggestion: Dict[str, Any]) -> Optional[str]:
    """Return the lookup tag for a suggestion, or None.

    Lookup precedence:
      * explicit ``category`` field (forward-compat for v2 suggestions)
      * else ``name`` field (current personas.yaml entries are keyed by name)
      * else ``id`` field (for mcp_server-shaped entries)

    Lowercases + trims for tag-table robustness.
    """
    if not isinstance(suggestion, dict):
        return None
    for key in ("category", "name", "id"):
        v = suggestion.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _suggestion_tag_set(suggestion: Dict[str, Any]) -> set:
    """Build the tag set used by Jaccard similarity.

    Includes (when present): ``category``, ``name``, ``archetype``,
    ``industry``, plus any explicit string ``tags`` list. Skips empty values.
    """
    out: set = set()
    if not isinstance(suggestion, dict):
        return out
    for key in ("category", "name", "id", "archetype", "industry"):
        v = suggestion.get(key)
        if isinstance(v, str) and v.strip():
            out.add(v.strip().lower())
    raw_tags = suggestion.get("tags")
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t.strip():
                out.add(t.strip().lower())
    return out


def _persona_tag_set(persona: Dict[str, Any]) -> set:
    """Build the tag set from persona.json's structured section.

    Reads:
      * ``structured.archetype``, ``structured.industry``, ``structured.role``,
        ``structured.project_style``
      * each ``top_projects[].name`` (lowercased)
    """
    out: set = set()
    if not isinstance(persona, dict):
        return out
    structured = persona.get("structured")
    if not isinstance(structured, dict):
        return out
    for key in ("archetype", "industry", "role", "project_style"):
        v = structured.get(key)
        if isinstance(v, str) and v.strip():
            out.add(v.strip().lower())
    top = structured.get("top_projects")
    if isinstance(top, list):
        for entry in top:
            if isinstance(entry, dict):
                n = entry.get("name")
                if isinstance(n, str) and n.strip():
                    out.add(n.strip().lower())
    return out


def _persona_has_structured(persona: Optional[Dict[str, Any]]) -> bool:
    """True if persona has at least one structured.* field populated."""
    if not isinstance(persona, dict):
        return False
    structured = persona.get("structured")
    if not isinstance(structured, dict):
        return False
    for key in ("role", "archetype", "industry", "skill_tolerance", "project_style"):
        v = structured.get(key)
        if isinstance(v, str) and v.strip():
            return True
    top = structured.get("top_projects")
    if isinstance(top, list) and top:
        return True
    return False


# ---------------------------------------------------------------------------
# compute_fit_score
# ---------------------------------------------------------------------------


def compute_fit_score(
    suggestion_or_alt: Dict[str, Any], persona: Dict[str, Any]
) -> float:
    """Deterministic 0.0-1.0 fit score. NO LLM call.

    Algorithm (per v2-cathedral.md "Defined Terms" -> Fit score):

      base = Jaccard(suggestion_tags, persona_tags)
      + bonus_archetype if archetypes match exactly  (+0.25)
      + bonus_industry if industries match exactly   (+0.20)
      - penalty_skill_tolerance if mismatched         (-0.15)

      result = clamp(base + bonuses - penalties, 0.0, 1.0)

    Weight rationale: archetype is the strongest discriminator in v1's
    personas.yaml curation (each archetype has its own block); industry is
    the next tier; skill_tolerance is a UX-fit signal, not a relevance signal,
    so its penalty is smaller. Tuned so an exact archetype+industry match
    clears 0.7 (>3.5 stars in stars_inline) for any non-empty Jaccard overlap.

    Edge cases:
      * persona lacks structured fields entirely -> return 0.5 (neutral)
        (per the spec "incomplete persona" hint default).
      * suggestion_or_alt has no recognisable tag fields -> 0.5 fallback.

    Determinism: identical inputs always produce identical output. Pure
    arithmetic over set operations and dict reads. No randomness, no
    timestamps, no I/O.
    """
    if not _persona_has_structured(persona):
        return 0.5

    sug_tags = _suggestion_tag_set(suggestion_or_alt)
    per_tags = _persona_tag_set(persona)

    if not sug_tags or not per_tags:
        return 0.5

    inter = sug_tags & per_tags
    union = sug_tags | per_tags
    jaccard = (len(inter) / len(union)) if union else 0.0

    score = jaccard

    # Archetype exact-match bonus.
    structured = persona.get("structured", {}) if isinstance(persona, dict) else {}
    sug_arch = _opt_str(suggestion_or_alt.get("archetype"))
    per_arch = _opt_str(structured.get("archetype"))
    if sug_arch and per_arch and sug_arch == per_arch:
        score += 0.25

    # Industry exact-match bonus.
    sug_ind = _opt_str(suggestion_or_alt.get("industry"))
    per_ind = _opt_str(structured.get("industry"))
    if sug_ind and per_ind and sug_ind == per_ind:
        score += 0.20

    # Skill-tolerance mismatch penalty.
    sug_tol = _opt_str(suggestion_or_alt.get("skill_tolerance"))
    per_tol = _opt_str(structured.get("skill_tolerance"))
    if sug_tol and per_tol and sug_tol != per_tol:
        score -= 0.15

    if score < 0.0:
        score = 0.0
    if score > 1.0:
        score = 1.0
    return float(score)


def _opt_str(v: Any) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip().lower()
    return None


# ---------------------------------------------------------------------------
# render_why_for_you
# ---------------------------------------------------------------------------


def render_why_for_you(
    alt: Dict[str, Any],
    suggestion: Dict[str, Any],
    persona: Optional[Dict[str, Any]],
) -> str:
    """Generate a deterministic single-line "why this for you" string.

    Strategy (no LLM call):
      1. If ``persona`` is None or has no paragraphs, return a generic reason.
      2. If there's a paragraph whose text mentions the suggestion's tag,
         return ``"Suggested because your persona p:NNN says ..."``.
      3. Else, fall back to ``"Matches your archetype/industry/style"``
         (whichever fields are present).
      4. Hard-cap output at ``_WHY_FOR_YOU_MAX_CHARS`` chars.
    """
    alt_why = ""
    if isinstance(alt, dict):
        why = alt.get("why")
        if isinstance(why, str) and why.strip():
            alt_why = why.strip()

    # Persona-less: generic reason, optionally seasoned with the alt's `why`.
    if not isinstance(persona, dict):
        msg = "General fit for the suggested category"
        if alt_why:
            msg = f"{msg}: {alt_why}"
        return _truncate(msg, _WHY_FOR_YOU_MAX_CHARS)

    paragraphs = persona.get("paragraphs")
    structured = persona.get("structured") if isinstance(persona, dict) else None
    if not isinstance(structured, dict):
        structured = {}

    tag = _suggestion_tag(suggestion) or _suggestion_tag(alt) or ""

    # Strategy 2: paragraph reference if any paragraph text mentions the tag.
    if isinstance(paragraphs, list) and tag:
        # Lowercase compare; we want substring tolerance ("research" matches
        # "research-assistant" tag).
        tag_token = tag.replace("-", " ")
        for entry in paragraphs:
            if not isinstance(entry, dict):
                continue
            pid = entry.get("id")
            text = entry.get("text")
            if not isinstance(pid, str) or not isinstance(text, str):
                continue
            if not _PARAGRAPH_ID_RE.match(pid):
                continue
            lower = text.lower()
            if tag in lower or tag_token in lower:
                snippet = _short_quote(text)
                msg = f"Suggested because your persona {pid} says \"{snippet}\""
                return _truncate(msg, _WHY_FOR_YOU_MAX_CHARS)

    # Strategy 3: archetype/industry/style fallback.
    parts: List[str] = []
    arch = structured.get("archetype")
    ind = structured.get("industry")
    style = structured.get("project_style")
    if isinstance(arch, str) and arch.strip():
        parts.append(arch.strip())
    if isinstance(ind, str) and ind.strip():
        parts.append(ind.strip())
    if isinstance(style, str) and style.strip():
        parts.append(style.strip())

    if parts:
        msg = "Matches your " + " / ".join(parts[:3])
    else:
        msg = "General fit for the suggested category"
    if alt_why and len(msg) + len(alt_why) + 2 < _WHY_FOR_YOU_MAX_CHARS:
        msg = f"{msg}: {alt_why}"
    return _truncate(msg, _WHY_FOR_YOU_MAX_CHARS)


def _short_quote(text: str, max_chars: int = 60) -> str:
    """Single-line excerpt suitable for embedding in a why-for-you string."""
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "…"


def _truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    if max_chars <= 1:
        return s[:max_chars]
    return s[: max_chars - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# pair_with_suggestion
# ---------------------------------------------------------------------------


def pair_with_suggestion(
    suggestion: Dict[str, Any],
    persona: Optional[Dict[str, Any]],
    *,
    yaml_path: Optional[Path] = None,
    max_alternatives: int = 2,
) -> List[Dict[str, Any]]:
    """Return 1-2 alternatives paired with the given suggestion.

    Returned shape per alt::

        {kind: str, name: str, url: str, why: str,
         fit_score: float | None, why_for_you: str}

    Behavior:
      * If suggestion has no ``category`` / ``name`` / ``id`` tag matchable
        against the alternatives table -> return [].
      * If persona is None -> alternatives are still returned, but
        ``fit_score`` is None and ``why_for_you`` is the generic fallback.
      * Selection is deterministic: kinds are checked in
        ``ALLOWED_KINDS`` order; within each kind, the first entry is taken.
        We pair at most ``max_alternatives`` (default 2) total — preferring
        SaaS+OSS contrast first, then claude_skill / mcp_server / agent_platform.

    Never raises. Catches all exceptions internally and returns [] on hard
    failure (so suggest.py's render path stays alive).
    """
    try:
        if not isinstance(suggestion, dict):
            return []
        tag = _suggestion_tag(suggestion)
        if not tag:
            return []

        table = load_alternatives(yaml_path)
        if not isinstance(table, dict):
            return []

        kinds = table.get(tag)
        if not isinstance(kinds, dict):
            return []

        out: List[Dict[str, Any]] = []
        cap = max(1, int(max_alternatives))

        for kind in ALLOWED_KINDS:
            if len(out) >= cap:
                break
            entries = kinds.get(kind)
            if not isinstance(entries, list) or not entries:
                continue
            entry = entries[0]
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            url = entry.get("url")
            why = entry.get("why")
            if not (isinstance(name, str) and isinstance(url, str)):
                continue
            alt: Dict[str, Any] = {
                "kind": kind,
                "name": name,
                "url": url,
                "why": why if isinstance(why, str) else "",
            }
            # Inherit the suggestion's archetype/industry/skill_tolerance/tags
            # context for fit-score computation: the alternative is being
            # evaluated AS a stand-in for the suggestion, so it inherits the
            # suggestion's relevance signals. The alt's own name/kind add
            # their own surface-area to the Jaccard set.
            scoring_view: Dict[str, Any] = {
                "kind": kind,
                "name": name,
                "category": _suggestion_tag(suggestion),
                "archetype": suggestion.get("archetype"),
                "industry": suggestion.get("industry"),
                "skill_tolerance": suggestion.get("skill_tolerance"),
                "tags": list(suggestion.get("tags", []))
                if isinstance(suggestion.get("tags"), list)
                else [],
            }
            if persona is not None:
                alt["fit_score"] = compute_fit_score(scoring_view, persona)
                alt["why_for_you"] = render_why_for_you(alt, suggestion, persona)
            else:
                alt["fit_score"] = None
                alt["why_for_you"] = render_why_for_you(alt, suggestion, None)
            out.append(alt)
        return out
    except Exception as exc:  # noqa: BLE001 — never break the render path
        sys.stderr.write(
            f"ai-quickstart alternatives: pair_with_suggestion failed: {exc}\n"
        )
        return []


# ---------------------------------------------------------------------------
# stars_inline (LOCAL placeholder — Wave 2B's badges.py replaces this)
# ---------------------------------------------------------------------------


def stars_inline(score: Optional[float]) -> str:
    """Render 1-5 ASCII stars from a 0.0-1.0 fit score.

    Delegates to ``badges.render_fit_score_stars_terminal`` for the actual
    bucket math so terminal rendering stays consistent across surfaces.
    Preserves the persona-less ``None -> "?????"`` placeholder behavior
    that ``alternatives.pair_with_suggestion`` relies on (badges' renderer
    treats None as 0.0 and shows a 1-star bar, which would be misleading
    for a "no persona, can't compute" state).
    """
    if score is None:
        return "?????"
    try:
        f = float(score)
    except (TypeError, ValueError):
        return "?????"
    if f != f:  # NaN
        return "?????"
    import badges  # type: ignore  # noqa: WPS433  # local import; sys.path set at module top
    return badges.render_fit_score_stars_terminal(f)


__all__ = [
    "ALTERNATIVES_SCHEMA_VERSION",
    "ALLOWED_KINDS",
    "HOME_DEFAULT",
    "load_alternatives",
    "pair_with_suggestion",
    "compute_fit_score",
    "render_why_for_you",
    "stars_inline",
]
