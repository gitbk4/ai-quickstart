"""Tests for scripts/alternatives.py — Wave 2A alternatives engine.

Covers:
  1. load_alternatives happy path
  2. load_alternatives schema-version mismatch
  3. load_alternatives malformed YAML
  4. load_alternatives caching (module-level memoization)
  5. pair_with_suggestion happy path with persona
  6. pair_with_suggestion persona=None
  7. pair_with_suggestion no category tag
  8. compute_fit_score determinism
  9. compute_fit_score archetype + industry exact match -> >0.7
 10. compute_fit_score skill_tolerance mismatch penalty
 11. compute_fit_score persona without structured fields -> 0.5
 12. render_why_for_you mentions p:NNN when paragraph fits
 13. render_why_for_you fallback to archetype/industry
 14. render_why_for_you length cap
 15. stars_inline buckets
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Make scripts/ importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import alternatives  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "alternatives.yaml"
    p.write_text(body, encoding="utf-8")
    return p


_HAPPY_YAML = """schema_version: 1
alternatives:
  research-assistant:
    saas:
      - name: Perplexity
        url: "https://perplexity.ai"
        why: "fast web-grounded answers"
    oss:
      - name: Open WebUI
        url: "https://example.com/owui"
        why: "self-hosted"
    claude_skill:
      - name: "anthropics/courses"
        url: "https://github.com/anthropics/courses"
        why: "curated paths"
  code-review:
    saas:
      - name: CodeRabbit
        url: "https://coderabbit.ai"
        why: "PR review bot"
    oss:
      - name: Aider
        url: "https://github.com/Aider-AI/aider"
        why: "terminal pair-programmer"
