# World Schema — what an agent needs to "play DM"

Design principle: the world is a **graph of nodes + edges with mutable state**. The DM agent
READS it to know the situation and WRITES it (via tools) to record what changed. "The world
remembers" = every node's state persists. Scales to multiplayer (add player nodes + presence).

Second principle, specifically for anything content-shaped rather than mechanics-shaped
(item properties/effects, NPC persona details, lore text): keep the **envelope** fixed but
leave the **content inside it loose and agent-authored**, not a rigid human-picked enum of
fields. A schema that tries to pre-enumerate every property an item or effect could have
will always be missing the one an LLM invents for a specific item's flavor — and forcing
generation into a fixed shape fights how the model actually generates and reasons about
varied content. `models.py` already does this for `Room.contents` (deliberately loose
dicts, not a Pydantic model) — the same logic applies anywhere content varies per-instance.
See ITEM EFFECTS below for the concrete pattern.

## What the DM agent must be able to ASK (read patterns)
- Where is the player? What's in this room — exits, features, creatures, items? → `location` + contents
- What's my character sheet (HP, AC, stats, inventory, conditions)? → `character`
- Who is this NPC, and what do they know/remember about me? → `npc` + its memory
- What has happened so far? Active quests? → `event` log + `quest`
- What is this world (tone, lore, rules)? → setting bible + `lore`

## NODES (entities)
**world / campaign** — id, setting_name, theme, premise, turn, current_time, world_bible_ref, current_location_id
**location** (room/place — the graph nodes) — id, name, kind, description(directional), features[],
  exits{dir→location_id}, discovered, visited, image_ref, region_id, ambient_pool, state{} (mutable:
  door_open, fire_lit, searched, trap_sprung…), hazards[]
**character** (PC) — id, name, class, level, xp, hp, max_hp, ac, stats{STR..CHA}, inventory[],
  conditions[], location_id, backstory, gold
**entity** (NPC / monster instance — UNIQUE per instance) — id, kind(SRD type), name, hp, max_hp, ac,
  attack_bonus, damage_dice, traits[], cr, location_id, disposition(hostile/neutral/ally),
  alive, persona(for ask_npc), memory[] (conversation/interaction history), goal
**item** — id, name, description, location_id OR owner_id, identified, acquired_at
  properties{} — agent-authored, loose (weight, value, slot, whatever THIS item's concept
    implies — not a fixed field list; only what generation chose to fill in)
  effects[] — agent-authored, loose (see ITEM EFFECTS below)
**quest** — id, title, description, state(active/done/failed), steps[], involves[entity/location ids]
**lore** — id, title, text, hook, found_in(location_id), about(entity/topic), discovered
**event** (log) — id, ts, kind(move/combat/social/discovery), text, location_id, actors[]
**faction** (later) — id, name, agenda, disposition_to_player, members[]
**player** (multiplayer, later) — id, character_id, session, last_seen, presence(location_id)

## ITEM EFFECTS (loose envelope, agent-authored content)

Each entry in `item.effects[]` is a dict. Only two keys are conventions, not requirements —
everything else is whatever the generating call (`generate_item_content` or similar) decided
this specific item needed:
- `trigger` — a short tag hinting WHEN it might matter. Not an enum, not validated against a
  fixed list — `"time_elapsed"`, `"on_equip"`, `"on_low_hp"`, `"on_combat_start"`, or
  something invented for one weird item, all equally valid.
- `narration` — freeform text for the DM to draw on when the effect comes up.

Example — a cursed doll:
```jsonc
{"trigger": "time_elapsed", "threshold_minutes": 10,
 "narration": "the doll's weight has grown cold and wrong in your pack",
 "mechanical_hint": "5 damage, once"}
```
`mechanical_hint` isn't a required field either — it's just this item's chosen way of telling
the DM roughly what should happen. Nothing reads it with `model["mechanical_hint"]`-shaped
code; it's context, same as `description`.

