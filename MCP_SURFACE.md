# DNDMCP — the MCP surface (tools · skills · resources)

Layering: **tools = mechanics (primitives the agent calls)**; **skills = prompts/personas that
know HOW to orchestrate the tools** (the "skills over tools" idea); **resources = state the agent reads.**
🟢 MVP (build now, ~24h) · ⚪ soon · 💭 vision.

## TOOLS (mechanics the agent calls)
**World / navigation**
- 🟢 look — describe current room (scene, exits, contents, map)
- 🟢 move(direction) — traverse the world graph; generates unexplored rooms
- ⚪ examine(target) — inspect a feature/item/creature
- ⚪ search_room — find hidden things (skill check)
- 💭 travel_to(place) — fast-travel across known graph

**Character**
- 🟢 start_adventure — new campaign + rolled character + opening room
- 🟢 character_sheet — stats, HP, AC, inventory
- ⚪ use_item / update_inventory — consume/equip
- ⚪ rest — recover HP; ⚪ level_up; 💭 cast_spell

**Dice / checks (the honest random heart)**
- 🟢 roll_dice("1d20+3") — real dice (candidate: GPU-random, see below)
- ⚪ skill_check(stat, dc) — d20 + modifier vs difficulty
- ⚪ saving_throw(stat, dc)

**Combat**
- 🟢 attack — d20 vs AC, damage, HP, monster retaliation
- ⚪ start_encounter / end_turn / flee — structured combat
- 💭 cast(spell, target)

**Social / NPC**
- ⚪ talk_to(npc) — converse (agent roleplays via npc skill)
- ⚪ persuade / intimidate / deceive — social skill checks
- 💭 recruit / companion management

**World-gen (GPU — the Flash story)**
- ⚪ generate_scene_art — GPU image → ASCII for the current room (R8)
- 💭 generate_npc / generate_portrait / generate_dungeon / generate_map / generate_location

**Quest / session**
- 🟢 get_state — full inspectable campaign state (the world remembers)
- 🟢 start_quest / update_quest / active_quests — trackable, shared-world quests
- ⚪ journal / summarize_session — recap + memory
- 💭 advance_factions

**Multiplayer (vision)**
- 💭 who_is_here / leave_message / check_world_events — async shared world

## SKILLS (prompts/personas — agent adopts; the "skills over tools" layer)
- 🟢 be_the_dm — assume the DM role; run the loop; call tools; never fake dice. (shipped)
- ⚪ run_combat — combat adjudication workflow (initiative → attack → damage → narrate)
- ⚪ roleplay_npc(npc) — adopt an NPC's voice + goals for a conversation
- ⚪ describe_scene — vivid, terminal-friendly narration style
- ⚪ session_recap — summarize what happened, hook the next session
- 💭 world_builder — the autonomous world-expansion persona (the background agent)
- 💭 generate_quest — quest design skill

## RESOURCES (state the agent reads)
- 🟢 get_state covers it for MVP. Later expose as proper MCP resources:
- ⚪ character://current · campaign://log · world://map (graph)
- 💭 lore://bible · rules://srd · bestiary://monsters · items://catalog

## GPU-random dice? (candidate, evaluate)
"Roll dice on a GPU" — HONEST: a GPU adds nothing to RNG; it'd read as gimmicky to technical
judges. SKIP unless framed as real entropy/parallel mass-rolls. The GPU's real job = ART + (vision)
NPC inference + world-gen. Don't force dice onto it.

## MVP surface (lock for 24h)
TOOLS: start_adventure, look, move, roll_dice, attack, character_sheet, get_state (🟢 done) + generate_scene_art (R8).
SKILLS: be_the_dm (done) + maybe run_combat / describe_scene if time.
RESOURCES: get_state. Everything else = ⚪/💭 narrated as roadmap.