"""


def _persona_full() -> dict:
    return {
        "schema_version": 1,
        "structured": {
            "role": "researcher",
            "archetype": "job",
            "industry": "marketing",
            "skill_tolerance": "medium",
            "project_style": "minimal",
            "top_projects": [{"name": "research-bot", "scaffolded_at": "2026-04-01T00:00:00Z"}],
        },
        "paragraphs": [
            {
                "id": "p:001",
                "text": "I work as a researcher and lean on a research-assistant flow daily.",
                "provenance": "anecdote",
                "trust_score": 4,
                "anchored_to": None,
                "locked": False,
                "merged_from": None,
            },
            {
                "id": "p:002",
                "text": "I prefer minimal scaffolds with low ceremony.",
                "provenance": "heal",
                "trust_score": 3,
                "anchored_to": None,
                "locked": False,
                "merged_from": None,
            },
        ],
        "deleted_ids": [],
    }


def _persona_minimal() -> dict:
    """Persona with no structured fields — should trigger neutral 0.5 fit."""
    return {
        "schema_version": 1,
        "structured": {},
        "paragraphs": [],
        "deleted_ids": [],
    }


# ---------------------------------------------------------------------------
# load_alternatives
# ---------------------------------------------------------------------------


class LoadAlternativesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        alternatives._clear_cache_for_tests()

    def test_happy_path(self):
        # Test 1
        p = _write_yaml(self.tmp_path, _HAPPY_YAML)
        result = alternatives.load_alternatives(p)
        self.assertIn("research-assistant", result)
        self.assertIn("code-review", result)
        kinds = result["research-assistant"]
        self.assertIn("saas", kinds)
        self.assertEqual(kinds["saas"][0]["name"], "Perplexity")

    def test_schema_version_mismatch_returns_empty(self):
        # Test 2
        p = _write_yaml(
            self.tmp_path,
            "schema_version: 99\nalternatives:\n  foo:\n    saas: []\n",
        )
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            result = alternatives.load_alternatives(p)
        self.assertEqual(result, {})
        self.assertIn("schema_version", err.getvalue())

    def test_malformed_yaml_returns_empty(self):
        # Test 3
        p = _write_yaml(
            self.tmp_path,
            "schema_version: 1\n  bad-indent: oops\n",
        )
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            result = alternatives.load_alternatives(p)
        self.assertEqual(result, {})
        self.assertIn("alternatives", err.getvalue())

    def test_missing_file_returns_empty(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            result = alternatives.load_alternatives(self.tmp_path / "nope.yaml")
        self.assertEqual(result, {})
        self.assertIn("not found", err.getvalue())

    def test_cached_across_calls(self):
        # Test 4
        p = _write_yaml(self.tmp_path, _HAPPY_YAML)
        first = alternatives.load_alternatives(p)
        # Now mutate the file. If the cache is honored, result is unchanged.
        p.write_text("schema_version: 1\nalternatives: {}\n", encoding="utf-8")
        second = alternatives.load_alternatives(p)
        self.assertIs(first, second)
        self.assertIn("research-assistant", second)

    def test_real_repo_alternatives_yaml_parses(self):
        """The committed mappings/alternatives.yaml must parse cleanly."""
        real = _REPO_ROOT / "mappings" / "alternatives.yaml"
        if not real.exists():
            self.skipTest("real alternatives.yaml not present in this checkout")
        alternatives._clear_cache_for_tests()
        result = alternatives.load_alternatives(real)
        self.assertGreaterEqual(len(result), 8)  # spec: ≥8 distinct tags
        # Each tag must have at least one entry per kind across the file.
        seen_kinds = set()
        for tag, kinds in result.items():
            for kind in kinds:
                seen_kinds.add(kind)
        for required in ("saas", "oss", "claude_skill", "mcp_server", "agent_platform"):
            self.assertIn(required, seen_kinds, f"missing kind {required}")


# ---------------------------------------------------------------------------
# pair_with_suggestion
# ---------------------------------------------------------------------------


class PairWithSuggestionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.yaml_path = _write_yaml(self.tmp_path, _HAPPY_YAML)
        alternatives._clear_cache_for_tests()

    def test_happy_path_with_persona(self):
        # Test 5
        suggestion = {"name": "research-assistant", "description": "Research"}
        persona = _persona_full()
        out = alternatives.pair_with_suggestion(
            suggestion, persona, yaml_path=self.yaml_path
        )
        self.assertGreaterEqual(len(out), 1)
        self.assertLessEqual(len(out), 2)
        for alt in out:
            self.assertIn("kind", alt)
            self.assertIn("name", alt)
            self.assertIn("url", alt)
            self.assertIn("why", alt)
            self.assertIn("fit_score", alt)
            self.assertIn("why_for_you", alt)
            self.assertIsInstance(alt["fit_score"], float)
            self.assertGreaterEqual(alt["fit_score"], 0.0)
            self.assertLessEqual(alt["fit_score"], 1.0)
            self.assertIsInstance(alt["why_for_you"], str)
            self.assertGreater(len(alt["why_for_you"]), 0)

    def test_persona_none_returns_alts_without_fit_score(self):
        # Test 6
        suggestion = {"name": "research-assistant"}
        out = alternatives.pair_with_suggestion(
            suggestion, None, yaml_path=self.yaml_path
        )
        self.assertGreaterEqual(len(out), 1)
        for alt in out:
            # Spec: "alternatives without fit_score / why_for_you (or with
            # placeholder values)". We use None placeholder for fit_score.
            self.assertIsNone(alt["fit_score"])
            # Wave 2.5: persona-None -> why_for_you field is omitted (was
            # boilerplate filler before). Other fields are still present.
            self.assertNotIn("why_for_you", alt)

    def test_no_category_tag_returns_empty(self):
        # Test 7
        suggestion = {"description": "no name no id no category"}
        out = alternatives.pair_with_suggestion(
            suggestion, _persona_full(), yaml_path=self.yaml_path
        )
        self.assertEqual(out, [])

    def test_unknown_tag_returns_empty(self):
        suggestion = {"name": "completely-unknown-tag-xyz"}
        out = alternatives.pair_with_suggestion(
            suggestion, _persona_full(), yaml_path=self.yaml_path
        )
        self.assertEqual(out, [])

    def test_at_most_two_alternatives(self):
        # research-assistant has 3 kinds in the test fixture; we cap at 2.
        suggestion = {"name": "research-assistant"}
        out = alternatives.pair_with_suggestion(
            suggestion, _persona_full(), yaml_path=self.yaml_path
        )
        self.assertLessEqual(len(out), 2)


# ---------------------------------------------------------------------------
# compute_fit_score
# ---------------------------------------------------------------------------


class ComputeFitScoreTests(unittest.TestCase):
    def test_determinism(self):
        # Test 8
        suggestion = {"name": "research-assistant", "archetype": "job", "industry": "marketing"}
        persona = _persona_full()
        s1 = alternatives.compute_fit_score(suggestion, persona)
        s2 = alternatives.compute_fit_score(suggestion, persona)
        s3 = alternatives.compute_fit_score(suggestion, persona)
        self.assertEqual(s1, s2)
        self.assertEqual(s2, s3)

    def test_archetype_industry_exact_match_high(self):
        # Test 9: same archetype + same industry should clear 0.7 even with
        # modest Jaccard overlap (since the bonuses are +0.20 + +0.15 = +0.35
        # on top of any tag-set Jaccard).
        suggestion = {
            "name": "research-assistant",
            "archetype": "job",
            "industry": "marketing",
            "tags": ["research-assistant", "job", "marketing"],
        }
        persona = _persona_full()  # archetype=job, industry=marketing
        score = alternatives.compute_fit_score(suggestion, persona)
        self.assertGreater(
            score,
            0.7,
            f"expected >0.7 for exact arch+industry match, got {score}",
        )

    def test_skill_tolerance_mismatch_penalty(self):
        # Test 10: equal everything except skill_tolerance — mismatch
        # should produce a strictly LOWER score than the matched control.
        base_sug = {
            "name": "research-assistant",
            "archetype": "job",
            "industry": "marketing",
            "skill_tolerance": "medium",
        }
        mismatch_sug = dict(base_sug)
        mismatch_sug["skill_tolerance"] = "high"
        persona = _persona_full()  # skill_tolerance=medium
        match = alternatives.compute_fit_score(base_sug, persona)
        mismatch = alternatives.compute_fit_score(mismatch_sug, persona)
        self.assertLess(mismatch, match)

    def test_persona_lacks_structured_returns_neutral(self):
        # Test 11
        suggestion = {"name": "research-assistant", "archetype": "job"}
        score = alternatives.compute_fit_score(suggestion, _persona_minimal())
        self.assertEqual(score, 0.5)

    def test_score_bounded_0_to_1(self):
        # Even on absurd inputs the score stays in [0, 1].
        sug = {"name": "x", "archetype": "job", "industry": "marketing", "tags": ["a", "b", "c"]}
        score = alternatives.compute_fit_score(sug, _persona_full())
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


# ---------------------------------------------------------------------------
# render_why_for_you
# ---------------------------------------------------------------------------


class RenderWhyForYouTests(unittest.TestCase):
    def test_paragraph_reference_when_match(self):
        # Test 12
        suggestion = {"name": "research-assistant"}
        alt = {
            "kind": "saas",
            "name": "Perplexity",
            "url": "https://perplexity.ai",
            "why": "fast",
        }
        persona = _persona_full()
        out = alternatives.render_why_for_you(alt, suggestion, persona)
        self.assertIn("p:001", out)

    def test_fallback_to_archetype_when_no_match(self):
        # Test 13
        suggestion = {"name": "totally-unrelated-tag"}
        alt = {"kind": "saas", "name": "Foo", "url": "https://x", "why": ""}
        persona = _persona_full()
        out = alternatives.render_why_for_you(alt, suggestion, persona)
        # No paragraph mentions "totally-unrelated-tag", so we fall back.
        self.assertNotIn("p:00", out)
        # archetype/industry/style fallback should mention something.
        self.assertTrue(
            any(token in out.lower() for token in ("job", "marketing", "minimal")),
            f"expected archetype/industry/style mention in {out!r}",
        )

    def test_length_cap_respected(self):
        # Test 14
        # Build a persona paragraph with absurdly long prose so the
        # paragraph-quote branch has to truncate.
        persona = _persona_full()
        persona["paragraphs"][0]["text"] = (
            "I run a research-assistant flow " + ("x" * 500)
        )
        suggestion = {"name": "research-assistant"}
        alt = {"kind": "saas", "name": "Perplexity", "url": "https://x", "why": "y" * 500}
        out = alternatives.render_why_for_you(alt, suggestion, persona)
        self.assertLessEqual(len(out), 140)

    def test_persona_none_returns_empty(self):
        # Wave 2.5: when persona is None, render_why_for_you returns ""
        # rather than the prior "General fit for..." filler. The empty
        # string lets pair_with_suggestion omit the field cleanly.
        suggestion = {"name": "research-assistant"}
        alt = {"kind": "saas", "name": "Perplexity", "url": "https://x", "why": "fast"}
        out = alternatives.render_why_for_you(alt, suggestion, None)
        self.assertEqual(out, "")


# ---------------------------------------------------------------------------
# Wave 2.5: curation alignment between personas.yaml and alternatives.yaml
# ---------------------------------------------------------------------------


class CurationAlignmentTests(unittest.TestCase):
    """Every personas.yaml entry name/id must resolve to a non-empty
    alternatives lookup. Without this, lane-2A's pair_with_suggestion
    silently returns ``[]`` for entries that have no tag in the table,
    which is exactly the cascading-kill mitigation we don't want."""

    @classmethod
    def setUpClass(cls):
        # Walk personas.yaml to collect every claude_skills[].name and
        # mcp_servers[].id, then load the real alternatives.yaml once.
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import suggest  # noqa: PLC0415

        personas_path = _REPO_ROOT / "mappings" / "personas.yaml"
        cls.personas = suggest.load_mapping(personas_path)
        cls.alts_path = _REPO_ROOT / "mappings" / "alternatives.yaml"
        alternatives._clear_cache_for_tests()
        cls.alts = alternatives.load_alternatives(cls.alts_path)

        names: set = set()
        ids: set = set()
        archetypes = cls.personas.get("archetypes", {})
        for archetype, blocks in archetypes.items():
            if not isinstance(blocks, dict):
                continue
            for industry_key, block in blocks.items():
                if not isinstance(block, dict):
                    continue
                for skill in block.get("claude_skills") or []:
                    if isinstance(skill, dict):
                        n = skill.get("name")
                        if isinstance(n, str) and n.strip():
                            names.add(n.strip().lower())
                for server in block.get("mcp_servers") or []:
                    if isinstance(server, dict):
                        i = server.get("id")
                        if isinstance(i, str) and i.strip():
                            ids.add(i.strip().lower())
        cls.persona_names = sorted(names)
        cls.persona_ids = sorted(ids)

    def test_every_persona_skill_name_has_alternatives(self):
        missing = [n for n in self.persona_names if n not in self.alts]
        self.assertFalse(
            missing,
            f"personas.yaml claude_skills names missing from alternatives.yaml: {missing}",
        )

    def test_every_persona_mcp_id_has_alternatives(self):
        missing = [i for i in self.persona_ids if i not in self.alts]
        self.assertFalse(
            missing,
            f"personas.yaml mcp_servers ids missing from alternatives.yaml: {missing}",
        )

    def test_every_persona_entry_yields_at_least_one_pair(self):
        """End-to-end: pair_with_suggestion must return >=1 alternative for
        every personas.yaml entry. Catches the case where a tag exists but
        all kinds are empty lists."""
        bad: list = []
        for n in self.persona_names:
            out = alternatives.pair_with_suggestion(
                {"name": n}, None, yaml_path=self.alts_path
            )
            if not out:
                bad.append(("name", n))
        for i in self.persona_ids:
            out = alternatives.pair_with_suggestion(
                {"id": i}, None, yaml_path=self.alts_path
            )
            if not out:
                bad.append(("id", i))
        self.assertFalse(
            bad, f"personas.yaml entries with no alternatives pairing: {bad}"
        )


