"""Plugin manifest + skill layout tests for lane-PA.

Verify that the Claude Code / Claude Cowork plugin layer added in v0.3.0:

* has a valid ``.claude-plugin/plugin.json``
* declares the four wedge skills (``start``, ``suggest``, ``whoami``, ``setup``)
  each with a ``SKILL.md`` whose frontmatter ``name`` matches the directory
* ships ``hooks/hooks.json`` reproducing the lane-p PostToolUse hook
* ships ``.mcp.json`` (flat format) referencing the PA-MCP stdio server
* still ships the standalone ``SKILL.md`` at the repo root unchanged
  (regression guard for the coexistent standalone-skill install path)

Stdlib only - no PyYAML dependency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
VERSION_FILE = REPO_ROOT / "VERSION"
HOOKS_FILE = REPO_ROOT / "hooks" / "hooks.json"
MCP_FILE = REPO_ROOT / ".mcp.json"
STANDALONE_SKILL = REPO_ROOT / "SKILL.md"
SKILLS_DIR = REPO_ROOT / "skills"

EXPECTED_SKILLS = ("start", "suggest", "whoami", "setup")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_skill(path: Path):
    """Return ``(frontmatter_dict, body_str)`` for a SKILL.md.

    Implements a tiny YAML subset sufficient for our skill frontmatter:
    top-level ``key: value`` pairs plus ``[a, b, c]`` flow-style lists.
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    assert match, f"{path} does not begin with a YAML frontmatter block"
    raw, body = match.group(1), match.group(2)
    fm = {}
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
            fm[key] = items
        else:
            fm[key] = value.strip("'\"")
    return fm, body


# ---------------------------------------------------------------------------
# 1. plugin.json basics
# ---------------------------------------------------------------------------

def test_plugin_manifest_exists_and_parses():
    assert PLUGIN_MANIFEST.is_file(), (
        f"missing plugin manifest at {PLUGIN_MANIFEST}"
    )
    data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "plugin.json must be a JSON object"
    assert data.get("name"), "plugin.json missing required 'name'"


# ---------------------------------------------------------------------------
# 2. required fields present
# ---------------------------------------------------------------------------

def test_plugin_manifest_required_fields():
    data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    for field in ("name", "description", "author"):
        assert data.get(field), f"plugin.json missing required '{field}'"
    author = data["author"]
    assert isinstance(author, dict) and author.get("name"), (
        "plugin.json 'author' must be an object with a 'name'"
    )


# ---------------------------------------------------------------------------
# 3. version matches VERSION file
# ---------------------------------------------------------------------------

def test_plugin_version_matches_version_file():
    data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    manifest_version = data.get("version")
    file_version = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert manifest_version == file_version, (
        f"plugin.json version={manifest_version!r} does not match "
        f"VERSION={file_version!r}"
    )


# ---------------------------------------------------------------------------
# 4 + 5 + 6. Each wedge skill exists, parses, and frontmatter name matches dir
# ---------------------------------------------------------------------------

def test_each_wedge_skill_file_exists():
    for name in EXPECTED_SKILLS:
        skill_path = SKILLS_DIR / name / "SKILL.md"
        assert skill_path.is_file(), f"missing SKILL.md for skill '{name}': {skill_path}"


def test_each_wedge_skill_parses_as_frontmatter_plus_body():
    for name in EXPECTED_SKILLS:
        skill_path = SKILLS_DIR / name / "SKILL.md"
        fm, body = _parse_skill(skill_path)
        assert fm, f"{skill_path} has empty frontmatter"
        assert body.strip(), f"{skill_path} has empty markdown body"


def test_each_skill_frontmatter_name_matches_directory():
    for name in EXPECTED_SKILLS:
        skill_path = SKILLS_DIR / name / "SKILL.md"
        fm, _ = _parse_skill(skill_path)
        assert fm.get("name") == name, (
            f"{skill_path} frontmatter name={fm.get('name')!r} != "
            f"directory name {name!r}"
        )
        assert fm.get("description"), (
            f"{skill_path} frontmatter missing required 'description'"
        )


