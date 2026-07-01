# DNDMCP — Handoff / Resume Point

**Date:** 2026-07-01, ~00:50 (hackathon day; video due Wed 12pm PST — check current time against this).
Read this first. Then REQUIREMENTS.md / WORLD_SCHEMA.md / JUDGING.md / STRATEGY.md.

## ⭐ THE BIG NEWS: Flash LLM world-gen is WORKING, confirmed live, end to end

This was the critical blocker all session. It's resolved. The working recipe:

- **SDK version matters — a lot.** `runpod-flash==1.7.0` (what `.venv` had) silently discards
  `Endpoint(image=...)` and deploys Flash's own default image instead (a real upstream bug,
  fixed in `runpod/flash` PR #339, merged 2026-05-27). `.venv` was Python 3.14, and no
  `runpod-flash` release above 1.7.0 supports 3.14 — that's why we were stuck on the buggy
  version. **Fix:** rebuilt `.venv` on **Python 3.13** (`.venv-py314-old` is the backup of the
  broken one), `pip install runpod-flash==1.18.0`. Confirmed fix via the new log line Flash
  prints: `"Endpoint 'X': using user-supplied image '...' (overrides Flash runtime image)"`.
- **Image:** `runpod/worker-v1-vllm:v2.22.4` — NOT `runpod/worker-vllm:stable-cuda12.1.0` (that
  Docker Hub repo is abandoned since July 2024, bundles vLLM 0.4.2, predates Qwen2.5 entirely —
  weights load, but generation silently 400s with a blank message). `runpod/worker-v1-vllm` is
  the actively maintained repo (new releases weekly).
- **Must pin `min_cuda_version=CudaVersion.V13_0`** on the `Endpoint(...)`. This image's
  container declares `cuda>=13.0`; without pinning, Flash can schedule the pod onto an
  older-driver host, which fails at container-init (`nvidia-container-cli: unsatisfied
  condition: cuda>=13.0`) — intermittent, host-dependent, looks like a random flake otherwise.
- **Calling convention:** use the documented OpenAI-compatible HTTP route directly —
  `POST https://api.runpod.ai/v2/{endpoint_id}/openai/v1/chat/completions` with a standard
  `{model, messages, max_tokens, temperature}` body. The QB `ep.run({"input": {...}})` job
  format hits an unrelated bug (a blank-message `BadRequestError` from the worker's internal
  `JobInput` routing) — not our bug, don't chase it, just use the HTTP route.
- **Constructing `Endpoint(...)` does NOT deploy it.** Deploy is lazy, triggered by the first
  actual call. `dndmcp/flash_llm.py::ensure()` fires one throwaway `.run({"input": {}})` (its
  error is expected and ignored — it's the known blank-message bug, harmless here since we're
  just forcing deployment) then resolves the real endpoint ID via a `myself { endpoints { id
  name } }` GraphQL query. Locked with an `asyncio.Lock` so concurrent fan-out calls don't race
  to construct duplicate endpoints.
- `dndmcp/flash_llm.py` is fully rewritten to this recipe, no `forge` dependency (bare
  `runpod_flash` + keychain auth). Endpoint name `dnd-llm-vllm`, `workers=(0,3)` (scale-to-zero).