# ---------------------------------------------------------------------------
# stars_inline
# ---------------------------------------------------------------------------


class StarsInlineTests(unittest.TestCase):
    def test_buckets(self):
        # Test 15
        cases = [
            (0.00, 1),
            (0.10, 1),
            (0.20, 2),
            (0.39, 2),
            (0.40, 3),
            (0.59, 3),
            (0.60, 4),
            (0.79, 4),
            (0.80, 5),
            (1.00, 5),
        ]
        for score, expected_filled in cases:
            out = alternatives.stars_inline(score)
            self.assertEqual(len(out), 5, f"5 chars expected for {score}")
            self.assertEqual(
                out.count("★"),
                expected_filled,
                f"score {score}: expected {expected_filled} filled, got {out!r}",
            )

    def test_none_returns_placeholder(self):
        out = alternatives.stars_inline(None)
        self.assertEqual(len(out), 5)
        self.assertNotIn("★", out)

    def test_clamps_out_of_range(self):
        # Negative -> 1 star, >1 -> 5 stars.
        self.assertEqual(alternatives.stars_inline(-1.0).count("★"), 1)
        self.assertEqual(alternatives.stars_inline(2.0).count("★"), 5)


# ---------------------------------------------------------------------------
# lane-PEXP: non-dev role expansion
# ---------------------------------------------------------------------------


