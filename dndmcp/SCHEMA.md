# DNDMCP — actual DB shape

This is the CONCRETE current schema (what's really in `~/.dndmcp_dev/campaign.db` today).
For the aspirational graph design (entity/item/quest/lore as first-class nodes, not built
yet), see `../WORLD_SCHEMA.md` instead — that's the target, this is the reality.

SQLite, one file, 4 tables. Version-guarded: `state.py`'s `SCHEMA_VERSION` constant is
compared against `PRAGMA user_version` on every open — mismatch wipes all 4 tables clean
and recreates them. **Bump `SCHEMA_VERSION` on any column/table change** — don't write a
one-off `ALTER TABLE` migration, that's exactly what caused tonight's bugs (partial patches
that didn't cover every edge case). This is dev-only throwaway state; wiping is correct.

## Tables

### `campaign` (singleton, id=1 — the shared world's metadata)
| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | always `1` |
| `theme` | TEXT | e.g. "gothic horror" — set once, at world creation |
| `premise` | TEXT | the opening narration line |
| `created_at` | REAL | unix timestamp |
| `start_room` | TEXT | room id new players spawn at |
| `turn` | INTEGER | increments on every `set_location` call (any player moving) |

→ Pydantic model: `Campaign` (`models.py`), `extra="forbid"`.

### `character` (one row per `player_id` — multiplayer)
| column | type | notes |
|---|---|---|
| `player_id` | TEXT PK | caller-supplied, minted by `start_adventure` (`uuid4().hex[:12]`) |
| `name`, `klass` | TEXT | |
| `level` | INTEGER | default 1, not currently advanced by any tool |
| `hp`, `max_hp`, `ac` | INTEGER | |
| `stats` | TEXT (JSON) | `{"STR":16,"DEX":16,"CON":12,"INT":14,"WIS":11,"CHA":14}` |
| `inventory` | TEXT (JSON) | `["a torch", "a worn dagger", ...]` — **flat list of item names, no item objects** |
| `location_id` | TEXT | FK-ish → `rooms.id`, this player's current room |

→ Pydantic model: `Character`. Read via `world.character(player_id)`; all characters via
`world.players()` (used by the GUI's shared-world roster).

### `rooms` (the world graph — nodes)
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | path-encoded, e.g. `r0`, `r0:north`, `r0:north:east` |
| `name`, `description` | TEXT | |
| `exits` | TEXT (JSON) | `{"north": "r0:north", "east": "r0:east"}` — direction → room id (the edges) |
| `contents` | TEXT (JSON) | **list of loose dicts, NOT a Pydantic model — see shapes below** |
| `visited` | INTEGER (bool) | has any player actually entered (vs. just known-to-exist via an exit) |
| `image_ref` | TEXT, nullable | art asset ref, not yet wired |
| `features` | TEXT (JSON) | `["a cracked stone sarcophagus, its lid shifted askew", ...]` — flavor strings |
| `kind` | TEXT | one/two-word room type (e.g. "attic"), LLM-picked when Flash is on, else `""` |
| `category` | TEXT | map UI display bucket, one of `worldgen.ROOM_CATEGORIES` (chamber/passage/open-air/water/underground/sacred/industrial/lair), LLM-picked; `""` if unset/unvalidated — `/state` derives a keyword-based fallback so the client never sees `""` |
| `danger` | INTEGER | map UI display only, 0-3 (0=safe/social, 3=deadly), LLM-picked, clamped on parse; `/state` floors it to at least 1 whenever the room has a live monster |

→ Pydantic model: `Room`. `contents` is deliberately loose (see module docstring in
`models.py`) since it's produced by `compendium.py`/`worldgen.py` and mutated in place
during combat — tightening it wasn't worth the risk this late.

**`contents` item shapes** (this is the part an agent needs before calling `attack` or
`pick_up_item`):
```jsonc
// monster
{"type": "monster", "name": "Specter", "hp": 22, "max_hp": 22, "ac": 12, "cr": 1,
 "traits": ["Incorporeal Movement", "Sunlight Sensitivity"],
 "attack_bonus": 3, "damage_dice": "1d6", "attack_name": "Life Drain"}

// loot
{"type": "loot", "name": "a vial of holy water"}
```
Monster fields beyond `type`/`name`/`hp` are populated from the SRD compendium
(`compendium.py`) when a real monster is placed, or from the procedural theme table
(`game.py`) as a lighter fallback — `attack` reads `.get(...)` with defaults, so both work.

### `log` (event history, append-only) — SCHEMA_VERSION 6
| column | type | notes |
|---|---|---|
| `seq` | INTEGER PK autoincrement | |
| `ts` | REAL | unix timestamp |
| `kind` | TEXT | dotted-namespace, e.g. `"player.moved"`, `"combat.resolved"`, `"item.picked_up"`, `"adventure.started"`, `"room.generated"`, `"memory.noted"` |
| `text` | TEXT | human-readable line, already narration-ready |
| `player_id` | TEXT, nullable | who caused it; null for world-level events with no single actor |
| `subject_type` / `subject_id` | TEXT, nullable | generic (aggregate_type, aggregate_id) pair — what the event is ABOUT, not who caused it. Currently only `"room"`/room_id is populated (combat/item-pickup events, queried by `world.recent_log(subject_type="room", subject_id=...)` for stigmergic trace narration in `look()`). `"item"`/item_id and `"entity"`/entity_id are valid once those have stable ids — monster/loot content dicts already carry an `"id"` field (`compendium.py`/`game.py`/`worldgen.py`) but nothing logs against it yet. |

→ Pydantic model: `LogEntry`.

## What's NOT in the DB yet (see `WORLD_SCHEMA.md` for the target)
- No first-class `entity` table — monster instances have no persistent identity across
  encounters, no persona/memory. `ask_npc` can't work until this exists.
- No first-class `item` table — items are just name strings in `character.inventory` and
  loose dicts in `room.contents`; no properties, no identification state.
- No generic `edges` table — relationships are hardcoded per-column (`rooms.exits`,
  `character.location_id`), which is fine at this scale, not fine at Postgres-scale.
- No `quest`, `lore`, `faction`.