- **Verified live** multiple times: real generated rooms ("The Whispering Crypt," "Tempest
  Tunnels," "Vault of Shadows," etc.), fan-out prefetch generating background rooms, `kind`
  field populated by the model.

## World-gen: facts, not pre-written prose

Reframed per direct feedback: the Flash world-builder returns FACTS (`name, kind, atmosphere`
[one sentence], `feature, has_monster, notable_item`), not a pre-composed scene description.
The DM agent (whoever's running the actual session) narrates FROM those facts, same as a human
DM works from notes — it does NOT just relay fields verbatim. `_compose_look()` (the old
`ahead/left/right/center` directional-prose scheme) is gone. `dndmcp/server.py`'s `DM_PERSONA`
says this explicitly now.

## Graph model: real edges table, not a JSON blob

Replaced `rooms.exits` (a JSON column on the room row) with a **generic `edges` table**
(`from_type, from_id, to_type, to_id, edge_type, metadata`) — same pattern as the Context DB's
own `edges` table, and what `WORLD_SCHEMA.md` actually specified from the start ("a generic
edges table for relationships"). Enables reverse lookups (`edges_to()`) that a JSON blob never
could. `Room.exits` stays the same `dict[str,str]` shape at the model/API level — only the
persistence changed (`state.py`: `set_edges()`, `edges_from()`, `edges_to()`, `room_exits()`).
`SCHEMA_VERSION` bumped to 4 (dev-mode: version mismatch = wipe and recreate — see ⚠️ below).

**Direction vocabulary is no longer cardinal-only.** `game.DIRECTIONS` now includes `up`/`down`
with proper opposites, plus `game.opposite_of()` for a generic `"back"` fallback on any future
free-form label (e.g. "through the broken wall") that has no natural single-word opposite.
Vertical continuity: a `down`/`up` passage has a 50% chance of continuing the same way (the
cellar keeps going down, not just dead-ending sideways).

**Fixed a real bidirectional-linking bug**: the procedural generator computes its own back-exit
id as `f"{new_room_id}:{opposite_direction}"`, which is NOT the actual origin room's id —
walking back the way you came used to silently generate a *duplicate* room instead of
returning you home. `server.py::_generate_and_link()` now force-corrects this.

**Speculative fan-out prefetch (R9) is built**: `_prefetch_frontier()` generates every
still-unlinked exit of a room in parallel, fire-and-forget, the moment you enter it — verified
generating 3 background rooms beyond what was actually visited in one rehearsal.

**Nearby-region context**: `_nearby_region()` (pure BFS over already-generated rooms, depth 2,
no extra LLM calls) feeds a compact `(name, kind)` list into the next room's prompt for tonal
continuity, so a "cellar" region doesn't randomly neighbor a "sunlit garden."

**Known-vs-unknown exits** are exposed to the DM agent (`_adjacent_rooms()`): an exit either
names an already-generated room ("known, not yet visited" / "visited") or says "unexplored —
do not invent what's there." Prevents the DM from hallucinating detail about unlinked exits.

## Domain events + self-managed agent memory

`log` table now has `player_id` + dotted-namespace `kind` (`adventure.started`, `player.moved`,
`room.generated`, `combat.resolved`, `memory.noted`, `item.picked_up`). `recent_log()` filters
by player and/or kind prefix. New tool **`remember(player_id, note)`** — the DM's own
self-managed free-form continuity memory (an NPC's real motive, a lie told, anything not
captured by the rigid mechanical schema) — separate on purpose from room/character state, which
stays rigid because game mechanics need one source of truth.

## Web GUI (`dndmcp/web.py`) — rewritten, D3-based, several real bugs found & fixed

- Old map used coordinate math from cardinal-direction deltas only — broke completely once
  `up`/`down`/free-form exits existed (silently overlapped at the same coordinate). Replaced
  with a real **D3.js force-directed graph** (matches the Context DB's own graph-viz approach).
- **Perf bug, fixed:** the first hand-rolled version fully re-ran 120 iterations of O(n²)
  physics on every 1.5s poll regardless of whether anything changed — permanent low-grade
  jitter, never settled. Now only reheats (`alpha().restart()`) when the room/edge set actually
  changes.
- **Correctness bug, fixed:** d3-force only resolves link `source`/`target` strings into
  positioned node objects when `.force('link').links(...)` is called, and only *paints* those
  positions onto the SVG when `ticked()` fires — which only happens while the simulation is
  actively running. Once it settles (alpha near zero, timer stopped), data was correctly
  resolving but never getting painted, so every line went invisible ~1s after first render.
  Fix: call `ticked()` manually once per render regardless of whether the sim is running.
- Fixed inventory rendering (`[object Object]` — items are `{name, description}` dicts now, not
  bare strings), a stuck "no adventure yet" placeholder overlapping the real graph, and added a
  live "⚡ N Flash calls" header counter (counts `room.generated` log rows containing `(flash)`).
- **Debugging gotcha, hard-won tonight:** `pkill -f "dndmcp.web.*8099"` does NOT match a
  process started as `GUI_PORT=8099 python -m dndmcp.web` — env var prefixes aren't part of the
  process's argv, so the pattern never matches. Every "restart and verify" using that pattern
  was silently no-op'ing, leaving the OLD process bound to the port and invalidating the test.
  **Always verify by PID** (`lsof -i :<port>` → `ps -p <pid> -o lstart`) that a process actually
  restarted before trusting any "fix confirmed" result.

## ⚠️ UNRESOLVED — your live game session is on stale code

The actual long-running MCP server (Claude Desktop or similar, been running all session) and
its web GUI (port 8001) were started **before** the edges-table migration and most of tonight's
fixes. `~/.dndmcp/campaign.db` still has the OLD schema (no `edges` table, has the old `exits`
JSON column) — confirmed by direct inspection. Restarting to pick up the fixes will trigger the
dev-mode "schema version mismatch → wipe and recreate" behavior, **losing the current live
campaign's progress**. Was mid-conversation on this when handoff happened — resolve with the
user before restarting anything on port 8001 / the live server process.

## Cleanup still pending
- Two vLLM endpoints exist: `dnd-vllm4` (debug leftover from tonight, safe to delete) and
  `dnd-llm-vllm` (the real one — leave it, scale-to-zero).
- Procedural "texture" features repeat noticeably across rooms (small fixed pool of ~8 per
  theme in `game.py::_THEMES`, sampled with replacement across many rooms) — flagged by
  the user, not yet fixed. Straightforward fix: bigger pool, or sample-without-replacement
  per campaign.
- `game.ascii_map()`'s tiny terminal compass diagram still only understands cardinal
  directions (cosmetic — the "Exits:" text list right below it is fully accurate regardless).
  Low priority, deferred on purpose.

## Still open from earlier in the session (never resolved)
- **stdio-vs-pod-hosted architecture contradiction**: `BUILD.md` locks "stdio only, no pod for
  MVP," but the actual pitch ("copy one line, add the MCP server, play from anywhere") needs
  pod-hosted HTTP. `Dockerfile`/`dndmcp/app.py` already support pod-hosted; this is a decision
  to lock, not a build task.
- R11 (install instructions for Claude Desktop) — not written.
- **R12 — the actual video. Not started. Due Wednesday 12pm PST.** This is the real deliverable;
  everything else has been in service of having something real to show.

## How to resume
- `.venv` is now Python 3.13 + `runpod-flash==1.18.0` (was 1.7.0/Python 3.14 — backed up at
  `.venv-py314-old`). Auth: keychain `runpod-api-key-prod`.
- Play locally: `DND_FLASH_LLM=1 DNDMCP_STATE_DIR=~/.dndmcp_dev .venv/bin/python -m dndmcp.web`
  (GUI :8001) + Claude Desktop stdio config, OR drive `dndmcp.server`'s functions directly in a
  script for fast iteration (that's how tonight's rehearsals worked, no MCP client needed).
- `DND_FLASH_LLM=1` to enable live generation (off by default → procedural fallback, game
  always works either way).
- Before touching the live game process: resolve the "stale process / campaign wipe" question
  above with the user first.
