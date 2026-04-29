# ai-quickstart — v1 Plan

A Claude Code skill that interviews users about their AI goals, suggests
projects + skills with live freshness data, scaffolds compathy-structured
projects, and maintains a self-healing global persona that informs future
suggestions.

**Plan reviewed:** 2026-04-29. /plan-eng-review with outside voice.
**Repo target:** GitHub-hosted at `<owner>/ai-quickstart`. Local working dir
`/Users/bk/Code/ai-quickstart`.
**v2 deferred:** see "Deferred to v2" section.

---

## Problem statement

People who want to "get started with AI projects" face a cold-start problem:
- They don't know what kinds of projects fit their role/industry/goals
- They don't know which Claude skills, MCP servers, or GitHub tools to use
- They don't know how to structure a project for context-efficient AI work

Existing answers (compathy, gstack, Memento) solve project structure and
skill ecosystems, but don't bridge from "I have a vague AI goal" to "here is
your scaffolded project + recommended toolkit + a persona that gets smarter
as you use it."

ai-quickstart is the bridge.

---

## Locked v1 spec

### 1. Runtime
- Claude Code skill (SKILL.md + Python scripts), pattern matching compathy and gstack.
- Repo at `/Users/bk/Code/ai-quickstart`, hosted on GitHub.
- Phase 0 self-update (git pull --ff-only) following compathy's pattern.
- v2 adds Codex + Antigravity compatibility (single SKILL.md works in both).

### 2. Interview flow — three steps (not five)

```
   ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
   │  STEP 1          │      │  STEP 2          │      │  STEP 3          │
   │  INTERVIEW       │ ───▶ │  SUGGEST         │ ───▶ │  SCAFFOLD        │
   │                  │      │                  │      │                  │
   │  3-Q entry       │      │  curated YAML    │      │  compathy init   │
   │  + progressive   │      │  + GitHub stars  │      │  per project     │
   │  deeper Qs       │      │  + MCP registry  │      │  + write anecdote│
   │  + web research  │      │  + mcpmarket     │      │  + ask for       │
   │  inline          │      │    (scraped)     │      │    starting files│
   │  + user confirms │      │  user accepts /  │      │  + heal persona  │
   │    or challenges │      │  rejects items   │      │                  │
   └──────────────────┘      └──────────────────┘      └──────────────────┘
            │                          │                          │
            ▼                          ▼                          ▼
   ~/.ai-quickstart/runs/<run-id>/step-1-interview.md
   ~/.ai-quickstart/runs/<run-id>/step-2-suggestions.md
   ~/.ai-quickstart/runs/<run-id>/step-3-scaffold.md
            (audit artifacts; LLM context inputs for next step)
```

Step 1 is one progressive interview, not two phases. Web research happens
mid-interview (when Claude detects it needs more context to ask good
follow-ups). User confirms/challenges assumptions before exit. Step 3 asks
for starting files inline (not a separate Step 5).

### 3. Suggestion sources (Step 2)

**Curated mapping** at `mappings/personas.yaml`:
```yaml
schema_version: 1
archetypes:
  job:
    industry-marketing:
      project_templates: [content-research, audience-personas]
      claude_skills:
        - { name: research, github: "owner/repo", mcpmarket_url: "..." }
      mcp_servers:
        - { id: "registry-id", title: "..." }
```

**Live freshness data** layered on curated entries:
- **GitHub** (3-tier auth: gh CLI → GITHUB_TOKEN env → unauth + 6h cache).
  Fetches stars, last-commit, contributor count.
- **Anthropic MCP registry** via the `mcp-registry` MCP server tool.
- **mcpmarket.com** scraped with stdlib urllib + html.parser, 24h TTL cache,
  identifying User-Agent, robots.txt respected, 1 req/sec throttle, graceful
  empty-with-warning on parse failure. **Accepted risk:** silent regression
  to 0 results when their HTML changes.

**Quality warnings:** any source returning <100 stars OR <100 users (where
the metric exists) gets flagged with a warning badge. Low-quality skills are
not hidden, just labeled.

**Sources are queried in parallel** via `concurrent.futures.ThreadPoolExecutor`
(stdlib, max_workers=3).

