# INSTALL — ai-quickstart per-runtime install paths

ai-quickstart is a portable skill: the same `SKILL.md` works in three AI
coding harnesses. Only the install paths and harness-level integrations
(hooks, settings file shapes) differ. This file documents the contract.

## Supported runtimes

| Runtime               | Detected via                          | Skill home dir       |
|-----------------------|---------------------------------------|----------------------|
| Claude Code           | `CLAUDE_HOME` env or `~/.claude/`     | `~/.claude/`         |
| OpenAI Codex CLI      | `CODEX_HOME` env or `~/.codex/`       | `~/.codex/`          |
| Google Antigravity    | `ANTIGRAVITY_HOME` env or `~/.antigravity/` | `~/.antigravity/` |

Detection order is exactly the order above. The first signal that hits
wins. Run `python3 scripts/paths.py --detect-runtime` to see what the
current host looks like to ai-quickstart.

## Install paths per runtime

| Runtime      | Skills directory          | Skill install path                       |
|--------------|---------------------------|------------------------------------------|
| claude-code  | `~/.claude/skills/`       | `~/.claude/skills/ai-quickstart/`        |
| codex        | `~/.codex/skills/`        | `~/.codex/skills/ai-quickstart/`         |
| antigravity  | `~/.antigravity/skills/`  | `~/.antigravity/skills/ai-quickstart/`   |

State files (managed-projects, installed-hooks manifest, persona, runs,
caches) always live under `~/.ai-quickstart/` regardless of runtime. This
keeps the per-user state portable if you switch runtimes.

## Settings / config files per runtime

| Runtime      | Path                              | Format     |
|--------------|-----------------------------------|------------|
| claude-code  | `~/.claude/settings.json`         | JSON       |
| codex        | `~/.codex/config.toml`            | TOML       |
| antigravity  | `~/.antigravity/settings.json`    | JSON       |

These paths are returned by `paths.host_settings_path(runtime)`. v1.1 only
*reads* the claude-code settings file; codex/antigravity paths are exposed
for future use.

## Hook support matrix

| Runtime      | PostToolUse hooks       | Status                                 |
|--------------|-------------------------|----------------------------------------|
| claude-code  | yes                     | Shipped in v1.0. Two hooks: Skill, Edit\|Write. |
| codex        | no (planned for v1.2)   | Codex CLI hooks model differs; not yet implemented. |
| antigravity  | no (planned for v1.2)   | Antigravity hooks model differs; not yet implemented. |

When `hooks_install.install()` runs on codex or antigravity it prints a
stderr warning and returns `False` without writing anything. This is the
intended graceful no-op.

## Manual logging fallback

The PostToolUse hooks exist to log Edit/Write/Skill events into
`~/.ai-quickstart/persona/activity.jsonl`, which feeds the persona-heal
loop. On runtimes without hook support you can record activity manually
from inside a managed project:

```bash
/ai-quickstart log-activity
```

This is equivalent to one hook fire. Use it after a meaningful work block
(or wire it into your editor's autosave). Persona heal will pick the
events up on the next `/ai-quickstart heal` invocation.

## How runtime detection works

`paths.detect_host_runtime()` checks signals in this fixed priority order:

1. `CLAUDE_HOME` env set OR `~/.claude/` exists -> `claude-code`
2. `CODEX_HOME` env set OR `~/.codex/` exists -> `codex`
3. `ANTIGRAVITY_HOME` env set OR `~/.antigravity/` exists -> `antigravity`
4. otherwise -> `unknown`

The env-var override is checked before the directory probe, so a user with
both `~/.claude/` and `~/.codex/` installed who explicitly sets
`CODEX_HOME=...` (or anything else) still gets the env-var winner. Without
overrides, claude-code wins because that is the runtime the skill was
authored against.

`paths.host_settings_path(runtime)` and `paths.host_skills_dir(runtime)`
return the per-runtime paths above. Both return `None` for `unknown`.

## v1.2 roadmap (deferred)

* Codex CLI hook integration (likely via `~/.codex/config.toml`
  `[[hooks.post_tool_use]]` once Codex stabilizes the schema).
* Antigravity hook integration.
* Optional: per-runtime install detector that scans for installed CLI
  binaries (`claude --version`, `codex --version`, `antigravity --version`)
  and prefers a runtime whose CLI is on `$PATH`.