**Resolution stays agent-judged, not code-dispatched.** A generic, trigger-agnostic check
(the "lazy tick" — see below) surfaces "this item's condition looks met" as a FACT at the top
of a tool response (same pattern as room/exit facts today — tools hand facts, the DM agent
narrates and decides the mechanical outcome, then calls existing tools like `attack`/damage
to actually apply it). No per-effect-type switch statement in code — a fixed enum there would
just relocate the rigid schema one layer down instead of removing it.

**Lazy tick, not a scheduler.** Nothing in this stack runs in the background — it's pure
MCP tool calls, nothing executes unless the agent invokes a tool. So "10 minutes in your
inventory" doesn't fire on a clock; it's checked against `item.acquired_at` at the top of
tool calls that already touch the character (`look`, `move`, `attack`). Effects surface
"late" relative to a real cron (whenever the player next does something), which is fine for
turn-based narration and needs zero new infrastructure.

## EDGES (relationships — the graph)
- location —exit(dir)→ location   (the dungeon graph)
- character —is_in→ location ; entity —is_in→ location
- item —located_in→ location  |  item —owned_by→ character/entity
- entity —disposition→ character   (how an NPC feels about a PC)
- quest —involves→ entity/location ; lore —found_in→ location ; lore —about→ entity/topic
- event —about→ any node ; player —controls→ character
- location —in_campaign→ campaign  (world/campaign membership; `from_type='room'` per the
  existing room-edge convention). `rooms.campaign_id` (plus the campaign-id room-id prefix
  convention) stays as a denormalized, indexed read-optimization for the many existing
  partition-scoped queries — this edge is the semantic source of truth. The real payoff: a
  room belonging to MORE than one campaign (e.g. a future portal/crossover room) becomes an
  additional edge row with zero schema change, instead of forcing the scalar column to become
  a list/join table. Written go-forward only in `upsert_room` — not backfilled for existing
  rooms, same as every other additive migration in this file.

## MUTABLE STATE (the "world remembers" — what tools write)
- location.state (searched, door_open, fire, changed by actions) + visited/discovered
- entity: hp, disposition, alive, memory (each conversation appended), goal progress
- character: hp, inventory, conditions, xp, location, gold
- quest.state/steps ; lore.discovered ; event log grows
- item.effects: nothing mutates the effect definition itself — resolution is lazy-checked
  against acquired_at/context at read time, not ticked down and rewritten each turn

## READ tools (agent queries the graph)
get_state, look (current location + contents), character_sheet, who_is_here, ask_npc(needs npc memory),
recall_lore, active_quests, examine(node)

## WRITE tools (agent records changes)
move, attack/apply_damage, update_inventory, use_item, record_conversation(npc_id,...),
set_location_state, discover_lore, start/update_quest, log_event, generate(location/npc/quest via Flash)

## MVP (have / build now) vs LATER
- 🟢 HAVE: campaign, character, location(+features+exits), event log. (state.py) Item
  name+description as loose dicts on inventory (state.py `add_item`). `quest` minimal
  (id/title/description/state/steps, `involves[]` via the generic `edges` table) —
  start_quest/update_quest/active_quests, DM-authored (no Flash generation yet).
- 🟡 BUILD NOW: item.effects[] + acquired_at + lazy-tick check (one prototype effect first —
  the cursed doll — before generalizing); `entity` as first-class (unique id + persona +
  memory + real SRD stats) — needed for ask_npc + living NPCs; location.state (mutable).
- 💭 LATER: item as a fully first-class node (own table, not just inventory dicts), lore
  node, faction, player/presence (multiplayer), full graph edges table.

## Storage
SQLite now (tables ≈ node types + a generic `edges` table for relationships). Postgres/graph-DB =
the shared-world scale path. Writes are TOOL-MEDIATED (agents never touch raw SQL) — that's the safety.
