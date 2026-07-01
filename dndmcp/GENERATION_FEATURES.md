# DNDMCP — planned generation features (spec / handoff doc)

Simple additions on top of the proven pattern: a tool call builds a prompt from current
context, calls `flash_llm.generate()` (procedural fallback if Flash is off/errors), parses
the result, returns it. None need new background triggers; the bigger "world simulation" /
entity-scoped-domain-events idea is intentionally NOT here — see `LEARNINGS.md` for that.

Status key: 🔲 not started · 🟡 in progress · ✅ done

---

## ✅ NPC conversation
**What:** A `talk_to(player_id, message, npc_name=None)` tool — talk to a monster/NPC in your
current room, get a generated in-character response.

**Why:** Currently monsters are silent stat blocks. This is the most demo-visible addition —
"talk to something and it responds, consistently, to what you've said before" beats another
room description.

**Scope (deliberately NOT the bigger entity/domain-events system):**
- Give monster/NPC dicts (in `room.contents`) a stable `id` field at creation time
  (`compendium.py`'s `combat_profile()`, `game.py`'s procedural monster placement) — just one
  more key on the existing loose dict, no new table.
- Give them a `conversation: []` field too — list of `{"role": "player"|"npc", "content": str}`.
- `talk_to` finds the NPC (by name if given, else first monster present), builds a Flash
  prompt from: the NPC's name/traits/CR, the room/theme, and its stored `conversation` history
  so far (for continuity within/across visits) + the player's new message.
- Response gets appended to `conversation`, persisted via the SAME `world.upsert_room(...)`
  call `attack`/`pick_up_item` already use to mutate+save `room.contents` — no new persistence
  mechanism needed.
- Since rooms are shared, the conversation thread is visible to ANY player who later talks to
  the same NPC — gives the shared-world "the world remembers, even for someone else" effect
  without needing the bigger entity/domain-events system.
- Procedural fallback (Flash off): a short generic in-character line, no real continuity —
  same permissive-fallback pattern as `generate_item_content`.

## 🔲 Monster loot on defeat
**What:** When a monster's HP hits 0 in `attack`, generate 0-1 loot drops instead of the
monster just vanishing.
**Scope:** In `attack`, when `monster["hp"] <= 0`, call `worldgen.generate_item_content` (reuse
what `pick_up_item` already built) seeded with the monster's name/type, add result to
`room.contents` as a loot dict so it's pickup-able afterward. Small, self-contained.

## 🔲 Examine / search
**What:** `examine(player_id, target)` — "search the sarcophagus," "look closer at the doll."
Generates what's found: could be nothing, an item, or a bit of lore text.
**Scope:** Same shape as `pick_up_item`'s freeform path (procedural fallback + Flash judgment),
but for looking instead of taking — doesn't necessarily add to inventory, might just return
descriptive text or trigger a `pick_up_item`-style result if something's found.

## 🔲 Ambient events, Flash-generated
**What:** Replace `game.ambient_event`'s fixed-list random pick with a Flash call that reads
the theme + a few recent log lines, generating a fresh, history-aware ambient line.
**Scope:** New function in `worldgen.py` mirroring `generate_room_content`'s pattern, called
from `_render_scene` instead of `game.ambient_event`. Purely flavor text — doesn't mutate any
persisted state, so it's low-risk regardless of Flash reliability.

## 🔲 Monster flavor line
**What:** One generated sentence about a monster's demeanor/threat when first encountered —
layered ON TOP of the real SRD stat block, doesn't replace or touch it.
**Scope:** Small addition to `_render_scene`'s monster-rendering branch, or to
`generate_room_content` when `want_monster` is true. Rules-accuracy (AC/HP/attacks) stays
exactly as-is from the compendium; this only adds narration.

## 🔲 Session recap
**What:** A `session_recap(player_id)` tool — summarizes the log into a "previously on..."
blurb. Already on `MCP_SURFACE.md`'s roadmap.
**Scope:** One Flash call over `recent_log(n, player_id=...)`, procedural fallback = just
concatenate the log lines as-is (no summarization, but never crashes).

---

## Explicitly NOT in this list (see `LEARNINGS.md`)
- Entity-scoped domain events / full NPC disposition-from-history system — bigger
  architectural bet (entity persistence, log-tagging by aggregate, consistency tradeoffs).
  NPC conversation above deliberately does a SIMPLER version (history on the dict itself, not
  a generic entity/event table) to get 80% of the value without that lift.
- World-simulation / autonomous world-events — needs a trigger mechanism + a write-back
  boundary that doesn't exist yet. Not scoped at all here.
- Image/art generation — explicitly deprioritized in locked decisions.