# ---------------------------------------------------------------------------
# 7. hooks.json reproduces the lane-p hook
# ---------------------------------------------------------------------------

def test_hooks_json_exists_and_has_post_tool_use_hook():
    assert HOOKS_FILE.is_file(), f"missing hooks file at {HOOKS_FILE}"
    data = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "hooks.json must be a JSON object"
    hooks = data.get("hooks")
    assert isinstance(hooks, dict), "hooks.json missing top-level 'hooks' object"

    post_tool_use = hooks.get("PostToolUse")
    assert isinstance(post_tool_use, list) and post_tool_use, (
        "hooks.json must declare a non-empty PostToolUse list"
    )

    matchers = []
    for entry in post_tool_use:
        assert isinstance(entry, dict), (
            "each PostToolUse entry must be an object with 'matcher' + 'hooks'"
        )
        matchers.append(entry.get("matcher"))
        sub_hooks = entry.get("hooks")
        assert isinstance(sub_hooks, list) and sub_hooks, (
            f"entry for matcher={entry.get('matcher')!r} has no 'hooks' list"
        )
        for h in sub_hooks:
            assert h.get("type") == "command"
            command = h.get("command", "")
            # The migrated lane-p hook delegates to hook_runner.py inside the
            # plugin and gates the hot path on managed-projects.json.
            assert "hook_runner.py" in command, (
                f"PostToolUse command must invoke hook_runner.py: {command!r}"
            )
            assert "managed-projects.json" in command, (
                "PostToolUse command must stat-check managed-projects.json "
                f"to keep the hot path tiny: {command!r}"
            )
            assert "${CLAUDE_PLUGIN_ROOT}" in command, (
                "PostToolUse command must use ${CLAUDE_PLUGIN_ROOT} to "
                f"reference bundled scripts: {command!r}"
            )

    # lane-p installs hooks for BOTH the Skill matcher and Edit|Write.
    assert "Skill" in matchers, (
        f"missing Skill matcher in hooks.json (got {matchers!r})"
    )
    assert any("Edit" in (m or "") and "Write" in (m or "") for m in matchers), (
        f"missing Edit|Write matcher in hooks.json (got {matchers!r})"
    )


# ---------------------------------------------------------------------------
# 8. .mcp.json references PA-MCP stdio server
# ---------------------------------------------------------------------------

def test_mcp_json_exists_and_references_persona_query_stdio():
    assert MCP_FILE.is_file(), f"missing MCP config at {MCP_FILE}"
    data = json.loads(MCP_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), ".mcp.json must be a JSON object"
    # Plugin-bundled .mcp.json is FLAT (not wrapped in 'mcpServers'). The
    # entry name is implementation-defined; we just require one entry whose
    # args reference scripts/persona_query_stdio.py.
    assert data, ".mcp.json must declare at least one MCP server"
    found = False
    for _name, spec in data.items():
        if not isinstance(spec, dict):
            continue
        args = spec.get("args") or []
        if any("persona_query_stdio.py" in str(a) for a in args):
            found = True
            break
    assert found, (
        f".mcp.json must reference scripts/persona_query_stdio.py "
        f"(got {data!r})"
    )


# ---------------------------------------------------------------------------
# 9. Standalone SKILL.md at repo root is untouched
# ---------------------------------------------------------------------------

def test_standalone_skill_md_still_exists():
    """Regression guard: the standalone-skill install path must keep working.

    The plugin layer is additive; the repo continues to function as a
    standalone skill loaded from ``~/.claude/skills/ai-quickstart/SKILL.md``.
    """
    assert STANDALONE_SKILL.is_file(), (
        "standalone SKILL.md must remain at repo root for the "
        "standalone-skill install path"
    )
    # Sanity-check: it still parses as frontmatter + body and the name is
    # still 'ai-quickstart'.
    fm, body = _parse_skill(STANDALONE_SKILL)
    assert fm.get("name") == "ai-quickstart", (
        f"standalone SKILL.md frontmatter name changed: {fm.get('name')!r}"
    )
    assert body.strip(), "standalone SKILL.md body is empty"
