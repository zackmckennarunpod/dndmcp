# DNDMCP — Requirements & Ideas Tracker

**Beads is the source of truth for individual requirement status** — `bd list --all` / `bd show <id>`.
This doc stays for the narrative that doesn't fit an issue tracker: the pitch, judging framing,
priority reasoning, and the vision roadmap.

## The pitch (LOCKED)
**The terminal IS the game.** DNDMCP turns your agent harness (Claude Code, Claude Desktop, any
terminal/MCP client) into a playable game console. You play a stateful solo RPG by talking to your
agent — the MCP server is the rules engine + persistent memory; the agent is the storyteller.
Everything renders IN the console: text, ASCII maps, and **GPU-generated art converted to ASCII** so
it appears inline in any harness. Install once → the terminal becomes a dungeon.

(Note: Context MCP is our model for HOW we track requirements below — not the product framing.)

## Why it wins (4 judging pillars — see JUDGING.md)
- Creativity: GM-OS for agents; newest MCP capabilities; "Context-MCP for games."
- Usefulness: specific audience (solo-RPG players, DMs, agent builders), install-anywhere, clear value.
- Execution: reuses proven kit (MCP server, SQLite, GPU mint); skeleton already plays.
- Presentation: live art + persistent state + dice in chat = a fun, visual video.

## Functional requirements
See `bd list --all` for current status (R1-R7, R9 closed as already-shipped; R8/R10/R11/R12
still open, R12 is the DUE Wed 12pm PST demo-video deliverable). This table is retired —
status drifted from beads once before, don't let it happen twice.

