"""Tests for scripts/persona.py.

Covers:
  - missing file -> defaults
  - malformed YAML -> warns and uses defaults
  - roundtrip (write then parse returns equivalent)
  - atomic backup created on overwrite
  - anecdote append creates file
  - anecdote append preserves existing content
  - version bumping
  - diff output for changed prose
  - schema fields exactly match PLAN.md schema
  - template renders with all placeholders filled
"""
from __future__ import annotations

import string
import sys
from pathlib import Path

import pytest

# Make scripts/ importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import persona  # noqa: E402  pylint: disable=wrong-import-position


# ---------- helpers ----------

def _make_persona(tmp_path: Path) -> Path:
    return tmp_path / "persona.md"


# ---------- schema ----------

def test_default_persona_has_all_schema_fields():
    d = persona.default_persona()
    # Top-level sections
    assert set(d.keys()) == {"identity", "goals", "preferences", "activity", "generated"}
    # identity
    assert set(d["identity"].keys()) == {"role", "industry", "archetype"}
    assert d["identity"]["archetype"] in ("job", "personal", "exploring")
    # goals
    assert set(d["goals"].keys()) == {"top_problems", "desired_outcomes"}
    assert isinstance(d["goals"]["top_problems"], list)
    assert isinstance(d["goals"]["desired_outcomes"], list)
    # preferences
    assert set(d["preferences"].keys()) == {
        "project_style", "coding_languages", "skill_tolerance",
    }
    assert d["preferences"]["project_style"] in ("minimal", "full")
    assert d["preferences"]["skill_tolerance"] in ("strict", "permissive")
    assert isinstance(d["preferences"]["coding_languages"], list)
    # activity
    assert set(d["activity"].keys()) == {
        "project_count", "total_skill_uses", "top_projects", "last_active",
    }
    assert d["activity"]["project_count"] == 0
    assert d["activity"]["total_skill_uses"] == 0
    assert isinstance(d["activity"]["top_projects"], list)
    assert isinstance(d["activity"]["last_active"], str)
    # generated
    assert set(d["generated"].keys()) == {
        "updated_at", "anecdote_count", "version",
    }
    assert d["generated"]["version"] == 1
    assert d["generated"]["anecdote_count"] == 0
    assert isinstance(d["generated"]["updated_at"], str)


# ---------- missing file ----------

def test_parse_missing_file_returns_defaults(tmp_path: Path):
    p = _make_persona(tmp_path)
    assert not p.exists()
    result = persona.parse_persona(p)
    assert "frontmatter" in result and "prose" in result
    assert result["prose"] == ""
    # Frontmatter matches default schema
    assert result["frontmatter"] == persona.default_persona() or \
        set(result["frontmatter"].keys()) == set(persona.default_persona().keys())


# ---------- malformed YAML ----------

def test_parse_malformed_frontmatter_warns_and_uses_defaults(tmp_path: Path, capsys):
    p = _make_persona(tmp_path)
    # No closing delimiter.
    p.write_text("---\nidentity:\n  role: dev\n# missing close\n", encoding="utf-8")
    result = persona.parse_persona(p)
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "frontmatter" in err.lower()
    # Defaults returned
    assert set(result["frontmatter"].keys()) == set(persona.default_persona().keys())


def test_parse_malformed_frontmatter_value_warns(tmp_path: Path, capsys):
    p = _make_persona(tmp_path)
    # Two-level nesting (not allowed) under identity.
    p.write_text(
        "---\nidentity:\n  role:\n    deep: nope\n---\nbody\n",
        encoding="utf-8",
    )
    result = persona.parse_persona(p)
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    # Falls back to defaults for frontmatter
    assert "identity" in result["frontmatter"]
    assert result["frontmatter"]["identity"]["role"] == ""  # default


# ---------- roundtrip ----------

