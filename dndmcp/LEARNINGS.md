# DNDMCP — running log of gotchas, gaps, and decisions

Append as we go. Newest at the top. This is for things worth remembering later — not a
changelog (git has that), a "why did we do it this way / what bit us" log.

---

## Exits are labeled by compass direction only — no description of what they look like
`room.exits` is just `{"north": "room_id", ...}` — a direction label, nothing narrative. The
DM agent has been literally relaying "Exits: north → unexplored" verbatim instead of
describing an actual doorway/archway/crack-in-the-wall, even though `DM_PERSONA` explicitly
says not to just relay fields verbatim. Two fixes, not mutually exclusive: (1) the narrating
agent should just invent exit flavor in the moment (no code change needed, purely a narration
discipline fix); (2) `worldgen.generate_room_content`'s Flash prompt could ask for a short
description per exit (not just direction), giving future sessions/other DM agents consistent
narrative material instead of relying on each agent to improvise it. Neither built yet —
(1) is free and should just start happening now.

---

## Flash LLM is proven to work, but not wired into the game yet
A real request against the deployed vLLM endpoint (`dnd-llm-vllm` / `v3jt3do91xcm9s`)
returned genuine, coherent, parseable JSON matching `worldgen.py`'s room schema — confirmed
by running the actual extraction logic (`gen[gen.find("{"):gen.rfind("}")+1]`) against the
real response. But `flash_llm.py` (what `worldgen.py`/`pick_up_item` actually call) mints its
OWN separate endpoint (`dnd-llm`, raw `transformers` handler via `forge.mint`) — a different,
untested code path from the one that's confirmed working. Everything played tonight has been
on the procedural fallback. Repointing `flash_llm.py` at the working endpoint is a known,
scoped, not-yet-done task.

## Models don't reliably follow the JSON schema exactly
`notable_item` came back as `{"description": ..., "value": "$50"}` (a nested object) instead
of the plain string the schema asked for. `worldgen.py` already has defensive normalization
for this (`if isinstance(item, dict): item = item.get("description") or item.get("name") or
...`) — written anticipating exactly this. Don't assume a model's structured output will match
its instructions; always have a fallback extraction path, not just a bare `json.loads`.

## No NPC/monster disposition or persona system exists — future direction: entity-scoped domain events
Monsters/NPCs are flat stat dicts in `room.contents` — no persistent identity, no memory, no
"hostile → neutral" state transition. A creative player action (offering an item to a hostile
monster, sitting with it in grief) can be narrated as a strong beat and recorded via
`remember()`, but nothing in the actual game state changes — the monster is still flatly
"hostile" for the next `attack` call, and the note isn't attached to anything. This is the same
gap `WORLD_SCHEMA.md` flagged as the real priority ("entity as first-class... needed for
ask_npc + living NPCs").

Shape for when this gets built (refined during design discussion, not yet started): monsters
get a stable entity id the first time they're generated (not a disposable dict), and
interactions become domain events scoped to that entity as the aggregate — not a flat
session/player log. That's what makes it a real SHARED-WORLD feature, not a personal journal:
a second player who later meets the same Ghost reads the same accumulated event history against
that same aggregate, regardless of who they are — "Vesper sat with this Ghost and it didn't
attack" persists and shapes what a totally different player experiences later. Consistency
risk still applies (inferring reaction from event history via Flash won't be as deterministic
as a stored enum), but the aggregate-scoping is the right foundation regardless of whether
disposition itself ends up inferred or stored. Deferred a third time tonight in favor of
higher-priority open items (Flash wiring, pod deploy, video) — this is the concrete starting
point for whoever picks it up next.

## Room name pool is small — expect repeats
Procedural room names come from a ~7-entry list per theme (`game.py`'s `_THEMES`). Once the
`_prefetch_frontier` system started eagerly generating multiple rooms per move, duplicate
names showed up fast ("moonlit cloister" and "flooded ossuary" each appeared twice in one
session). Cosmetic, not a bug — but noticeable in a longer demo. Fix would be widening the
name list or having Flash generate names (which also fixes it, since it's not draw-from-list).

## `pick_up_item`'s procedural fallback is permissive by design
Without Flash, there's no model to judge whether something is actually portable — so the
fallback defaults `portable=True` for anything you describe, including things that shouldn't
be takeable (a stone sarcophagus lid, tested and confirmed pickupable). This is intentional
(keeps the game playable with Flash off) but means the plausibility check is currently a
no-op until Flash is actually wired in.

## Stdio MCP servers don't hot-reload — this caused most of tonight's "confusing bugs"
Every `dndmcp/*.py` edit requires a `/mcp` reconnect (or fresh session) to take effect — the
subprocess is spawned once and keeps running old code otherwise. With multiple Claude Code
sessions/windows open against the same `dndmcp` server, a stale subprocess in one window can
keep reading/writing the shared SQLite file with outdated code while a different window has
already reconnected — producing what looks like "a different bug every time" but is actually
the same root cause. Consolidating to one active session eliminated this.

## Schema changes wipe dev state on purpose
`SCHEMA_VERSION` (in `state.py`) is checked against `PRAGMA user_version` on every open;
mismatch = drop all 4 tables and recreate clean. No bespoke `ALTER TABLE` migrations — every
attempt at those tonight (adding a column, renaming one) produced a new edge-case bug. This is
correct for pre-launch dev data (nothing to preserve) but means every schema-changing edit
resets any in-progress playthrough — including live demo sessions. Worth remembering before
recording video: don't touch schema-affecting code between takes.