## Multiplayer extension — SUPERSEDED, now core (see below)
The feasibility table below was written when multiplayer was vision/pitch-only. It's been
elevated to the core premise — see beads epic `flash-hackathon-cof` ("stigmergic multiplayer
— players are ghosts who shape a shared world") and the MVP LINE update below. Table kept
as feasibility reference, not as the current scope statement.
| # | Idea | Feasibility |
|---|---|---|
| M1 | Shared world state on a long-running pod DB (players, presence, event log) | 🟢 EASY — just a shared DB |
| M2 | Async/event-log world: actions accumulate; ambient events returned on each action ("Mara passed through, went north") — play-by-post / async-MMO-lite | 🟡 NATURAL — the realistic target; works with standard MCP |
| M3 | poll-on-action liveness: every tool call + a `check_world` tool pull fresh events so the world FEELS live | 🟡 the right way to fake "presence" |
| M4 | TRUE real-time push ("you see Mara walk in this instant") | 🔴 HARD — MCP is request/response. Server→client SSE notifications exist but host (Claude Desktop) surfacing them mid-session is unreliable. DON'T depend on it. |
| M5 | Two agents/harnesses meet & quest together | 🟡 async version achievable; live version = stretch-of-stretch |

**Honest multiplayer pitch:** "a persistent shared world any agent can join, where everyone's actions
accumulate" (async MMO-lite over MCP). Real, novel. "Live co-op same-instant" is NOT a promise.
Design solo state layer SHARED-READY (DB already; add players + events table + location-scoped event pull).

## Vision: the agentic living world (ROADMAP — pitch it, don't fully build it)
The grand vision, coherent and pitch-worthy. NOT all buildable in 2 days — this is the "where it goes."
| # | Idea | Notes |
|---|---|---|
| V1 | World = graph DB of connected places/spaces | 🟢 already true (rooms+exits = directed graph); just formalize |
| V2 | Background **world-builder agent**: watches player positions, generates next region ahead of them (desc + ASCII art) | the speculative-prefetch idea AS a persistent autonomous agent |
| V3 | Autonomous **NPC agents**: move around the graph, pursue goals, leave traces/events | makes the world alive between your turns (no push needed) |
| V4 | "Tons of skills" the DM/world-builder/NPC agents use to operate | skills/tools per agent role |
| V5 | THE GPU-BURST STORY: NPC inference + world-gen + art = many parallel jobs → Flash serverless burst, scale to zero when quiet | strongest, non-gimmicky GPU justification we have |
| V6 | Honest cost: always-on agents = continuous burn; scale-to-zero (NPCs only "think" when activity nearby) | real consideration |

## ⛔ MVP LINE (what we ACTUALLY build for the video) vs PITCH
**UPDATE 2026-07-01: multiplayer is no longer pitch-only — elevated to CORE PREMISE** (beads
epic `flash-hackathon-cof`, stigmergic multiplayer: players never see/talk to each other
directly, only encounter the permanent traces of what earlier players did to the shared
world). Shared-world DB and pod-hosted HTTP transport were already built as a byproduct of
the solo-play architecture (verified against state.py/app.py, closed as done in beads); the
one piece actually outstanding is surfacing those traces as narration to later players
(`flash-hackathon-nva`).

**BUILD (shippable in 2 days):** solo play · persistent graph world · dice/combat · GPU ASCII art w/ prefetch ·
DM persona · stigmergic trace narration in `look()` (the CORE PREMISE payoff — see `nva`).
**PITCH (vision/roadmap):** world-builder agent, NPC agents, anything beyond stigmergic traces
(direct presence, chat, real-time push — explicit non-goals per the epic). Show the
architecture, demo the slice. Working game NOW + jaw-dropping "where this goes."
→ Do NOT try to build V2-V6 fully. Ship the MVP (now including stigmergic traces); narrate the rest.

## Pod brain + web map (BUILDING NOW — enabler for pod-hosted everything)
- Containerize DNDMCP, run on a Runpod pod as the persistent "brain." Pod = always-warm (no
  serverless cold-start lottery). MCP over HTTP transport (stdio only works locally).
- **Web map UI served by the pod** (reuses our monitor pattern): renders the world graph + the
  player's position, SYNCED to their session (both MCP + map read the same DB). High video value:
  split-screen terminal play | live map updating as you travel. 🟡 high-value stretch.
- Keep LOCAL stdio working as the zero-risk fallback for the video. Both paths supported.

## ⭐ FLASH ANCHOR (LOCKED — this is the hackathon requirement, central not afterthought)
**UPDATE 2026-07-01: world-builder LLM (priority 1 below) is CONFIRMED WORKING, live, end to
end.** See HANDOFF.md for the full working recipe (SDK version, image, CUDA pin, calling
convention). This was the critical blocker all session — it's resolved.

Frame (user's words): "an MCP that proxies requests to GPUs." DNDMCP's tools proxy GPU
GENERATION to Runpod Flash (via our `forge` kit). Image/world/NPC are ONE proxy pattern, 3 uses.
Priority:
1. **World-builder (PRIMARY):** traversal-driven speculative generation. Watch player position +
   heading → Flash model cheaply generates the next rooms + characters AHEAD → store in graph →
   ready when the agent enters & queries. "World builds itself ahead of you." Burst → scale to zero.
2. **Images (SECOND):** Flash image gen → ASCII for scene/portrait/map, inline in terminal.
3. **NPC LLM `ask_npc` (DEFERRED killer-future):** persona-conditioned Flash inference; built +
   STUBBED in inference.py, flip FLASH_NPC=1 + endpoint id to go live.
Reliability (Flash flaky): pre-warmed endpoint, small fast model, ADA_24, STUB fallback always so
the game never dies. Build stub→real. State: SQLite now; Postgres persona/char graph = scale path.

## Art / GPU strategy (LOCKED — see BUILD.md)
- Fast model (Z-Image Turbo / SDXL-Turbo / FLUX-schnell class) — speed > max quality.
- Speculative prefetch = the Flash-burst story (burst to pre-render adjacent rooms, scale to zero).
- Honest caveat: first gen pays cold start → warm at adventure start / stage weights on volume.

## Open questions
- O1: GPU image → ANSI for terminal, vs image-for-GUI-only? (rendering-agnostic for now)
- O2: which exact fast image model + its cold-start/size profile on Flash?
- O3: multiplayer transport — remote HTTP MCP vs local MCP → pod API?
- O4: reliability — serverless allocation hangs observed (ADA_24 reliable); pre-warm strategy for demo.

## Build status
🟢 Skeleton plays end-to-end (zero GPU spend). NEXT: wire real Flash image gen (R8) + prefetch (R9).
