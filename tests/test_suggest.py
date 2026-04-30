"""Tests for scripts/suggest.py.

Covers:
  * load_mapping happy path returns the parsed dict; missing schema_version
    or wrong value raises ValueError; missing archetypes raises ValueError;
    malformed YAML raises ValueError; nonexistent file raises FileNotFoundError.
  * gather happy path with all three sources mocked: skills get GitHub
    enrichment, mcp_servers get registry enrichment, ranking is
    deterministic across repeated runs of the same input.
  * gather where one source raises -> per-item warning recorded, others
    succeed and remain in the output.
  * gather with low-stars from GitHub -> warning_low_quality flag is set on
    the result.
  * gather with archetype not present in the mapping -> empty results plus
    a warning.
  * gather with invalid archetype short-circuits before touching disk.
  * apply_user_edits filters by accept (allow-list) or reject lists, and
    preserves warnings verbatim.

All tests stub the three source modules via ``unittest.mock.patch`` so no
network or subprocess invocation actually happens.

Run with: ``python3 -m pytest tests/test_suggest.py -v``
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import suggest  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mapping_path(tmp_path: Path) -> Path:
    """Write a minimal valid personas.yaml and return its path."""
    text = """schema_version: 1
archetypes:
  job:
    industry-marketing:
      project_templates: [content-research, audience-personas]
      claude_skills:
        - name: research-assistant
          description: "Research and summarize"
          github: anthropics/anthropic-cookbook
        - name: low-quality-skill
          description: "Star count below threshold"
          github: foo/low-stars
      mcp_servers:
        - id: brave-search
          description: "Web search"
          search_keywords: [search, brave]
        - id: missing-server
          description: "Not found in registry"
          search_keywords: [missing]
    industry-engineering:
      project_templates: [code-review-bot]
      claude_skills:
        - name: claude-code-reference
          description: "Claude Code patterns"
          github: anthropics/claude-code
      mcp_servers:
        - id: github
          description: "GitHub access"
          search_keywords: [github]
  personal:
    default:
      project_templates: [journaling-coach]
      claude_skills:
        - name: courses
          description: "Anthropic courses"
          github: anthropics/courses
      mcp_servers: []
  exploring:
    default:
      project_templates: [first-skill]
      claude_skills:
        - name: marketplace-only
          description: "Skill discovered via mcpmarket scrape"
          mcpmarket_search: "hello world"
      mcp_servers: []
