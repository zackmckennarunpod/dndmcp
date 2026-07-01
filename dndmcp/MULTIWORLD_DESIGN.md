# Multi-world design — spec, not yet built

Today: one process = one `World()` = one SQLite file (`DNDMCP_STATE_DIR/campaign.db`) = one
shared campaign. Every player joins the same world; `start_adventure` "joins if a campaign
row exists, else creates one." This doc is about letting multiple independent worlds exist
(a friend group runs their own gothic-horror crawl, someone else spins up a fresh one),
while new players joining a given world still land at that world's single spawn point
(already solved — `campaign.start_room` is per-campaign).

**Explicitly out of scope here** (deferred per conversation): live multiplayer
position/presence sync. That's a separate, harder feature (needs a "tick" broadcast or
polling delta, and real thought about how crowded a room gets) — this doc only covers
"multiple separate worlds exist and you can create/join one," not "watch other players move
in real time within one world" (the map already shows other players' *current* room via
`/state`'s `players` list — that part already works, it just refreshes on the 1.5s poll like
everything else, not push).

## Storage model: one SQLite file per world (not one DB with a `world_id` column)

Recommended over a shared multi-tenant DB because:
- `state.py`'s existing schema/queries need **zero changes** — every table, every query stays
  exactly as-is. Only `World.__init__`'s path resolution changes.
- `SCHEMA_VERSION`'s wipe-on-mismatch behavior (`state.py:35`) already assumes "this file is
  disposable, whole-file operations are fine" — that philosophy extends naturally to
  per-world files (delete a world = `rm` its file).
- The alternative (single DB, `world_id` column on every table) means auditing every query
  in `state.py` for a `WHERE world_id=?` clause — one missed clause silently leaks data
  across worlds. Not worth it at this scale (SQLite, dev tool, no concurrent-write pressure
  that would push toward one DB).

Path layout: `DNDMCP_STATE_DIR/worlds/<world_id>.db`. The existing
`DNDMCP_STATE_DIR/campaign.db` becomes the `world_id="default"` world's file — no migration
needed, just special-case `"default"` to keep using the old flat path so today's save isn't
orphaned.

## The real snag: tool calls don't carry `world_id`

`look(player_id)`, `move(player_id, direction)`, `attack(...)`, `pick_up_item(...)` etc. all
take `player_id` only, and reach through the single global `world = World()`
(`server.py:48`). Two ways to fix this:

1. **Add `world_id` to every tool's signature.** Rejected — breaking change to ~10 tools,
   and it makes the calling agent responsible for remembering/threading a value it has no
   natural reason to track once past `start_adventure`.
2. **A small player→world registry, looked up by `player_id`.** `start_adventure` writes
   `player_id → world_id` once. Every other tool does `world = _world_for(player_id)` instead
   of using the bare global — one line changed at the top of each tool, no signature changes.
   **Recommended.**

Registry storage: a tiny flat file, `DNDMCP_STATE_DIR/player_index.json`
(`{"<player_id>": "<world_id>", ...}`) — doesn't need its own SQLite table, it's an
append-mostly lookup with no query needs beyond "get one value by key." `_world_for()` caches
open `World` instances per-process (dict keyed by world_id) so repeated calls don't reopen
the SQLite connection every time.

## `start_adventure`: create and join collapse into one call

Add an optional `world_id: str = "default"` param.
- Omitted → today's behavior exactly (everyone lands in the one shared world, zero breaking
  change for existing links/players).
- An existing `world_id` → joins that world's campaign (same "join if exists" semantics as
  today, just scoped to that file instead of the only file).
- A new `world_id` → creates it (same "else start one" semantics, scoped to that file).

No separate `create_world` / `join_world` tools needed — matches the existing pattern where
the DM agent decides conversationally, not through a form. The agent would ask the player
"shared world, or a new one?" and either omit `world_id` or pass a slug the player picks.

Spawn point is then already correct for free: each world file has its own `campaign` row
with its own `start_room`, exactly like today's single world does.

## `web.py` (GUI)

- `_db()` needs a `world_id` (new `?world=` query param, default `"default"`) to pick which
  file to open, mirroring the existing `?player=` param.
- Add a lightweight world list to the index page: enumerate
  `DNDMCP_STATE_DIR/worlds/*.db` (+ the default file) and render as links
  (`?world=<id>`) — just navigation, not a "create" form. Creating a world happens through
  `start_adventure` (agent-driven), not the browser.
- `flash_calls` and everything else `/state` returns is already correctly scoped once `_db()`
  resolves to the right file — no other changes needed there.

## One thing to confirm with you before I build any of this

You mentioned "mirror it or replicate it" for new worlds — I read that as "spin up a fresh,
**independent** world" (i.e., copy the *schema*, not the data), not literal DB
replication/sync between worlds. If you actually meant something like "start a new world
pre-seeded from an existing one's map," that's a different, bigger feature (needs a
file-copy-then-diverge step) — flag if that's what you meant.

## Effort if approved

- `state.py`: `World.__init__` takes `world_id`, resolves path — small.
- `server.py`: `player_index.json` helper + `_world_for()` + `world_id` param on
  `start_adventure` + swap the global `world` reference for `_world_for(player_id)` at the
  top of each tool — moderate, mechanical, touches every tool function but each change is
  one line.
- `web.py`: `?world=` param + world-list on index — small.
- No DB schema changes, no migration script (per-file model sidesteps that entirely).
