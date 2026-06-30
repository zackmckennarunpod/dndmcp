# World Schema — what an agent needs to "play DM"

Design principle: the world is a **graph of nodes + edges with mutable state**. The DM agent
READS it to know the situation and WRITES it (via tools) to record what changed. "The world
remembers" = every node's state persists. Scales to multiplayer (add player nodes + presence).

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
**item** — id, name, type, properties{}, location_id OR owner_id, identified
**quest** — id, title, description, state(active/done/failed), steps[], involves[entity/location ids]
**lore** — id, title, text, hook, found_in(location_id), about(entity/topic), discovered
**event** (log) — id, ts, kind(move/combat/social/discovery), text, location_id, actors[]
**faction** (later) — id, name, agenda, disposition_to_player, members[]
**player** (multiplayer, later) — id, character_id, session, last_seen, presence(location_id)

## EDGES (relationships — the graph)
- location —exit(dir)→ location   (the dungeon graph)
- character —is_in→ location ; entity —is_in→ location
- item —located_in→ location  |  item —owned_by→ character/entity
- entity —disposition→ character   (how an NPC feels about a PC)
- quest —involves→ entity/location ; lore —found_in→ location ; lore —about→ entity/topic
- event —about→ any node ; player —controls→ character

## MUTABLE STATE (the "world remembers" — what tools write)
- location.state (searched, door_open, fire, changed by actions) + visited/discovered
- entity: hp, disposition, alive, memory (each conversation appended), goal progress
- character: hp, inventory, conditions, xp, location, gold
- quest.state/steps ; lore.discovered ; event log grows

## READ tools (agent queries the graph)
get_state, look (current location + contents), character_sheet, who_is_here, ask_npc(needs npc memory),
recall_lore, active_quests, examine(node)

## WRITE tools (agent records changes)
move, attack/apply_damage, update_inventory, use_item, record_conversation(npc_id,...),
set_location_state, discover_lore, start/update_quest, log_event, generate(location/npc/quest via Flash)

## MVP (have / build now) vs LATER
- 🟢 HAVE: campaign, character, location(+features+exits), event log. (state.py)
- 🟡 BUILD NOW: `entity` as first-class (unique id + persona + memory + real SRD stats) — needed for
  ask_npc + living NPCs; location.state (mutable); quest minimal.
- 💭 LATER: item as node, lore node, faction, player/presence (multiplayer), full graph edges table.

## Storage
SQLite now (tables ≈ node types + a generic `edges` table for relationships). Postgres/graph-DB =
the shared-world scale path. Writes are TOOL-MEDIATED (agents never touch raw SQL) — that's the safety.