"""
    p = tmp_path / "personas.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Source stubs
# ---------------------------------------------------------------------------


def _gh_high_stars(owner: str, repo: str, force_refresh: bool = False) -> Dict[str, Any]:
    return {
        "stars": 5000,
        "forks": 200,
        "contributors": None,
        "last_commit_iso": "2026-04-20T00:00:00Z",
        "watchers": 100,
        "warning_low_quality": False,
        "source_tier": "gh-cli",
    }


def _gh_low_stars(owner: str, repo: str, force_refresh: bool = False) -> Dict[str, Any]:
    return {
        "stars": 12,
        "forks": 1,
        "contributors": None,
        "last_commit_iso": "2026-04-20T00:00:00Z",
        "watchers": 1,
        "warning_low_quality": True,
        "source_tier": "unauth",
    }


def _gh_dispatch(owner: str, repo: str, force_refresh: bool = False) -> Dict[str, Any]:
    """Star count varies by repo so we can exercise the low-quality branch."""
    if "low-stars" in repo:
        return _gh_low_stars(owner, repo, force_refresh)
    return _gh_high_stars(owner, repo, force_refresh)


def _registry_brave(keywords: List[str], limit: int = 20, force_refresh: bool = False):
    if "brave" in keywords or "search" in keywords:
        return {
            "results": [{"id": "brave-search", "title": "Brave Search MCP"}],
            "source": "mcp-registry",
            "warnings": [],
        }
    return {"results": [], "source": "mcp-registry", "warnings": []}


def _registry_empty(keywords: List[str], limit: int = 20, force_refresh: bool = False):
    return {"results": [], "source": "mcp-registry", "warnings": []}


def _market_hello(query: str, limit: int = 20, force_refresh: bool = False):
    if "hello" in (query or "").lower():
        return {
            "results": [{"title": "hello-world MCP", "url": "https://example.com"}],
            "source": "mcpmarket",
            "warnings": [],
            "source_tier": "mcpmarket-scrape",
        }
    return {
        "results": [],
        "source": "mcpmarket",
        "warnings": [],
        "source_tier": "mcpmarket-scrape",
    }


# ---------------------------------------------------------------------------
# load_mapping
# ---------------------------------------------------------------------------


def test_load_mapping_happy_path(mapping_path):
    parsed = suggest.load_mapping(mapping_path)
    assert parsed["schema_version"] == 1
    assert "archetypes" in parsed
    assert "job" in parsed["archetypes"]
    assert "industry-marketing" in parsed["archetypes"]["job"]


def test_load_mapping_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        suggest.load_mapping(tmp_path / "does-not-exist.yaml")


def test_load_mapping_missing_schema_version_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("archetypes:\n  job:\n    default:\n      project_templates: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        suggest.load_mapping(p)


def test_load_mapping_wrong_schema_version_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: 99\narchetypes:\n  job:\n    default:\n      project_templates: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        suggest.load_mapping(p)


def test_load_mapping_missing_archetypes_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        suggest.load_mapping(p)


def test_load_mapping_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    # Top-level key at depth 0 followed by an unexpected indented line at
    # depth 2 with no parent — strict parser refuses this shape.
    p.write_text(
        "schema_version: 1\n  stray_indent: oops\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        suggest.load_mapping(p)


def test_load_mapping_missing_colon_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    # A line in mapping context that lacks the required ':' separator.
    p.write_text("schema_version: 1\nbroken line without colon\n", encoding="utf-8")
    with pytest.raises(ValueError):
        suggest.load_mapping(p)


def test_real_mapping_file_loads(tmp_path):
    """The committed mappings/personas.yaml must parse cleanly."""
    real = REPO_ROOT / "mappings" / "personas.yaml"
    parsed = suggest.load_mapping(real)
    assert parsed["schema_version"] == 1
    archetypes = parsed["archetypes"]
    # All three required archetypes appear.
    assert {"job", "personal", "exploring"} <= set(archetypes.keys())


# ---------------------------------------------------------------------------
# gather - happy path with all three sources
# ---------------------------------------------------------------------------


def test_gather_happy_path_all_sources(mapping_path):
    answers = {"archetype": "job", "industry": "marketing"}
    with patch.object(suggest.github, "fetch_repo", side_effect=_gh_dispatch), \
         patch.object(suggest.mcp_registry, "search", side_effect=_registry_brave), \
         patch.object(suggest.mcpmarket, "search", side_effect=_market_hello):
        result = suggest.gather(answers, mapping_path)

    assert result["project_templates"] == ["content-research", "audience-personas"]
    skill_names = [s["name"] for s in result["skills"]]
    assert "research-assistant" in skill_names
    assert "low-quality-skill" in skill_names

    research = next(s for s in result["skills"] if s["name"] == "research-assistant")
    assert research["stars"] == 5000
    assert research["source_tier"] == "github"

    low_q = next(s for s in result["skills"] if s["name"] == "low-quality-skill")
    assert low_q.get("warning_low_quality") is True

    server_ids = [s["id"] for s in result["mcp_servers"]]
    assert "brave-search" in server_ids
    brave = next(s for s in result["mcp_servers"] if s["id"] == "brave-search")
    assert brave["source_tier"] == "mcp-registry"
    assert brave["registry_match"] is True


def test_gather_ranking_is_deterministic(mapping_path):
    """Same inputs produce the same ordering across multiple calls."""
    answers = {"archetype": "job", "industry": "marketing"}

    def run_once():
        with patch.object(suggest.github, "fetch_repo", side_effect=_gh_dispatch), \
             patch.object(suggest.mcp_registry, "search", side_effect=_registry_brave), \
             patch.object(suggest.mcpmarket, "search", side_effect=_market_hello):
            return suggest.gather(answers, mapping_path)

    a = run_once()
    b = run_once()
    c = run_once()

    skills_a = [s["name"] for s in a["skills"]]
    skills_b = [s["name"] for s in b["skills"]]
    skills_c = [s["name"] for s in c["skills"]]
    assert skills_a == skills_b == skills_c

    servers_a = [s["id"] for s in a["mcp_servers"]]
    servers_b = [s["id"] for s in b["mcp_servers"]]
    assert servers_a == servers_b

    # High-stars / source_tier=github sorts ahead of low-quality-flagged entries.
    assert skills_a[0] == "research-assistant"


def test_gather_ranking_high_stars_first(mapping_path):
    """Within the same source tier, more stars ranks earlier."""
    def gh_two_repos(owner, repo, force_refresh=False):
        if repo == "anthropic-cookbook":
            return {**_gh_high_stars(owner, repo), "stars": 100}
        if repo == "low-stars":
            return {**_gh_high_stars(owner, repo), "stars": 9999}
        return _gh_high_stars(owner, repo)

    answers = {"archetype": "job", "industry": "marketing"}
    with patch.object(suggest.github, "fetch_repo", side_effect=gh_two_repos), \
         patch.object(suggest.mcp_registry, "search", side_effect=_registry_empty), \
         patch.object(suggest.mcpmarket, "search", side_effect=_market_hello):
        result = suggest.gather(answers, mapping_path)

    names = [s["name"] for s in result["skills"]]
    # low-quality-skill has 9999 stars in this stub, so it ranks first.
    assert names[0] == "low-quality-skill"


# ---------------------------------------------------------------------------
# gather - source-level failures
# ---------------------------------------------------------------------------


def test_gather_one_source_raises_others_succeed(mapping_path):
    def gh_explodes(owner, repo, force_refresh=False):
        raise RuntimeError("boom")

    answers = {"archetype": "job", "industry": "marketing"}
    with patch.object(suggest.github, "fetch_repo", side_effect=gh_explodes), \
         patch.object(suggest.mcp_registry, "search", side_effect=_registry_brave), \
         patch.object(suggest.mcpmarket, "search", side_effect=_market_hello):
        result = suggest.gather(answers, mapping_path)

    # Skills still appear, but with curated source_tier and a warning each.
    assert len(result["skills"]) == 2
    for s in result["skills"]:
        assert s["source_tier"] == "curated"
        assert any("github" in w.lower() for w in s.get("warnings", []))

    # mcp_servers should still be enriched normally.
    server_ids = {s["id"] for s in result["mcp_servers"]}
    assert "brave-search" in server_ids

    # Warnings aggregated at top level.
    assert any("research-assistant" in w for w in result["warnings"])


def test_gather_github_returns_error_dict(mapping_path):
    """github.fetch_repo error dict (not exception) is also surfaced gracefully."""
    def gh_errors(owner, repo, force_refresh=False):
        return {"error": "404 not found", "error_kind": "not_found"}

    answers = {"archetype": "job", "industry": "marketing"}
    with patch.object(suggest.github, "fetch_repo", side_effect=gh_errors), \
         patch.object(suggest.mcp_registry, "search", side_effect=_registry_empty), \
         patch.object(suggest.mcpmarket, "search", side_effect=_market_hello):
        result = suggest.gather(answers, mapping_path)

    for s in result["skills"]:
        assert s["source_tier"] == "curated"
        assert any("github" in w.lower() for w in s.get("warnings", []))


def test_gather_mcp_registry_no_match_flags_low_quality(mapping_path):
    """Server whose id is not in the registry results gets warning_low_quality."""
    def reg_partial(keywords, limit=20, force_refresh=False):
        # Returns brave-search but never missing-server.
        if "brave" in keywords or "search" in keywords:
            return {
                "results": [{"id": "brave-search"}],
                "source": "mcp-registry",
                "warnings": [],
            }
        return {"results": [], "source": "mcp-registry", "warnings": []}

    answers = {"archetype": "job", "industry": "marketing"}
    with patch.object(suggest.github, "fetch_repo", side_effect=_gh_dispatch), \
         patch.object(suggest.mcp_registry, "search", side_effect=reg_partial), \
         patch.object(suggest.mcpmarket, "search", side_effect=_market_hello):
        result = suggest.gather(answers, mapping_path)

    by_id = {s["id"]: s for s in result["mcp_servers"]}
    assert by_id["missing-server"].get("warning_low_quality") is True
    assert by_id["brave-search"].get("registry_match") is True


def test_gather_mcpmarket_only_skill_no_results_flags_low_quality(mapping_path):
    """An mcpmarket-only skill with zero results gets warning_low_quality."""
    def market_empty(query, limit=20, force_refresh=False):
        return {
            "results": [],
            "source": "mcpmarket",
            "warnings": [],
            "source_tier": "mcpmarket-scrape",
        }

    answers = {"archetype": "exploring"}  # exploring/default has marketplace-only
    with patch.object(suggest.github, "fetch_repo", side_effect=_gh_dispatch), \
         patch.object(suggest.mcp_registry, "search", side_effect=_registry_empty), \
         patch.object(suggest.mcpmarket, "search", side_effect=market_empty):
        result = suggest.gather(answers, mapping_path)

    assert len(result["skills"]) == 1
    s = result["skills"][0]
    assert s["name"] == "marketplace-only"
    assert s.get("warning_low_quality") is True


# ---------------------------------------------------------------------------
# gather - mapping miss / invalid input
# ---------------------------------------------------------------------------


def test_gather_archetype_missing_in_mapping(tmp_path):
    """When the mapping has no entry for the archetype, return empty + warning."""
    p = tmp_path / "tiny.yaml"
    p.write_text(
        "schema_version: 1\narchetypes:\n  job:\n    default:\n      project_templates: []\n",
        encoding="utf-8",
    )
    answers = {"archetype": "personal"}
    with patch.object(suggest.github, "fetch_repo", side_effect=_gh_dispatch), \
         patch.object(suggest.mcp_registry, "search", side_effect=_registry_empty), \
         patch.object(suggest.mcpmarket, "search", side_effect=_market_hello):
        result = suggest.gather(answers, p)

    assert result["skills"] == []
    assert result["mcp_servers"] == []
    assert result["project_templates"] == []
    assert any("personal" in w for w in result["warnings"])


def test_gather_industry_falls_back_to_default(tmp_path):
    p = tmp_path / "tiny.yaml"
    p.write_text(
        "schema_version: 1\n"
        "archetypes:\n"
        "  job:\n"
        "    default:\n"
        "      project_templates: [fallback-template]\n"
        "      claude_skills: []\n"
        "      mcp_servers: []\n",
        encoding="utf-8",
    )
    answers = {"archetype": "job", "industry": "obscure-industry"}
    with patch.object(suggest.github, "fetch_repo", side_effect=_gh_dispatch), \
         patch.object(suggest.mcp_registry, "search", side_effect=_registry_empty), \
         patch.object(suggest.mcpmarket, "search", side_effect=_market_hello):
        result = suggest.gather(answers, p)
    assert result["project_templates"] == ["fallback-template"]
    assert any("obscure-industry" in w for w in result["warnings"])


def test_gather_invalid_archetype_returns_empty(mapping_path):
    answers = {"archetype": "founder"}  # not in the closed enum
    result = suggest.gather(answers, mapping_path)
    assert result["skills"] == []
    assert result["mcp_servers"] == []
    assert result["project_templates"] == []
    assert any("founder" in w for w in result["warnings"])


def test_gather_missing_archetype_returns_empty(mapping_path):
    """Answers with no archetype at all is also a hard short-circuit."""
    result = suggest.gather({}, mapping_path)
    assert result["skills"] == []
    assert result["warnings"]


def test_gather_rejects_non_dict_answers(mapping_path):
    with pytest.raises(TypeError):
        suggest.gather("not a dict", mapping_path)  # type: ignore[arg-type]


def test_gather_missing_mapping_file_returns_warning(tmp_path):
    answers = {"archetype": "job"}
    result = suggest.gather(answers, tmp_path / "missing.yaml")
    assert result["skills"] == []
    assert any("mapping load failed" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# apply_user_edits
# ---------------------------------------------------------------------------


def test_apply_user_edits_accept_filters_to_allow_list():
    suggestions = {
        "project_templates": ["alpha", "beta", "gamma"],
        "skills": [{"name": "s1"}, {"name": "s2"}, {"name": "s3"}],
        "mcp_servers": [{"id": "m1"}, {"id": "m2"}],
        "warnings": ["w1"],
    }
    out = suggest.apply_user_edits(suggestions, accepted=["alpha", "s1", "m2"])
    assert out["project_templates"] == ["alpha"]
    assert [s["name"] for s in out["skills"]] == ["s1"]
    assert [s["id"] for s in out["mcp_servers"]] == ["m2"]
    # Warnings preserved verbatim.
    assert out["warnings"] == ["w1"]


def test_apply_user_edits_reject_filters_out_named_items():
    suggestions = {
        "project_templates": ["alpha", "beta", "gamma"],
        "skills": [{"name": "s1"}, {"name": "s2"}],
        "mcp_servers": [{"id": "m1"}, {"id": "m2"}],
        "warnings": [],
    }
    out = suggest.apply_user_edits(suggestions, rejected=["beta", "s2", "m1"])
    assert out["project_templates"] == ["alpha", "gamma"]
    assert [s["name"] for s in out["skills"]] == ["s1"]
    assert [s["id"] for s in out["mcp_servers"]] == ["m2"]


def test_apply_user_edits_accept_overrides_reject():
    """When ``accepted`` is non-empty, ``rejected`` is ignored."""
    suggestions = {
        "project_templates": ["alpha", "beta"],
        "skills": [{"name": "s1"}, {"name": "s2"}],
        "mcp_servers": [],
        "warnings": [],
    }
    out = suggest.apply_user_edits(
        suggestions, accepted=["alpha", "s1"], rejected=["alpha", "s1"]
    )
    assert out["project_templates"] == ["alpha"]
    assert [s["name"] for s in out["skills"]] == ["s1"]


def test_apply_user_edits_no_filters_returns_input_shape():
    suggestions = {
        "project_templates": ["alpha"],
        "skills": [{"name": "s1"}],
        "mcp_servers": [{"id": "m1"}],
        "warnings": ["something"],
    }
    out = suggest.apply_user_edits(suggestions)
    assert out["project_templates"] == ["alpha"]
    assert [s["name"] for s in out["skills"]] == ["s1"]
    assert [s["id"] for s in out["mcp_servers"]] == ["m1"]
    assert out["warnings"] == ["something"]


def test_apply_user_edits_does_not_mutate_input():
    suggestions = {
        "project_templates": ["alpha", "beta"],
        "skills": [{"name": "s1"}],
        "mcp_servers": [],
        "warnings": [],
    }
    out = suggest.apply_user_edits(suggestions, rejected=["beta"])
    # Input is untouched.
    assert suggestions["project_templates"] == ["alpha", "beta"]
    # Output is a new structure.
    assert out is not suggestions