### 4. Compathy dependency
- **Soft dep + auto-install** in Phase 0.
- Checks for compathy at `~/.claude/skills/compathy/`.
- If missing: `git clone <pinned-SHA> https://github.com/<owner>/compathy.git`.
- If present: runs `compathy/scripts/update.py` (compathy's existing self-update).
- **SHA pin** stored in `ai-quickstart/COMPATHY_VERSION`. Bumped intentionally
  by maintainer after testing, not pulled from HEAD. Avoids supply-chain risk
  and reproducibility loss.

### 5. Persona system (purpose-built, NOT compathy-shaped)

```
~/.ai-quickstart/
├── persona/
│   ├── persona.md              ← rendered: YAML frontmatter + 200-400 word prose
│   ├── persona.md.bak          ← atomic backup written before each heal
│   ├── activity.jsonl          ← current week's events (rotated weekly)
│   ├── activity-YYYY-WW.jsonl  ← rotated weekly archive
│   ├── activity-summary.json   ← monthly aggregate (project counts, top skills)
│   ├── anecdotes/
│   │   └── {project-slug}.md   ← per-project append-only anecdote
│   └── .heal.lock              ← flock target
├── runs/
│   └── {run-id}/
│       ├── step-1-interview.md ← adversarial prompt files (audit + LLM input)
│       ├── step-2-suggestions.md
│       └── step-3-scaffold.md
├── cache/
│   ├── github/{repo}.json      ← 6h TTL
│   └── mcpmarket/{query}.json  ← 24h TTL
├── managed-projects.json       ← central registry of ai-quickstart projects
├── installed-hooks.json        ← manifest of hooks ai-quickstart installed
├── heal-errors.jsonl           ← heal failure telemetry
└── config.json                 ← user preferences
```

**Persona schema:**
```yaml
---
identity:
  role: "<freeform>"
  industry: "<freeform>"
  archetype: job | personal | exploring
goals:
  top_problems: ["..."]
  desired_outcomes: ["..."]
preferences:
  project_style: minimal | full
  coding_languages: ["..."]
  skill_tolerance: strict | permissive
activity:
  project_count: 0
  total_skill_uses: 0
  top_projects: ["..."]
  last_active: "2026-04-29T..."
generated:
  updated_at: "2026-04-29T..."
  anecdote_count: 0
  version: 1
---

<200-400 word prose summary written by Claude during heal>
```

### 6. Heal loop

```
                       ┌─────────────────────────┐
                       │  /ai-quickstart heal    │
                       │  (or auto-fire when     │
                       │   /ai-quickstart runs   │
                       │   with new anecdotes)   │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  flock(.heal.lock)      │
                       │  fail fast if held      │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  read activity.jsonl +  │
                       │  activity-summary.json  │
                       │  + all anecdotes/*.md   │
                       │  + current persona.md   │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  Claude rewrites prose  │
                       │  + bumps frontmatter    │
                       │  fields (counts, dates) │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  cp persona.md → .bak   │
                       │  atomic write tmp+rename│
                       │  show diff to user      │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌─────────────────────────┐
                       │  release flock          │
                       └─────────────────────────┘
```

**Failure mode:** any exception during heal logs to `heal-errors.jsonl`,
releases flock, leaves prior `persona.md` untouched. Backup not consumed.

**flock + NFS warning:** Phase 0 detects if `~/.ai-quickstart/` is on a sync'd
filesystem (iCloud Drive, NFS, Dropbox) and warns. flock semantics are
unreliable on those — recommend either moving the dir or running heal from
one Claude Code session at a time.

### 7. Subroutine hooks (full hardening)

```
ai-quickstart Phase 0 install:
─────────────────────────────────────────────────────────
1. Detect Claude Code version. Warn if outside known-compatible range.
2. Read ~/.claude/settings.json. Parse current hooks.
3. Show user: "ai-quickstart will add 2 PostToolUse hooks. OK? [Y/n]"
4. On consent, append our 2 hooks (one for Skill, one for Edit/Write).
5. Each hook command is a TINY bash one-liner:
       [ -f ~/.ai-quickstart/managed-projects.json ] && \
         grep -q "$(pwd)" ~/.ai-quickstart/managed-projects.json && \
         exec python3 ~/.claude/skills/ai-quickstart/scripts/hook_runner.py
   Stat-check completes in <1ms. Python only execs when cwd is managed.
6. Atomic write settings.json via tmp+rename (validates JSON parses first).
7. Record exact hook entries written into ~/.ai-quickstart/installed-hooks.json
   (the manifest). Manifest is the source of truth for /ai-quickstart uninstall.
─────────────────────────────────────────────────────────
```

```
/ai-quickstart uninstall:
─────────────────────────────────────────────────────────
1. Read installed-hooks.json manifest.
2. Read settings.json. Remove exact entries listed in manifest.
3. Validate result still parses. Atomic write.
4. Optionally: rm -rf ~/.ai-quickstart (asks user).
5. Leaves user's other hooks untouched.
─────────────────────────────────────────────────────────
```

**Activity log line shape (must stay ≤4096 bytes for atomic POSIX append):**
```jsonl
{"ts":"2026-04-29T13:35:00Z","event":"skill","skill":"compathy","cwd":"/path","run_id":"..."}
{"ts":"2026-04-29T13:36:00Z","event":"edit","file":"<path>","cwd":"/path","run_id":"..."}
```

### 8. activity.jsonl rotation
- Weekly: `activity.jsonl` renamed to `activity-YYYY-WW.jsonl` at first heal of new ISO week.
- Monthly: archived weeks compacted into `activity-summary.json` (per-week aggregates).
- Heal reads only **current week raw** + **summary**. Bounded heal time forever.

### 9. Project marker (centralized, not per-project)
- `~/.ai-quickstart/managed-projects.json` is a JSON array of absolute project paths.
- Each ai-quickstart-scaffolded project is appended to the registry on Step 3.
- **No per-project marker file** — avoids `.ai-quickstart.json` getting accidentally git-committed and leaking run-ids.
- Hook stat-check looks up cwd against this registry.

---

## Architecture overview

```
                                ┌──────────────────────┐
   user types /ai-quickstart    │  SKILL.md            │
   in Claude Code            ───▶  Phase 0:            │
                                │   • self-update      │
                                │   • detect compathy  │
                                │   • detect filesystem│
                                │   • install hooks    │
                                └──────────┬───────────┘
                                           │
                              ┌────────────┴────────────┐
                              │                         │
                              ▼                         ▼
                    ┌──────────────────┐      ┌──────────────────┐
                    │  scripts/init.py │      │  scripts/heal.py │
                    │  (3-step flow)   │      │  (flock-protected│
                    └────┬─────────────┘      │   recompile)     │
                         │                    └──────────────────┘
            ┌────────────┼────────────┐
            ▼            ▼            ▼
    ┌────────────┐┌─────────────┐┌──────────────┐
    │interview.py││  suggest.py ││ scaffold.py  │
    └────┬───────┘└──┬──────────┘└──────┬───────┘
         │           │                  │
         │           ▼                  │
         │  ┌──────────────────┐        │
         │  │ sources/         │        │
         │  │   github.py      │        │
         │  │   mcp_registry.py│        │
         │  │   mcpmarket.py   │        │
         │  │   cache.py (TTL) │        │
         │  └──────────────────┘        │
         │                              ▼
         ▼                    ┌──────────────────┐
    ┌────────────┐            │  compathy        │
    │ prompts.py │            │  scaffold.py     │
    │ (run-id    │            │  (auto-installed)│
    │  artifacts)│            └──────────────────┘
    └────────────┘

    Hook side (separate process per tool call):
    ─────────────────────────────────────────────
    Claude Code fires PostToolUse → bash one-liner →
    stat-check managed-projects.json (<1ms) →
    if matched: exec python hook_runner.py → append activity.jsonl
```

### Module layout (mirrors compathy)

```
ai-quickstart/
├── SKILL.md
├── README.md
├── ARCHITECTURE.md
├── COMPATHY_VERSION         # pinned SHA
├── VERSION
├── LICENSE
├── mappings/
│   └── personas.yaml
├── templates/
│   ├── prompts/
│   │   ├── step-1.md.tmpl
│   │   ├── step-2.md.tmpl
│   │   └── step-3.md.tmpl
│   ├── persona.md.tmpl
│   └── anecdote.md.tmpl
├── scripts/
│   ├── init.py             # 3-step orchestrator
│   ├── interview.py        # Step 1
│   ├── suggest.py          # Step 2
│   ├── scaffold.py         # Step 3
│   ├── heal.py             # heal command
│   ├── hooks_install.py    # Phase 0 hook installer
│   ├── hook_runner.py      # the actual hook handler
│   ├── persona.py          # frontmatter parse/write
│   ├── prompts.py          # adversarial prompt files
│   ├── update.py           # self-update (compathy pattern)
│   ├── paths.py            # path helpers
│   └── sources/
│       ├── github.py
│       ├── mcp_registry.py
│       ├── mcpmarket.py
│       └── cache.py
├── tests/
│   ├── test_init.py
│   ├── test_interview.py
│   ├── test_suggest.py
│   ├── test_scaffold.py
│   ├── test_heal.py
│   ├── test_hooks_install.py
│   ├── test_hook_runner.py
│   ├── test_persona.py
│   ├── test_prompts.py
│   ├── e2e/
│   │   ├── test_first_install.py
│   │   ├── test_full_init_flow.py
│   │   ├── test_second_run_uses_persona.py
│   │   ├── test_anecdote_appended.py
│   │   ├── test_manual_heal.py
│   │   ├── test_auto_heal_on_init.py
│   │   └── test_uninstall.py
│   └── sources/
│       ├── test_github.py
│       ├── test_mcp_registry.py
│       ├── test_mcpmarket.py
│       └── test_cache.py
└── evals/
    └── persona_heal_quality.yaml
```

### Stdlib-only convention (matches compathy)

No external dependencies. Use:
- `urllib` for HTTP (no requests)
- `html.parser` for scraping (no BeautifulSoup)
- `json` for serialization
- `argparse` for CLI
- `concurrent.futures` for parallel source queries
- `fcntl` for flock
- `string.Template` for prompt templates
- `subprocess` for shelling to git, gh CLI, compathy scripts

---

## Test coverage diagram

(Greenfield; every path is a GAP until written alongside feature code.)

```
CODE PATHS (47 unit) — all GAP, all to be written in v1
═══════════════════════════════════════════════════════
[+] init.py                      5 paths (archetype branches, cancel, rerun)
[+] interview.py                 4 paths (S1 happy, S2 web ok/fail, challenge)
[+] suggest.py                   4 paths (3-source merge, partial fail, warning, ranking)
[+] scaffold.py                  5 paths (new, exists, compathy autoinstall ok/fail, anecdote)
[+] heal.py                      6 paths (lock ok, lock contended, empty, malformed, prose, version)
[+] hooks_install.py             5 paths (create, append, idempotent, decline, uninstall)
[+] hook_runner.py               4 paths (matched, unmatched, jsonl create, IO failure)
[+] sources/github.py            5 paths (gh, token, unauth+cache, 403, 401)
[+] sources/mcp_registry.py      2 paths (success, network error)
[+] sources/mcpmarket.py         6 paths (robots ok/disallow, cache hit/miss, parse fail, UA)
[+] sources/cache.py             3 paths (within TTL, expired, concurrent)
[+] persona.py                   4 paths (missing, malformed, roundtrip, counter)
[+] prompts.py                   3 paths (write, read, template render)

USER FLOWS (8 E2E) — all GAP
══════════════════════════════
[+] First-time install on clean machine
[+] Full 5→3 step init flow [→E2E]
[+] Second run with existing persona uses it [→E2E]
[+] New anecdote appended on subsequent project [→E2E]
[+] Manual /ai-quickstart heal [→E2E]
[+] Auto-heal when /ai-quickstart fires with new anecdotes [→E2E]
[+] /ai-quickstart uninstall [→E2E]
[+] Filesystem-on-iCloud detection warns [→E2E]

LLM EVALS (1 suite, v1) — all GAP
══════════════════════════════════
[+] Persona heal prose quality [→EVAL]
    - Given activity.jsonl + N anecdotes, persona summary preserves stated facts
    - Doesn't hallucinate, stays under 400 words, mentions top_projects[]
    - 10+ test cases, judge prompt = Claude Haiku, advisory in CI for 30 days

DEFERRED to v2
══════════════
[+] Step 2 deeper-interview generation eval
[+] Adversarial prompt files quality eval
[+] Contract tests against compathy's actual scaffold output

─────────────────────────────────────────────────────
COVERAGE: 0/56 paths tested (greenfield, all GAPs)
GAPS: 56 to write — 47 unit, 8 E2E, 1 eval suite
─────────────────────────────────────────────────────
```

---

## Failure modes

| Codepath | Realistic prod failure | Test? | Error handling? | User experience |
|----------|------------------------|-------|-----------------|-----------------|
| init.py archetype branch | User picks none, hits Ctrl-C | GAP | Yes (clean exit) | Clear: "Cancelled, no changes made" |
| interview.py Step 1 progressive | Web search times out mid-Q | GAP | Yes (continue without research) | Notice: "Skipping research, using your answers as-is" |
| suggest.py 3-source merge | All 3 sources fail | GAP | Yes (return curated mapping only) | Warning: "Live data unavailable, showing curated suggestions" |
| scaffold.py compathy autoinstall | git clone fails (no network) | GAP | Yes (fail loudly with retry hint) | Clear error: "Cannot reach github.com. Retry or install manually." |
| heal.py lock contention | Two parallel /ai-quickstart heal | GAP | Yes (fail-fast) | Clear: "Heal in progress in another session, retry in a minute" |
| heal.py malformed activity.jsonl line | Disk corruption / partial write | GAP | Yes (skip + warn) | Visible: "Skipped 1 malformed activity entry" |
| heal.py LLM rewrite failure | API outage mid-rewrite | GAP | Yes (release lock, log, restore .bak) | Clear: "Heal failed, persona unchanged. See heal-errors.jsonl" |
| hooks_install.py settings.json corrupt | User has malformed settings | GAP | Yes (refuse to install) | Clear: "Your settings.json doesn't parse. Fix it first." |
| hook_runner.py disk full | Edit/Write hook tries to append | GAP | Yes (silently no-op, never crash Claude) | Invisible (correct: hook must NEVER block Claude Code) |
| github.py 403 rate limit | Hourly cap hit | GAP | Yes (fall back to cache) | Subtle: "Using cached GitHub data (older than 1h)" |
| mcpmarket.py HTML changes | Selectors fail | GAP | Yes (return empty + warn) | Warning: "mcpmarket.com results unavailable; check for ai-quickstart update" |
| persona.py malformed frontmatter | User hand-edited persona | GAP | Yes (log + use defaults) | Warning: "persona.md frontmatter unreadable, regenerated from anecdotes" |

**Critical gaps (no test + no error handling + would be silent):** none after this plan. Every path either has a test planned, an error path, OR is intentionally a no-op (hook on disk-full).

---

## What already exists (don't rebuild)

| Existing thing | Where | How ai-quickstart uses it |
|----------------|-------|---------------------------|
| compathy scaffold/ingest/lint | `/Users/bk/Code/compathy/scripts/` | Soft dep + auto-install. Step 3 shells out. NEVER vendored. |
| compathy update.py self-update pattern | `compathy/scripts/update.py` | Replicated for ai-quickstart's own self-update. |
| compathy's flat-YAML parser | `compathy/scripts/lint.py` | Pattern reused for ai-quickstart's `personas.yaml` parsing (or use stdlib json instead — simpler). |
| MCP registry MCP tool | `mcp__mcp-registry__search_mcp_registry` (already in env) | Step 2 source for MCP server suggestions. |
| Claude Code hook system | `~/.claude/settings.json` PostToolUse | Subroutine subsystem. |
| `gh` CLI | If user has it installed | GitHub auth tier 1. |

---

## NOT in scope (deferred to v2 with rationale)

| Item | Why deferred |
|------|--------------|
| `/next-project` subskill | Persona collection is in v1; consumption can wait. Defer payoff to when v1 has real users. |
| SaaS-vs-OSS alternative engine | Real research effort per persona; v1 uses curated mapping. |
| Codex + Antigravity skill compatibility | Single SKILL.md works in both, but testing in two runtimes is real cost. v2. |
| Auto-heal threshold (>N new entries triggers heal) | v1 has manual heal + auto-on-init. Threshold is a refinement, not a blocker. |
| Step-2 deeper-interview eval suite | LLM-quality eval of generated interviews. Costly to maintain, low signal in week 1. |
| Adversarial-prompt-files eval suite | Same reason. |
| Contract tests against compathy's actual scaffold output | Compathy's surface is small; manual verification fine for v1. v2 if compathy churns. |
| Persona diff/review UX as web view | v1 prints unified diff to terminal. Web view is polish, not load-bearing. |

---

## Accepted risks (will bite — flagging)

1. **mcpmarket.com scraping fragility.** Their HTML changes silently break the parser. v1 mitigation: graceful empty-with-warning. **You will hit this.** Plan a 2-hour selector-update ritual every few months until they ship an API.

2. **Curated mapping `personas.yaml` becomes stale.** The whole "curation > live search" v1 bet rests on this file being maintained. Risk: it goes 6 months without updates and recommendations get worse than live search would have been. Mitigation: a TODO to revisit cadence at v2.

3. **flock unreliable on iCloud/NFS-synced home dirs.** Phase 0 warns; some users will ignore the warning. Mitigation: fail fast with clear message if heal detects a corrupted prior run.

4. **Persona prose drift.** Over many heals, the LLM-generated prose can drift from what the user actually said. Mitigation: heal shows a diff. v2 could add a "lock this paragraph" mechanism.

5. **GitHub API quota for unauthenticated users.** Even with caching, very active users on 60 req/h limit will see degraded freshness. Mitigation: nudge to set GITHUB_TOKEN; if `gh` is installed, this is moot.

---

## Worktree parallelization strategy

| Step | Modules touched | Depends on |
|------|----------------|------------|
| A: SKILL.md + Phase 0 + paths.py | `SKILL.md`, `scripts/paths.py`, `scripts/update.py` | — |
| B: persona module | `scripts/persona.py`, `templates/persona.md.tmpl`, `tests/test_persona.py` | — |
| C: prompts module | `scripts/prompts.py`, `templates/prompts/`, `tests/test_prompts.py` | — |
| D: sources/cache | `scripts/sources/cache.py`, `tests/sources/test_cache.py` | — |
| E: sources/github + sources/mcp_registry + sources/mcpmarket | `scripts/sources/`, `tests/sources/` | D (cache) |
| F: hooks_install + hook_runner | `scripts/hooks_install.py`, `scripts/hook_runner.py`, `tests/test_hooks_*.py` | — |
| G: heal | `scripts/heal.py`, `tests/test_heal.py` | B (persona) |
| H: interview + suggest | `scripts/interview.py`, `scripts/suggest.py`, `tests/test_interview.py`, `tests/test_suggest.py` | C (prompts), E (sources) |
| I: scaffold | `scripts/scaffold.py`, `tests/test_scaffold.py` | B (persona), F (hooks for marker append) |
| J: init orchestrator | `scripts/init.py`, `tests/test_init.py` | H, I |
| K: E2E suite | `tests/e2e/` | A through J |
| L: persona heal eval | `evals/persona_heal_quality.yaml` | G |

**Lanes (parallel):**

```
Lane 1: A (Phase 0 + paths)            ──┐
Lane 2: B (persona)        ──── G (heal) │── J (init)── K (E2E)
Lane 3: C (prompts)        ───┐          │── L (eval)
Lane 4: D (cache)── E (sources)──── H ───┤
Lane 5: F (hooks)             ──────── I ┘
```

**Execution order:**
- **Wave 1 (parallel worktrees):** A, B, C, D, F. Five lanes, no shared modules.
- **Wave 2 (parallel):** E (after D), G (after B). Merge as they finish.
- **Wave 3 (parallel):** H (after C+E), I (after B+F). Merge.
- **Wave 4 (sequential):** J (after H+I), then K and L can run together.

**Conflict flags:** B + I both touch `persona.py`-adjacent code (I writes anecdotes, B owns the persona module proper). Coordinate via clear interface: B exposes `append_anecdote(project_slug, content)` so I imports it rather than duplicating logic.

---

## Completion summary

```
+====================================================================+
|         ENG PLAN REVIEW — COMPLETION SUMMARY                       |
+====================================================================+
| Step 0  (Scope challenge)   | scope reduced from 8+ subsystems    |
|                             | to 3-step + persona + hooks v1       |
| Architecture review         | 4 issues found, 4 resolved          |
| Code quality review         | 1 issue found, 1 resolved           |
| Test review                 | full coverage diagram (47U+8E+1eval)|
|                             | 1 issue (eval scope), resolved      |
| Performance review          | 1 issue (jsonl growth), resolved    |
| Outside voice               | ran (Claude subagent), 8 critiques  |
|                             | 4 reversals adopted, 1 risk accepted|
| Cross-model tensions        | 4/4 resolved                        |
+--------------------------------------------------------------------+
| NOT in scope                | written (8 items, deferred to v2)   |
| What already exists         | written (6 reuses identified)       |
| Failure modes               | written (12 paths, 0 critical gaps) |
| Worktree parallelization    | 5 lanes Wave 1, 2 Wave 2, 2 Wave 3  |
| Accepted risks              | written (5 explicit)                |
+--------------------------------------------------------------------+
| Lake Score                  | 9/10 recommendations chose complete |
|                             | (only mcpmarket scope was a stretch)|
+====================================================================+
```

### Decisions made (added to plan)
1. Runtime: Claude Code skill, GitHub-hosted
2. Persona: purpose-built event-log→summary (not compathy structure)
3. Subroutine: Claude Code hooks via settings.json with full hardening package
4. Suggestion sources: curated mapping + live freshness from GitHub + MCP registry + scraped mcpmarket
5. Interview flow: 3 steps (collapsed from 5)
6. Compathy dep: soft + auto-install + SHA-pinned
7. Persona schema: structured frontmatter + prose
8. Heal concurrency: flock-protected
9. mcpmarket: cache + graceful degradation (accepted risk)
10. GitHub auth: 3-tier (gh → token → unauth+cache)
11. activity.jsonl rotation: weekly + monthly aggregate
12. Hook hardening: external manifest + bash one-liner + spec'd uninstall + version detection + SHA pin
13. Prompt files: LLM input + audit (kept, not theater per outside voice — they're cheap)
14. Persona scope, schema, heal trigger: as specced
15. Marker file: centralized at `~/.ai-quickstart/managed-projects.json`
16. Persona heal: shows diff + atomic backup
17. Heal failure telemetry: `heal-errors.jsonl`
18. activity.jsonl line invariant: ≤4096 bytes (POSIX atomic append)
19. flock-on-NFS warning: Phase 0 detects sync'd FS

### Deferred (to v2 with rationale)
1. /next-project subskill
2. SaaS-vs-OSS alternative engine
3. Codex + Antigravity compatibility
4. Auto-heal threshold
5. Interview + adversarial prompt eval suites
6. Contract tests against compathy
7. Persona diff/review web UX

### Unresolved decisions
None. Every AskUserQuestion was answered.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 7 issues, 0 critical gaps, scope reduced 8→3 subsystems |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | n/a | exited early — no visual UI scope (CLI/skill) |
| Outside Voice | (Claude subagent) | Independent plan challenge | 1 | issues_found | 4 reversals adopted (persona shape, 3-step flow, hook hardening, marker file location), 1 risk accepted (mcpmarket scrape) |

- **CROSS-MODEL:** Outside voice raised 8 critiques. 4 reversed primary review decisions and were adopted into the locked spec. 1 (mcpmarket scrape) was disputed and the user accepted the risk explicitly. 3 (compathy SHA pin, marker file git-leak, persona backup/restore) were absorbed as v1 spec additions.
- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to begin implementation. CEO review optional (the outside voice already pressure-tested scope structurally).