def test_roundtrip_write_then_parse(tmp_path: Path):
    p = _make_persona(tmp_path)
    fm = persona.default_persona()
    fm["identity"]["role"] = "ML engineer"
    fm["identity"]["industry"] = "fintech"
    fm["identity"]["archetype"] = "job"
    fm["goals"]["top_problems"] = ["onboarding chaos", "no shared context"]
    fm["preferences"]["coding_languages"] = ["python", "typescript"]
    fm["activity"]["project_count"] = 3
    fm["activity"]["top_projects"] = ["alpha", "beta"]
    prose = "Two paragraphs of narrative.\n\nSecond paragraph here.\n"
    persona.write_persona(p, fm, prose)
    parsed = persona.parse_persona(p)
    pfm = parsed["frontmatter"]
    assert pfm["identity"]["role"] == "ML engineer"
    assert pfm["identity"]["industry"] == "fintech"
    assert pfm["identity"]["archetype"] == "job"
    assert pfm["goals"]["top_problems"] == ["onboarding chaos", "no shared context"]
    assert pfm["preferences"]["coding_languages"] == ["python", "typescript"]
    assert pfm["activity"]["project_count"] == 3
    assert pfm["activity"]["top_projects"] == ["alpha", "beta"]
    assert parsed["prose"].strip() == prose.strip()


def test_roundtrip_handles_special_chars(tmp_path: Path):
    p = _make_persona(tmp_path)
    fm = persona.default_persona()
    # String that needs quoting (contains a colon).
    fm["identity"]["role"] = "site reliability: on-call lead"
    fm["goals"]["desired_outcomes"] = ["ship: weekly", "reduce: on-call"]
    persona.write_persona(p, fm, "")
    parsed = persona.parse_persona(p)
    assert parsed["frontmatter"]["identity"]["role"] == "site reliability: on-call lead"
    assert parsed["frontmatter"]["goals"]["desired_outcomes"] == [
        "ship: weekly", "reduce: on-call",
    ]


# ---------- atomic backup ----------

def test_overwrite_creates_backup(tmp_path: Path):
    p = _make_persona(tmp_path)
    fm = persona.default_persona()
    persona.write_persona(p, fm, "first version\n")
    backup = p.with_suffix(p.suffix + ".bak")
    assert not backup.exists(), "no backup expected on first write"
    # Capture original content
    original = p.read_text(encoding="utf-8")
    # Overwrite
    persona.write_persona(p, fm, "second version\n")
    assert backup.exists(), "backup .bak must be created on overwrite"
    assert backup.read_text(encoding="utf-8") == original


def test_no_tmp_left_behind(tmp_path: Path):
    p = _make_persona(tmp_path)
    fm = persona.default_persona()
    persona.write_persona(p, fm, "x")
    persona.write_persona(p, fm, "y")
    tmp_p = p.with_suffix(p.suffix + ".tmp")
    assert not tmp_p.exists(), "tmp file must be cleaned up by atomic replace"


# ---------- anecdote append ----------

def test_append_anecdote_creates_file(tmp_path: Path):
    anecdotes = tmp_path / "anecdotes"
    out = persona.append_anecdote(anecdotes, "alpha-project", "tried X, observed Y.")
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "alpha-project" in text
    assert "tried X, observed Y." in text
    # Has a timestamp header
    assert "## " in text


def test_append_anecdote_preserves_existing(tmp_path: Path):
    anecdotes = tmp_path / "anecdotes"
    persona.append_anecdote(anecdotes, "alpha", "first entry body.")
    persona.append_anecdote(anecdotes, "alpha", "second entry body.")
    target = anecdotes / "alpha.md"
    text = target.read_text(encoding="utf-8")
    assert "first entry body." in text
    assert "second entry body." in text
    # Two timestamp headers
    assert text.count("## ") >= 2


def test_append_anecdote_rejects_bad_slug(tmp_path: Path):
    anecdotes = tmp_path / "anecdotes"
    with pytest.raises(ValueError):
        persona.append_anecdote(anecdotes, "bad/slash", "x")
    with pytest.raises(ValueError):
        persona.append_anecdote(anecdotes, "", "x")
    with pytest.raises(ValueError):
        persona.append_anecdote(anecdotes, ".dotfile", "x")


# ---------- version bumping ----------

def test_version_bumps_each_write(tmp_path: Path):
    p = _make_persona(tmp_path)
    fm = persona.default_persona()
    assert fm["generated"]["version"] == 1
    persona.write_persona(p, fm, "v1 prose")
    parsed1 = persona.parse_persona(p)
    v1 = parsed1["frontmatter"]["generated"]["version"]
    persona.write_persona(p, parsed1["frontmatter"], "v2 prose")
    parsed2 = persona.parse_persona(p)
    v2 = parsed2["frontmatter"]["generated"]["version"]
    persona.write_persona(p, parsed2["frontmatter"], "v3 prose")
    parsed3 = persona.parse_persona(p)
    v3 = parsed3["frontmatter"]["generated"]["version"]
    assert v2 == v1 + 1
    assert v3 == v2 + 1


