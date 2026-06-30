# Idea: DNDMCP — a Game-Master OS for agents  (STRONGEST candidate)

**One-liner:** a persistent MCP server that turns ANY agent (Claude/Cursor/Codex/etc.) into a
stateful Dungeon Master — world state, rules, dice, NPCs, maps, encounters, inventory, memory —
with GPU-generated maps and portraits.

**Pitch:** "Install DNDMCP once, then play from ANY agent harness — Claude Desktop, Cursor,
ChatGPT, whatever. Your agent becomes a persistent Dungeon Master with memory, rules, dice,
maps, and art." Not "LLM tells a story" — stateful, rule-aware, inspectable, tool-driven.

## The distribution story (this is the sharp part — MCP's whole point)
You don't build a D&D app. You ship an MCP server people **install**, and then they play from
**whatever agent they already use**. MCP = portable, client-agnostic. The server holds the
state + rules + GPU art; the harness is just the table you sit at. "Bring your own agent."

## Solo RPG = the right scope (and a real, beloved genre)
Frame it as a **solo RPG** (think Ironsworn / solo 5e / journaling games): ONE player + the agent
as DM + the persistent server. This is a huge scope win for a 2-day video:
- no multiplayer coordination — one character, one session, one player (you, in the demo)
- the whole experience is demoable by one person from one harness
- solo RPG is a genuine, popular genre → credible specific audience
- the agent IS your DM; the server is the world that remembers

## Why it's the strongest idea (4 judging pillars, 25% each)
- **Creativity:** "game-master OS for agents over MCP" — distinctive, memorable.
- **Usefulness:** SPECIFIC audience (D&D players, DMs, agent builders), obvious value + tools.
  Fixes our weakest pillar (prior ideas were abstract substrates).
- **Execution:** pieces exist — MCP server (`forge/server.py`), SQLite state (`registry.py`),
  GPU mint/burst (`forge.mint`). Reskin the kit into a product.
- **Presentation:** BIG — live maps/portraits/campaign state on screen = fun to watch. It's a
  video; this is inherently a wow. Split screen: agent chat | live campaign + art.

## Architecture (resolves our cold-start pain — pod + burst hybrid)
- **Long-running Runpod POD:** hosts the MCP server + campaign DB → ALWAYS WARM, no cold-start
  lottery for the interactive loop. (Exactly the pod+volume direction we backed into.)
- **Serverless GPU BURST (Flash):** called by the MCP server for bursty/expensive work — image
  gen (maps, battle maps, portraits), maybe local NPC inference/embeddings. A few-second
  "the DM is drawing the map…" pause is dramatically appropriate, not a failure.
- **State:** SQLite (campaign, characters, inventory, world graph, logs). Vector store for lore recall.
- **Rules:** SRD-compatible mechanics (dice, combat, conditions, spells).

## Interface: MCP Apps (interactive UI in Claude's chat) — verified real
MCP Apps (launched Jan 26 2026, first official MCP extension) lets a tool return an
INTERACTIVE UI rendered in a sandboxed iframe inside Claude Desktop/chat. → an interactive
dungeon map you click to navigate, a character-sheet panel, live art. Fresh capability =
free Creativity + Presentation points (judges are AI-infra people).
- Refs: blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps, modelcontextprotocol.io/extensions/apps/overview
- @mcp-ui/client + @modelcontextprotocol/ext-apps SDK; React in sandboxed iframe, postMessage bridge.

**CATCH + mitigation (execution risk):** MCP Apps is new and finicky — open bug about UI not
rendering in Claude Desktop (ext-apps issue #671). So: **graceful degradation.** Tools return
images + text BY DEFAULT (works in any harness, today); layer the interactive MCP App view on
top. Fancy UI renders → wow; flaky → still plays as narrated text + generated art. Never a dead demo.

## MCP surface
Tools: create_campaign, create_character, generate_npc, generate_location, generate_dungeon,
generate_battle_map, generate_portrait, roll_dice, start_encounter, resolve_turn, apply_damage,
update_inventory, lookup_rule, summarize_session, get_campaign_state.
Resources: character sheets, campaign log, lore bible, location graph, monster DB, item index.
Prompts: DM style, NPC roleplay, combat adjudication, session recap, quest generation.

## Killer demo flow
1. "Start a level-3 gothic horror campaign for 4 players." → create_campaign → world premise,
   starting town, 5 NPCs, dungeon map, encounter table, hooks, portraits + map images.
2. "Run the first encounter." → roll_initiative, get_monster_stats, resolve_attack, update_hp,
   describe_scene, generate_battle_map.
3. After: summarize_session, update_campaign_memory, advance_factions.

## Honest risks
- **SCOPE is the killer.** ~2 days for a video. CANNOT build it all → pick a vertical slice that
  demos beautifully; stub/fake the rest. (e.g. create_campaign + one encounter + 2 image gens + state view.)
- **GPU isn't the novel part** — image gen = "call SDXL." Fine: criteria reward creativity/
  usefulness/presentation, not GPU cleverness. Novelty = the stateful MCP game-master.
- **"AI D&D" exists** (AI Dungeon). Fresh angle = "any agent + MCP + stateful + GPU art." Don't
  pitch as "we made AI D&D."

## What we reuse from the kit
MCP server pattern (forge/server.py) · SQLite state (registry.py) · GPU mint+fan-out (forge.mint) ·
diagnostics/logs/teardown · the monitor UI pattern. We're reskinning the kit into a product.

## Related: [[gpu-tools-for-agents]] (the substrate this sits on), [[stateful-gpu-service]] (the pod+volume arch).