class NonDevExpansionTests(unittest.TestCase):
    """The v0.3.0 Cowork wedge needs alternatives for non-dev roles:
    M&A research, financial modeling, energy project mgmt, supply chain,
    sales outreach. Guards the curation so a non-dev persona doesn't get
    empty suggestions."""

    # Tags lane-PEXP added. If a tag is renamed, update this list AND the
    # personas it serves.
    NEW_TAGS = [
        "deal-research",
        "financial-modeling-mna",
        "financial-modeling",
        "revenue-reporting",
        "project-management-energy",
        "vendor-research-energy",
        "supply-chain-analysis",
        "logistics-optimization",
        "sales-outreach-smb",
        "customer-research",
        "meeting-research",
        "document-research",
    ]

    ALLOWED_KINDS = {"saas", "oss", "claude_skill", "mcp_server", "agent_platform"}

    @classmethod
    def setUpClass(cls):
        alternatives._clear_cache_for_tests()
        cls.alts = alternatives.load_alternatives(
            _REPO_ROOT / "mappings" / "alternatives.yaml"
        )

    def test_new_tags_present_and_nonempty(self):
        for tag in self.NEW_TAGS:
            self.assertIn(tag, self.alts, f"non-dev tag {tag!r} missing")
            kinds = self.alts[tag]
            self.assertTrue(
                any(kinds.get(k) for k in self.ALLOWED_KINDS),
                f"tag {tag!r} has no entries in any kind bucket",
            )

    def test_every_url_is_syntactically_valid(self):
        # No-network: just assert every url parses as http(s) with a netloc.
        from urllib.parse import urlparse

        bad = []
        for tag, kinds in self.alts.items():
            if not isinstance(kinds, dict):
                continue
            for kind, entries in kinds.items():
                for entry in entries or []:
                    url = entry.get("url", "")
                    p = urlparse(url)
                    if p.scheme not in ("http", "https") or not p.netloc:
                        bad.append(f"{tag}/{kind}/{entry.get('name')}: {url!r}")
        self.assertFalse(bad, f"malformed URLs: {bad}")

    def test_no_known_dead_vendors(self):
        # clearbit (wound into HubSpot) + thomasnet (hard bot-block, 403)
        # were dropped during PEXP cleanup. Regression guard so they don't
        # creep back via copy-paste.
        blob = (_REPO_ROOT / "mappings" / "alternatives.yaml").read_text(
            encoding="utf-8"
        ).lower()
        self.assertNotIn("clearbit.com", blob)
        self.assertNotIn("thomasnet.com", blob)

    def test_ma_analyst_persona_fits_deal_research(self):
        persona = {
            "schema_version": 1,
            "structured": {
                "role": "M&A analyst",
                "archetype": "job",
                "industry": "pharma",
                "skill_tolerance": "low",
                "project_style": "minimal",
                "top_projects": [],
            },
            "paragraphs": [],
            "deleted_ids": [],
        }
        suggestion = {
            "name": "deal-research",
            "category": "deal-research",
            "archetype": "job",
            "industry": "pharma",
        }
        score = alternatives.compute_fit_score(suggestion, persona)
        self.assertGreater(
            score, 0.5,
            f"M&A analyst should fit deal-research > 0.5, got {score}",
        )


if __name__ == "__main__":
    unittest.main()