def test_updated_at_changes_on_write(tmp_path: Path):
    p = _make_persona(tmp_path)
    fm = persona.default_persona()
    fm["generated"]["updated_at"] = "2000-01-01T00:00:00Z"
    persona.write_persona(p, fm, "prose")
    parsed = persona.parse_persona(p)
    assert parsed["frontmatter"]["generated"]["updated_at"] != "2000-01-01T00:00:00Z"


# ---------- diff ----------

def test_diff_persona_identical(tmp_path: Path):
    out = persona.diff_persona("same prose\n", "same prose\n")
    assert out == ""


def test_diff_persona_changed(tmp_path: Path):
    old = "Alice is a marketer.\nShe likes data.\n"
    new = "Alice is a senior marketer.\nShe likes data and code.\n"
    out = persona.diff_persona(old, new)
    assert out != ""
    # Unified diff has these markers
    assert out.startswith("---")
    assert "+++" in out
    assert "@@" in out
    # Removed and added lines present
    assert any(line.startswith("-Alice is a marketer.") for line in out.splitlines())
    assert any(line.startswith("+Alice is a senior marketer.") for line in out.splitlines())


# ---------- template ----------

def test_template_has_all_placeholders():
    """The persona template must reference every schema field."""
    tmpl_path = REPO_ROOT / "templates" / "persona.md.tmpl"
    assert tmpl_path.exists(), f"missing template at {tmpl_path}"
    raw = tmpl_path.read_text(encoding="utf-8")
    expected = [
        "$identity_role", "$identity_industry", "$identity_archetype",
        "$goals_top_problems", "$goals_desired_outcomes",
        "$preferences_project_style", "$preferences_coding_languages",
        "$preferences_skill_tolerance",
        "$activity_project_count", "$activity_total_skill_uses",
        "$activity_top_projects", "$activity_last_active",
        "$generated_updated_at", "$generated_anecdote_count", "$generated_version",
        "$prose",
    ]
    for placeholder in expected:
        assert placeholder in raw, f"template missing placeholder: {placeholder}"


def test_template_renders_with_string_template(tmp_path: Path):
    tmpl_path = REPO_ROOT / "templates" / "persona.md.tmpl"
    raw = tmpl_path.read_text(encoding="utf-8")
    t = string.Template(raw)
    rendered = t.substitute(
        identity_role="dev",
        identity_industry="saas",
        identity_archetype="job",
        goals_top_problems="[]",
        goals_desired_outcomes="[]",
        preferences_project_style="minimal",
        preferences_coding_languages="[]",
        preferences_skill_tolerance="permissive",
        activity_project_count=0,
        activity_total_skill_uses=0,
        activity_top_projects="[]",
        activity_last_active="2026-04-29T00:00:00Z",
        generated_updated_at="2026-04-29T00:00:00Z",
        generated_anecdote_count=0,
        generated_version=1,
        prose="hello world",
    )
    # Rendered output should be valid persona-shaped markdown.
    assert rendered.startswith("---\n")
    assert "hello world" in rendered
    # Sanity: all placeholders substituted (no $ tokens left except literal ones).
    # string.Template raises on missing keys when using substitute(), so reaching
    # here means every placeholder was resolved.


# ---------- parse with no-frontmatter file ----------

def test_parse_file_with_no_frontmatter_returns_defaults(tmp_path: Path):
    p = _make_persona(tmp_path)
    p.write_text("just prose, no frontmatter at all\n", encoding="utf-8")
    result = persona.parse_persona(p)
    assert set(result["frontmatter"].keys()) == set(persona.default_persona().keys())
    assert "just prose" in result["prose"]


# ---------- parse handles partial frontmatter gracefully ----------

def test_parse_partial_frontmatter_fills_defaults(tmp_path: Path):
    p = _make_persona(tmp_path)
    p.write_text(
        "---\nidentity:\n  role: dev\n  industry: edtech\n  archetype: personal\n---\nbody\n",
        encoding="utf-8",
    )
    result = persona.parse_persona(p)
    fm = result["frontmatter"]
    assert fm["identity"]["role"] == "dev"
    assert fm["identity"]["industry"] == "edtech"
    assert fm["identity"]["archetype"] == "personal"
    # Missing sections filled by defaults.
    assert "goals" in fm and "preferences" in fm
    assert "activity" in fm and "generated" in fm
    assert result["prose"].strip() == "body"
