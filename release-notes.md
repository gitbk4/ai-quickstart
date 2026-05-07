# v0.2.0: Persona as portable identity (Waves 1 + 2)

ai-quickstart's persona is no longer a private terminal-only artifact. This
release ships the architectural foundation for personas as a queryable,
shareable identity layer for AI tooling, plus the alternatives engine and
trust/provenance scoring that make Step 2 suggestions actually useful.

Stdlib-only. No new dependencies. v1 flow unchanged; everything below is
additive.

## What's new (user-facing)

- **`persona.json` alongside `persona.md`.** Same prose, machine-readable.
  Every paragraph has a stable `p:NNN` id (HTML-comment markers in the `.md`)
  with hash-fallback recovery if you hand-edit the file. Other tools
  (including compathy) can now read your persona at scaffold time.
- **Local `persona_query` HTTP endpoint.** `python3 scripts/dashboard/server.py`
  starts a stdlib `http.server` on a free localhost port and exposes
  `GET /persona/current`, `GET /persona/p/{id}`. Returns the persona with a
  `stale: true` flag if a heal is in progress. Other Claude Code skills can
  query this without an SDK.
- **Dashboard skeleton at `/dashboard/`.** Visible scaffold + a fetch demo
  proving the server wiring. Full panes (persona-prose, structured-fields,
  diff-review, activity-timeline, suggestions) ship in a follow-up release.
- **Alternatives engine.** Step 2 suggestions now surface 1–2 alternatives per
  recommendation across SaaS / OSS / Claude skill / MCP server / agent
  platform. 26 tags curated; covers the engineering, marketing, data,
  nonprofit, and general archetypes. Each alternative includes a one-line
  "why this for you" referencing your persona when present.
- **Trust + provenance scoring on every suggestion AND every persona
  paragraph.** Trust score 1–5, deterministic at render time; ANSI-colored
  badges in the terminal, HTML spans for the dashboard. Provenance tags:
  `pinned` / `anecdote` / `heal` / `activity-inferred` / `multi-hop` for
  paragraphs; `curated` / `live-registry` / `inferred` / `community` for
  suggestions.
- **Compathy persona-aware scaffolding** (requires the matching compathy
  release). New compathy projects automatically get an `entities/builder.md`
  page recording who built them and a `patterns/style.md` seeded from
  high-trust persona paragraphs (when a persona is present). Compathy stays
  fully usable without ai-quickstart (graceful degradation by design).

## Under the hood

- **Telemetry foundation** with 8 event types (`persona.heal.*`,
  `persona.lock.*`, `suggestion.*`, `dashboard.*`). Privacy-first: opt-in for
  remote aggregation, anonymous per-install ID derived from random bytes
  never tied to user identity. Local `activity.jsonl` always records
  regardless. 4096-byte line invariant for POSIX-atomic appends. Total-disk
  cap with drop-oldest-on-overflow when the remote endpoint is unreachable.
- **Auto-heal threshold** trigger from the hook runner. Heal fires
  automatically when ≥ N new entries accumulate since the last heal.
- **Lock-this-paragraph mechanism.** Wrap any persona paragraph in
  `<!-- lock:start --> ... <!-- lock:end -->` markers and heal will preserve
  it byte-for-byte. Useful for quotes, identity claims, or anything you
  don't want a future LLM to paraphrase.
- **Combined `ThreadingHTTPServer`** for `/persona/*` + `/dashboard/*` with
  thread-pool cap, per-request 30s timeout, and `flock`-based port-file
  discovery so two parallel starts can't double-bind.

## Known limits

- Dashboard panes 2–6 are not in this release (skeleton + sanity-check fetch
  only). The terminal-side trust badges + alternatives are the floor; the
  dashboard rich version comes next.
- Community persona-template directory is gated on telemetry showing real
  usage and ships in a separate release.
- The compathy `lint` xfail (`index-stale: log` on a fresh scaffold) is an
  upstream signal we left intact; it does not affect functionality.

## Test stats

- 729 passed + 1 xfailed in `tests/`
- 8 new modules, 7 new test modules, 26 alternatives tags

## Compatibility

- Backwards-compatible with v0.1.x personas. The migration is one-shot,
  idempotent, and creates a `.bak` before any mutation.
- Stdlib-only: requires Python 3.9+, no new packages.

## Acknowledgments

Built across 8 worktree-isolated lanes (1A–1D, 2A–2B, 2.5) plus a compressed
maintainer dogfood that caught and fixed 6 real integration issues before
this release.
