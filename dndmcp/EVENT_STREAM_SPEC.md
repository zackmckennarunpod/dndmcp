# DNDMCP — central event stream (spec, not yet built)

Grew out of a design conversation covering exit immersion, spoiler leaks, ghost trails, and
"can we see Flash calls happen live." All five converge on one thing: `state.py`'s existing
`log` table is already an append-only, aggregate-scoped domain event stream (`kind` is a
dotted namespace — `player.moved`, `room.generated`, `combat.resolved`, `memory.noted` —
`subject_type`/`subject_id` is the aggregate, `player_id` is the actor). No Redis, no new
table, no event-sourcing rearchitecture of `rooms`/`character` — just use what's there more
fully, plus a small amount of new wiring. Status key: 🔲 not started · 🟡 in progress · ✅ done.

---

## 🔲 1. System events: Flash call lifecycle

**What:** `flash.call_started` / `flash.call_completed` (with `duration_ms`, endpoint/model,
`via`) written to the SAME `log` table as everything else — `kind` prefix `flash.*` is what
makes these "system events" vs "world events," not a new column or table.

**Why:** Right now a Flash call is invisible until it's already finished (`room.generated`
logs `via` after the fact — server.py:256). The GPU work — the actual hackathon pitch — never
shows as "happening." Two real call sites today:
- `flash_llm.generate()` (flash_llm.py) — used by `worldgen.py` for room/item/NPC-reply gen
- `inference.py`'s `_flash_reply()` — separate path, calls `runpod_flash.Endpoint` directly for `ask_npc`

**Scope:**
- Thread a logging callback (or bound `world.log`) into both call sites — NOT an import of
  `state.py`/second DB connection from `flash_llm.py`/`inference.py`. `world` stays a single
  instance owned by `server.py` (server.py:70); these lower modules stay decoupled from
  storage, same as today.
- Log `flash.call_started` right before the await, `flash.call_completed` (or
  `flash.call_failed`) right after, with `subject_type`/`subject_id` set to whatever the call
  is for (`room`/`<room_id>`, `npc`/`<npc_id>`) so it's filterable the same way room events are.
- No change to `art.py` for now — it's stubbed/disabled, not worth wiring yet.

## 🔲 2. Exit descriptors (physical, not spoiler)

**What:** A short threshold description ("a warped iron door," "a black stairwell") per exit,
generated once by `worldgen.generate_room_content` alongside name/description/features, stored
in the existing `edges.metadata` column (state.py:76-83, already there, currently unused) —
`set_edges(..., metadata={direction: descriptor})`.

