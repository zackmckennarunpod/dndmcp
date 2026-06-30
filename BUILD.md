# DNDMCP — locked build plan

**Project (LOCKED):** DNDMCP — a solo-RPG Dungeon Master you install once as an MCP server and
play from ANY agent harness. Stateful world + rules + dice + GPU-generated art. All through MCP
tools (NO interactive UI) → robust, portable, plays in every harness.

**Pitch:** "Install DNDMCP, then play a persistent solo RPG from Claude Desktop (or any harness).
Your agent becomes a DM with memory, rules, dice, and live-generated art."

## Architecture (LOCKED)
- **MCP server, stdio, runs locally** (installed into Claude Desktop config). Reskin of `forge/server.py`.
- **SQLite state** (campaign, character, rooms, inventory, log). Reskin of `registry.py`.
- **Flash serverless GPU** for image gen (scenes, portraits, maps) — the GPU/Flash showcase, bursted.
- No pod for the MVP (stdio is local). Pod = later option for hosted/shared campaigns.
- **Rendering = console/terminal-first.** Tools return CONTENT BLOCKS: text/ASCII ALWAYS
  (renders in any terminal harness — Claude Code etc.), + GPU image as progressive enhancement
  for GUI harnesses (Claude Desktop). Never a dead demo.
- Build rendering-AGNOSTIC: tools produce structured scene data; a render layer emits text/ASCII
  (+ optional image). Open: GPU-image→ANSI for terminal vs GPU-art-for-GUI-only. Decide later.

## Vertical slice (the video demo)
Playable solo dungeon crawl:
1. start_adventure(theme) → premise + character + opening scene + image
2. move(direction) → navigate rooms → new scene + image
3. roll(dice) / skill checks → real dice
4. attack/resolve → one encounter, HP tracking
5. character_sheet() / get_state() → inspectable persistent state
6. state survives the session (SQLite) — "the world remembers"

## Art / GPU strategy (LOCKED)
- **Model: FAST over fanciest.** Use a turbo/fast text-to-image model (Z-Image Turbo / SDXL-Turbo
  / FLUX-schnell class) — ~1-3s warm. Speed is what makes background gen viable; we're fighting cold start.
  RPG-tuned options exist (CharGen ecosystem) but speed is the deciding factor for us.
- **Speculative prefetch (the latency killer):** on entering a room, FAN OUT background GPU jobs to
  pre-render every room the exits lead to (and optionally their encounters/NPCs). By the time the
  player `move`s, the art is already generated → instant. Burst to pre-generate, scale to zero on pause.
  This IS the Flash-burst story, and it hides cold start as background work.
- **First-gen warm-up:** kick the opening-room image at start_adventure (while player reads the intro);
  optionally stage weights on a network volume so cold start is load-only, not download.
- Art interface (`art.generate`) stays fixed; real impl fills image_b64 from a Flash endpoint. Add an
  async `prefetch(room_ids)` that fans out and caches by room image_ref.

## Build order (de-risked)
1. **Skeleton, art STUBBED** (no GPU spend): MCP server + SQLite state + working tools that
   return text + placeholder image refs. Verify it PLAYS end-to-end locally / in Claude Desktop.
2. **Wire real Flash image gen**: mint an image-gen endpoint (SDXL/fast model), return real images.
   Pre-warm before recording; cold start is editable in a video.
3. **Polish + record**: install in Claude Desktop, play a session, screen-record, edit.

## Reused from the kit
forge MCP/FastMCP pattern · registry.py (SQLite) · forge.mint + fan-out (image gen) ·
diagnostics/logs/teardown · env/auth. We are reskinning the kit into a product.

## Judging fit (4 pillars, see JUDGING.md)
Creativity (GM-OS for agents, newest MCP) · Usefulness (solo-RPG players, install-anywhere,
clear value) · Execution (reuses proven kit; art stubbed-then-real) · Presentation (live art +
state in chat = fun video). Solo scope = buildable in ~2 days.

## Open / parked
- Image model + cold-start strategy (pre-warm; maybe network-volume staged weights).
- MCP Apps interactive dungeon view = explicitly OUT for MVP (robustness + portability win).
- Pod-hosted shared campaigns = post-MVP.