**Why:** Exits currently render as bare compass words ("east → unexplored"). This describes
the doorway itself — part of the CURRENT room, already known — without inventing what's
beyond it. Safe to show even for undiscovered destinations (see #3).

**Scope:**
- `worldgen._room_messages`/`generate_room_content` prompt gains a short per-exit descriptor
  in its response shape.
- `_generate_and_link` (server.py:246) passes `metadata=` through to `world.upsert_room`/
  `set_edges`.
- `_adjacent_rooms` (server.py:146-160) surfaces the descriptor for every exit, known or not.
- `DM_PERSONA` gains an instruction: lead narration with the physical descriptor, don't dump
  the raw `Exits:` list verbatim (it already says "don't just relay fields verbatim" but this
  makes it explicit for exits specifically).

## 🔲 3. Per-player discovery (fixes a real spoiler leak)

**What:** A new edge type, `character --discovered--> room`, written the moment a player
actually arrives somewhere (same spot `move()` already calls `world.mark_visited`).
`_adjacent_rooms` stops treating "room exists in DB" as "known, name it" and instead checks
"has *this player* discovered it."

**Why (bug, not just polish):** `_prefetch_frontier` (server.py:259-269) world-builds every
exit's destination room in the background after every move, so it exists in the DB almost
immediately — well before the player has looked through that doorway. `_adjacent_rooms`
currently reveals the real room name the instant it's generated, to every player, regardless
of whether they've been there. In a shared multiplayer world this also means `Room.visited`
(models.py:51, a single global bool) shows "(visited)" to a player who's personally never set
foot in that room, because someone else has.

**Scope:**
- `World.discover(player_id, room_id)` — insert a `character`→`room` `discovered` edge (reuse
  `edges` table, no schema change).
- `World.has_discovered(player_id, room_id) -> bool`.
- Call `world.discover` in `move()` (and for the start room in `start_adventure`) at the same
  point `mark_visited` fires today.
- `_adjacent_rooms`: reveal `dest.name`/visited-status only if `has_discovered(player_id,
  dest_id)`; otherwise show only the exit descriptor from #2 (physical, safe) and no name.
- `Room.visited` can stay as a "has anyone, ever" stat for other purposes but stops driving
  per-player narration.

## 🟡 4. Filterable live feed — transport decided: SSE, not polling

**Revised in conversation:** built and deployed as SSE (`web.py` `/stream/events`,
`sse_starlette.EventSourceResponse`), not polling. `sse_starlette` was already a transitive
dep via `mcp` (which uses SSE for its own streamable-http transport), so this is zero new
infra, not new infra risk. The 421/DNS-rebinding issue this section originally cited as the
reason to avoid SSE was a Host-header validation bug in `mcp`'s FastMCP (fixed in `daea618`)
— unrelated to SSE as a mechanism, and would have broken plain HTTP polling through that same
host-check too. Verified working through the pod's proxy.

**What's live now:** an unfiltered global feed — every player, every session, no filter —
which is the actual demo centerpiece (the stigmergic "watch the world remember itself"
moment). The filter axis below (`player_id`/`room_id`/`kind_prefix`) is still valuable as a
*separate* capability layered on top, not yet built.

Original spec (filters, not yet built):

**What:** A `web.py` endpoint reading `log` with filters: `player_id` (one user's events),
`subject_type`+`subject_id` (one room's events — what happened here), `kind` prefix
(`flash.*` = system/GPU vs everything else = world/narrative). "Filter by world" needs no new
filter at all — per `MULTIWORLD_DESIGN.md`, each world is its own SQLite file, so a world's
`log` table only ever contains that world's events by construction.

**Why:** This is the actual "watch Flash generate a room live" + "see what happened in this
room" + "see what this player has been doing" surface — one query shape, three use cases,
zero new storage.

**Scope:**
- New `recent_log`-backed endpoint in `web.py`, params: `player_id?`, `room_id?`, `kind_prefix?`.
- GUI panel: a scrolling feed, filter toggle (world/system/mine), reusing the existing poll
  loop (`setInterval(tick, 1500)`, web.py:201) — tightened to ~500ms so Flash calls (a few
  hundred ms to a couple seconds) visibly show `call_started` then `call_completed` instead of
  only ever appearing as done.
- Explicitly NOT WebSocket/SSE/Redis — poll-on-existing-DB is enough for a turn-based game and
  avoids new infra risk through the pod's proxy (which has already broken once — see the 421/
  DNS-rebinding fix, `daea618`).

## ✅ 5. Request provenance + a metrics page

**What:** `log` gains two nullable columns, `ip` and `session_id`, populated for every new
event via a `contextvars.ContextVar` pair (`state.py`'s `request_context()`) that the
transport layer sets for the duration of one inbound request — a single choke point, so
none of the ~15 existing `world.log(...)` call sites in `server.py` needed to change.
`web.py`'s `/metrics` page (linked from the header, same click-to-new-tab pattern as
`#flashcount` → `/flash-calls`) surfaces counters computed from `log`/`character` via
aggregate queries: total events, unique players, unique IPs, Flash-call count, a breakdown
by `kind`, hourly activity for the last 24h, and a per-player table (name/class, event
count, last-seen IP, last-seen time).

**Why:** Hackathon-demo surface — "how much is actually happening in this world, and who's
in it" as visible counters, not just a raw scrolling feed.

**How IP is captured:**
- **Web GUI** (`web.py`): `_client_ip(request)` reads `X-Forwarded-For` first (the pod sits
  behind Runpod's proxy, so `request.client.host` alone is the proxy, not the caller),
  falling back to `request.client.host`.
- **MCP tool calls** (`server.py` — the actual gameplay traffic): `FastMCP.run(transport=...)`
  builds+serves its Starlette app in one call with no middleware hook, so `main()` now calls
  a new `_run_http()` that replicates `run_streamable_http_async`/`run_sse_async`'s two
  internal lines but wraps a pure-ASGI `_RequestContextMiddleware` around the app first. Same
  XFF-first resolution as the web side, plus captures the inbound `Mcp-Session-Id` header.
  Deliberately NOT `Starlette.BaseHTTPMiddleware` (buffers the body, can break
  streamable-http's long-lived SSE responses).

**Not done:** no new "user" table/auth — `player_id` (self-minted) stays the identity, IP is
just a signal riding on `log` rows, computed on read.

## Deploy note

None of the above adds/renames/removes a column or table — new `kind` values, a new edge
`edge_type`, and populating the already-existing `edges.metadata` column are all just new
*rows*, not schema changes. `SCHEMA_VERSION` (state.py:32) does NOT need a bump, so
`scripts/redeploy_pod.sh` (git pull + restart `dndmcp.app`) will NOT trigger the wipe-on-
mismatch path (state.py:47-53) — the shared campaign and every player's progress survive a
redeploy of this work.

## Explicitly out of scope here

- Ghost-trail *rendering* (fading trails along edges in the GUI) — frontend-only, depends on
  #4's feed but is its own pass.
- Full event-sourcing (deriving `rooms`/`character` state purely by replaying `log`, no
  mutable snapshot) — real rearchitecture, real risk, not needed to get any of the above.
- Redis / any new service — see #4.
