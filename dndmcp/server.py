"""DNDMCP — a solo-RPG Dungeon Master as an MCP server (stdio).

Install once, play from any harness. The server is the rules engine + persistent world;
your agent is the storyteller. All through MCP tools; output is text/ASCII (any terminal
harness) + optional GPU art (GUI harnesses).

Run / install (Claude Desktop config):
    "dndmcp": { "command": "/abs/.venv/bin/python", "args": ["-m", "dndmcp.server"] }
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid

from mcp.server.fastmcp import FastMCP

from . import art, compendium, game, linear_gen, worldgen
from .linear_world import TicketWorld
from .models import Campaign, Room
from .state import MAIN_CAMPAIGN_ID, World

# This server hosts more than one world on the same graph engine — D&D is one instance of it,
# the task graph is another. Shown to the connecting agent FIRST, before either world's own
# persona, so "which world?" is the first question asked, not assumed.
WELCOME = """This MCP server hosts multiple independent worlds on the same underlying engine
(a graph of nodes + edges, with Flash-generated content). Ask the user which one they want,
then act accordingly:

- **D&D adventure** — a solo/shared tabletop RPG. Call start_adventure to begin; once started,
  BECOME the Dungeon Master (see the full persona below) for the rest of the session.
- **Task graph** — a Linear-style ticket graph. Call list_tickets (or seed_demo_tickets if it's
  empty) to see it, look_at_ticket to inspect one + its related tickets, complete_ticket to
  mark one done and generate a follow-up linked into the graph.
- Call list_worlds() any time for a concrete, current list.

Don't assume D&D by default — ask first."""

# Shipped WITH the server so connecting DNDMCP makes the agent assume the DM role, once the
# user has actually chosen the D&D world (see WELCOME above).
DM_PERSONA = """You are the Dungeon Master for a solo tabletop RPG running on DNDMCP. The
terminal IS the game. When this server is connected, BECOME a vivid, fair Dungeon Master.

How to run the game:
- BEFORE calling start_adventure, ask the player which world they want (don't assume — this
  is a real choice, not a formality):
    1. Join the MAIN shared world (default) — a persistent world other players' ghosts have
       already passed through; you'll see traces of what they did.
    2. Start their OWN new world — pass campaign_id="new" to start_adventure. They'll get
       back a shareable world id; tell them plainly they can send that id to friends so those
       friends can join THIS SAME world (campaign_id=<that id> on their own start_adventure).
    3. Join a SPECIFIC world a friend already shared with them — pass that id as campaign_id.
  Also ask for theme + character (or offer to pick something evocative) in the same breath.
  Call start_adventure with whichever campaign_id fits.
- start_adventure's result includes a player_id, a live-map link, AND (for a new world) a
  shareable world id. ALWAYS restate ALL of these plainly in your own reply, near the top —
  don't leave them buried in the raw tool output where the player might miss them. Same goes
  for get_state or any other call that surfaces the link again later.
- The tools hand you FACTS, not finished prose: a room's name, kind, one atmosphere note,
  features, contents, and which exits are known vs unexplored. That's your notes, same as a
  human DM's — YOU write the actual scene in your own voice, richly, from those facts. Don't
  just relay the fields verbatim. Then ALWAYS end your turn with "What do you do?"
- Exits come with a `descriptor` (the threshold itself — a door, archway, stairwell, gap —
  part of THIS room, always safe to describe) and a `direction` (north/south/etc). The
  `direction` is INTERNAL PLUMBING ONLY — never say it, print it, or hint at it to the
  player. Describe exits ONLY by their descriptor: "a warped iron door" / "a gap in the
  collapsed wall" / "a stairwell spiraling down," never "to the north" or "the north exit."
  If a room has two exits, distinguish them by their descriptors (door vs. stairwell), not by
  direction. You still silently track which descriptor maps to which direction (the tool
  gives you both) so that when the player says "I go through the iron door" or "I take the
  stairs down," you call move(direction) with the RIGHT direction behind the scenes — the
  player should never need to know or say a compass word to play the game. Never invent
  what's beyond an undiscovered exit, but the doorway itself is fair game since it's right
  in front of you.
- The player explores by telling you their intent. Translate intent into tool calls:
    move there        -> move(direction)
    any check/attack  -> roll_dice / attack  (NEVER invent dice — always call the tool)
    look around       -> look      check self -> character_sheet     recap -> get_state
    leave/drop item   -> drop_item(player_id, item_name)
- 0 HP is real death, not a scare: move/attack/talk_to/pick_up_item all refuse to proceed once
  a character has fallen and hand back a clear restart message instead. Narrate the death
  properly, then let the player choose: start_adventure again (same campaign_id, a fresh
  character in this same persistent world) or delete_world (wipes their own custom world
  entirely, so long as they're its only remaining player — never offer this for "main").
- Players never see or talk to each other directly — only their live position on the map (a
  "ghost" moving through the world, per the GUI). The ONLY way players affect each other is
  through the shared graph itself: drop_item leaves something real in a room for whoever
  arrives next to pick_up_item, and log_event traces work the same way. If a player asks to
  "leave this for someone" or "signal" another player, that's drop_item or log_event — never
  invent a way to message another player directly, there isn't one.
- The player will do things none of the above cover — read a diary, examine something
  closely, search a corpse, notice a detail. ALWAYS call log_event(player_id, text) for
  these. It's what makes the moment durable (future players see it as a trace when they
  visit the same room/item) instead of just narrated once and forgotten. If you invent
  actual content (what the diary SAYS, what the search TURNS UP), put that content in the
  log_event text — that's the only place it gets remembered.
- Narrate results dramatically but keep mechanics HONEST: use the exact numbers the tools return.
- The world is PERSISTENT — the tools remember. Refer back to what happened; the world is real.
- Room/character state is rigid on purpose (mechanics need one source of truth). For anything
  that ISN'T captured there but matters for continuity — an NPC's real motive, a lie you told,
  something you decided in the moment — call remember(note) yourself. That's YOUR self-managed
  memory, not predefined fields; write whatever's worth keeping, in whatever shape fits.
- Keep it terminal-friendly: short paragraphs, show the ASCII map/art from tools, give clear choices.
- Be a fair DM: let dice and rules decide; build tension; reward clever play."""

mcp = FastMCP("dndmcp", instructions=WELCOME + "\n\n---\n\n" + DM_PERSONA)
world = World()

# A second, fully independent world proving the engine generalizes beyond D&D — own file,
# own SQLite DB, zero shared state with `world` above. See linear_world.py/linear_gen.py.
tickets = TicketWorld()


def _nearby_region(from_room_id: str, *, depth: int = 2,
                   exclude: str | None = None) -> list[tuple[str, str, str]]:
    """BFS outward through already-generated (linked) rooms up to `depth` hops — (name, kind,
    room_id) triples. The (name, kind) pair feeds the next room's LLM prompt for tonal/
    architectural continuity; room_id lets callers also check what's already living nearby
    (see _maybe_spawn_entity_persona's density gate). Pure DB reads; no extra LLM calls."""
    seen = {from_room_id} | ({exclude} if exclude else set())
    frontier = [from_room_id]
    out: list[tuple[str, str, str]] = []
    for _ in range(depth):
        next_frontier = []
        for rid in frontier:
            room = world.room(rid)
            if not room:
                continue
            for dest_id in room.exits.values():
                if dest_id in seen:
                    continue
                seen.add(dest_id)
                dest = world.room(dest_id)
                if dest:
                    out.append((dest.name, dest.kind, dest_id))
                    next_frontier.append(dest_id)
        frontier = next_frontier
        if not frontier:
            break
    return out


def _require_room(room_id: str) -> Room:
    """A player's location_id / a room's exit target must always resolve — that's a game-logic
    invariant, not something to silently tolerate. Fail loudly if it's ever violated."""
    room = world.room(room_id)
    assert room is not None, f"room {room_id!r} referenced but missing from the world"
    return room


def _require_campaign(campaign_id: str = MAIN_CAMPAIGN_ID) -> Campaign:
    camp = world.campaign(campaign_id)
    assert camp is not None, f"campaign {campaign_id!r} referenced but none exists yet"
    return camp


def _dead_gate(ch) -> str | None:
    """0 HP is death, not a flesh wound — every action tool calls this first and returns its
    message instead of proceeding when the character has fallen. Returns None (proceed as
    normal) while HP > 0."""
    if ch.hp > 0:
        return None
    return (f"☠ {ch.name} has died. Call start_adventure(campaign_id={ch.campaign_id!r}) to "
            f"begin a new character in this world, or delete_world to wipe it and start fresh.")


def _gui_link() -> str:
    pod_id = os.environ.get("RUNPOD_POD_ID")
    if pod_id:
        return f"https://{pod_id}-{os.environ.get('GUI_PORT', '8001')}.proxy.runpod.net"
    return f"http://localhost:{os.environ.get('GUI_PORT', '8001')}"


@mcp.tool()
def list_worlds() -> str:
    """List the worlds this server hosts. Call this first when connecting, before assuming
    which one the user wants."""
    camp = world.campaign()  # the main dnd world specifically
    dnd_status = f"in progress ({camp.theme})" if camp else "not started yet"
    n_tickets = len(tickets.all_tickets())
    return (
        "**dnd** — solo/shared tabletop RPG. start_adventure joins the main shared world by "
        'default (everyone\'s ghosts pass through here). Pass campaign_id="new" to start your '
        "own world instead (you get back a shareable id), or campaign_id=<id> to join a "
        f"specific world someone shared with you. Main world status: {dnd_status}.\n"
        "**tickets** — a task graph (Linear-style). list_tickets / seed_demo_tickets to begin. "
        f"Status: {n_tickets} ticket(s) loaded."
    )


@mcp.prompt()
def be_the_dm() -> str:
    """Invoke to make your agent assume the Dungeon Master role and start a session."""
    return DM_PERSONA + "\n\nGreet me and offer to begin an adventure."


def _adjacent_rooms(room: Room, player_id: str | None = None) -> list[dict]:
    """Per-exit info the agent needs to narrate/navigate honestly: does THIS PLAYER know
    what's beyond this exit (name it) or not (say so, don't invent details)? "Known" is
    gated on world.has_discovered, NOT on whether the destination room row exists — the
    background prefetch (_prefetch_frontier) world-builds every exit's destination the
    instant a room is entered, well before any player has looked through that doorway, so
    "exists in the DB" leaked real room names to players who'd never been there. `descriptor`
    (the exit's physical threshold — door/archway/stairwell) is always safe to reveal,
    discovered or not, since it describes THIS room, not what's beyond it."""
    descriptors = world.room_exit_descriptions(room.id)
    out = []
    for direction, dest_id in room.exits.items():
        dest = world.room(dest_id)
        known = dest is not None and player_id is not None and world.has_discovered(player_id, dest_id)
        out.append({
            "direction": direction,
            "descriptor": descriptors.get(direction),
            "known": known,
            "name": dest.name if known and dest else None,
            "visited": dest.visited if known and dest else False,
        })
    return out


def _render_scene(room: Room, *, player_id: str | None = None, ambient: bool = True,
                  with_art: bool = True) -> str:
    """Text/ASCII render of a room — the universal (terminal) output."""
    lines = [f"## {room.name.title()}", "", room.description]
    for f in room.features:
        lines.append(f"  • {f}")
    for c in room.contents:
        if c["type"] == "monster":
            cr = f", CR {c.get('cr')}" if c.get("cr") is not None else ""
            traits = f" [{', '.join(c['traits'])}]" if c.get("traits") else ""
            lines.append(f"\n⚔  A {c['name']} is here (AC {c.get('ac','?')}, HP {c['hp']}{cr}).{traits} It looks hostile.")
        elif c["type"] == "loot":
            lines.append(f"\n✦  You notice {c['name']}.")
    # Stigmergy: what other players did here before you arrived — FACTS for the DM to weave
    # into narration (same pattern as everything else this function hands the agent), not
    # pre-written prose. Excludes the viewer's own past actions in this room — this is about
    # noticing OTHER players' traces, not being told what you already know you did.
    traces = world.recent_log(3, subject_type="room", subject_id=room.id, exclude_player_id=player_id)
    if traces:
        lines.append("\nTraces of those who came before:")
        for t in traces:
            lines.append(f"  - {t.text}")
    if ambient:
        ch = world.character(player_id) if player_id else None
        camp = world.campaign(ch.campaign_id if ch else MAIN_CAMPAIGN_ID)
        lines.append(f"\n_{game.ambient_event(camp.theme if camp else 'default')}_")
    lines.append("")
    lines.append(game.ascii_map(room.model_dump()))
    # Descriptor leads, not direction — direction is bracketed at the END and labeled
    # explicitly as internal-only, so even a skim of this raw data reads "door/stairwell/gap"
    # first. Reordering this (not just relying on the DM_PERSONA instruction) is deliberate:
    # the old direction-first format got parroted into player-facing narration in practice.
    lines.append("\nExits (describe by descriptor ONLY — never say the bracketed direction "
                 "to the player, it's for your own move() calls):")
    for adj in _adjacent_rooms(room, player_id):
        threshold = adj["descriptor"] or "an unmarked passage"
        if adj["known"]:
            status = "visited" if adj["visited"] else "known, not yet visited"
            lines.append(f"  {threshold}, leading to {adj['name'].title()} ({status}) "
                         f"[direction: {adj['direction']}]")
        else:
            lines.append(f"  {threshold} — beyond it is unexplored, do not invent what's there "
                         f"[direction: {adj['direction']}]")
    if with_art:
        a = art.generate(f"{room.name}: {room.description}", kind="scene")
        lines.append("\n" + a["ascii"])
        if not a["enabled"]:
            lines.append("(art: stubbed — GPU image gen not yet wired)")
    return "\n".join(lines)


@mcp.tool()
async def start_adventure(theme: str = "gothic horror", character_name: str = "Wanderer",
                          character_class: str = "Fighter",
                          campaign_id: str | None = None, premise: str | None = None) -> str:
    """Begin your adventure. `campaign_id` picks WHICH world:
      - omit it (or pass "main") -> the persistent default world everyone lands in
      - "new" -> create a brand-new world; the reply gives you back its shareable id — send
        that to others so they can join THIS world with campaign_id=<that id>
      - any other value -> join that specific existing world by its id (a clear error if it
        doesn't exist)
    `premise` (only used when actually creating a new world — ignored when joining an
    existing one): a short, evocative, YOUR-OWN-WORDS description of THIS world's opening
    hook. Write one — don't rely on the generic fallback. Loose/free-form on purpose (see
    WORLD_SCHEMA.md's envelope-fixed-content-loose principle) — this is creative writing,
    not a form to fill in.
    Returns your player_id — pass it as player_id to every other tool call — and a link to
    watch your position live on the map."""
    player_id = uuid.uuid4().hex[:12]

    if campaign_id is None or campaign_id == MAIN_CAMPAIGN_ID:
        target_id = MAIN_CAMPAIGN_ID
    elif campaign_id == "new":
        target_id = secrets.token_hex(4)
    else:
        if not world.campaign_exists(campaign_id):
            return (f'No world with id "{campaign_id}" exists. Omit campaign_id to join the '
                     'main world, or pass campaign_id="new" to start your own.')
        target_id = campaign_id

    camp = world.campaign(target_id)
    if not camp:
        start_id = "r0" if target_id == MAIN_CAMPAIGN_ID else f"{target_id}:r0"
        premise = premise or (f"A {theme} adventure. Something stirs in the dark, "
                              f"seeking what others feared to find.")
        camp = world.create_campaign(target_id, theme=theme, premise=premise, start_room=start_id)
        gen = await worldgen.generate_room_content(start_id, theme, salt=camp.salt)
        await _maybe_spawn_entity_persona(gen, start_id, theme, [], campaign_id=target_id)  # no neighbors yet
        world.upsert_room(room_id=start_id, campaign_id=target_id, name=gen["name"],
                          description=gen["description"], exits=gen["exits"],
                          contents=gen["contents"], features=gen.get("features"),
                          kind=gen.get("kind", ""), exit_descriptions=gen.get("exit_descriptions"))
    room = _require_room(camp.start_room)
    ch = game.new_character(character_name, character_class)
    char = world.new_character(player_id, camp.id, name=ch["name"], klass=ch["klass"], hp=ch["hp"],
                               ac=ch["ac"], stats=ch["stats"], inventory=ch["inventory"],
                               location_id=camp.start_room)
    world.mark_visited(camp.start_room)
    world.discover(player_id, camp.start_room)
    world.log("adventure.started", f"{char.name} the {char.klass} joined the adventure.",
              player_id=player_id)
    asyncio.create_task(_prefetch_frontier(room, camp.theme, camp.id, camp.salt))  # fire-and-forget
    share_note = (f'\n\n🔗 **World id: `{camp.id}`** — share this so others can join THIS '
                 f'exact world (start_adventure with campaign_id="{camp.id}").'
                 if camp.id != MAIN_CAMPAIGN_ID else "")
    map_link = f"{_gui_link()}/?player={player_id}"
    if camp.id != MAIN_CAMPAIGN_ID:
        map_link += f"&campaign={camp.id}"
    return (f"# {camp.premise}\n\nYou are **{char.name}**, a level 1 {char.klass} "
            f"(HP {char.hp}, AC {char.ac}).\n\n**player_id: `{player_id}`** — pass this to every "
            f"other tool call.{share_note}\n\n🗺 Watch your adventure live: {map_link}\n\n"
            + _render_scene(room, player_id=player_id))


@mcp.tool()
def look(player_id: str) -> str:
    """Describe the current room again (scene, exits, contents, map)."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    return _render_scene(_require_room(ch.location_id), player_id=player_id)


# Max alive named NPCs within a depth-2 neighborhood before we stop generating more personas
# nearby — a deterministic, non-LLM gate (per-room persona generation is the expensive part)
# that keeps distinct personalities from clustering shoulder-to-shoulder. Spawned monsters
# beyond this limit still exist and still fight — they just stay a nameless mechanical
# encounter instead of getting a full identity.
_NPC_DENSITY_LIMIT = 2


async def _maybe_spawn_entity_persona(new_room: dict, dest_id: str, theme: str,
                                      nearby_room_ids: list[str], *, campaign_id: str,
                                      recent_events: list[str] | None = None) -> None:
    """If this room's procedural gen placed a monster, decide (deterministically, from what's
    already alive nearby) whether it's worth a full LLM persona, generate one, and store it
    as a first-class `entity` row. Mutates new_room['contents'] in place so the monster's
    display name matches its generated identity before the room is even saved — call this
    BEFORE world.upsert_room. `campaign_id` is required (not resolved from a player_id, since
    this runs during background room-gen with no requesting player in scope). `recent_events`:
    nearby log text (see _generate_and_link) so a freshly-spawned NPC's persona can react to
    what's already happened around it, not invent one in a vacuum."""
    mon = next((c for c in new_room["contents"] if c.get("type") == "monster"), None)
    if not mon:
        return
    if world.count_alive_entities_in(nearby_room_ids) >= _NPC_DENSITY_LIMIT:
        return  # stays a nameless mechanical encounter — still fights fine
    kind = mon["name"]  # SRD species name (e.g. "Goblin") — capture before we overwrite it
    persona = await worldgen.generate_npc_persona(
        mon, theme, new_room["name"], new_room.get("kind", ""), new_room["description"],
        recent_events=recent_events)
    mon["name"] = persona["name"]
    world.upsert_entity(entity_id=mon["id"], campaign_id=campaign_id, kind=kind,
                        name=persona["name"], location_id=dest_id,
                        disposition=persona["disposition"], persona=persona["persona"],
                        goal=persona["goal"])
    world.log("entity.spawned",
             f"{persona['name']} the {kind} appeared in {new_room['name']} ({persona['via']}).",
             campaign_id=campaign_id, subject_type="entity", subject_id=mon["id"])


async def _generate_and_link(dest_id: str, theme: str, campaign_id: str, salt: str, *,
                             entry_from: str, back_to_id: str) -> None:
    """Generate one room and apply the same bidirectional-link fix as the reactive path —
    used by both move() (reactive) and the fan-out prefetch (speculative) below. `salt` is
    the owning campaign's — see game._seeded for why every room in a world must share it."""
    nearby_full = _nearby_region(back_to_id, depth=2)
    nearby = [(name, kind) for name, kind, _rid in nearby_full]
    # Stigmergy feeding INTO generation, not just narration: what just happened in the room
    # you're generating FROM should be able to ripple into what this new room actually is —
    # a fight/discovery next door, not a blank slate. exclude_player_id is deliberately
    # omitted here — unlike _render_scene's viewer-facing traces, generation should see
    # everyone's recent actions, including the very player who's about to walk through.
    recent_events = [e.text for e in world.recent_log(
        5, campaign_id=campaign_id, subject_type="room", subject_id=back_to_id)]
    new_room = await worldgen.generate_room_content(
        dest_id, theme, entry_from=entry_from, nearby=nearby, recent_events=recent_events, salt=salt)
    new_room["exits"][game.opposite_of(entry_from)] = back_to_id
    await _maybe_spawn_entity_persona(new_room, dest_id, theme, [rid for _, _, rid in nearby_full],
                                      campaign_id=campaign_id, recent_events=recent_events)
    world.upsert_room(room_id=dest_id, campaign_id=campaign_id, name=new_room["name"],
                      description=new_room["description"], exits=new_room["exits"],
                      contents=new_room["contents"], features=new_room.get("features"),
                      kind=new_room.get("kind", ""),
                      exit_descriptions=new_room.get("exit_descriptions"))
    world.log("room.generated", f"{new_room['name']} generated ({new_room.get('via', 'procedural')})",
             campaign_id=campaign_id)


async def _prefetch_frontier(room: Room, theme: str, campaign_id: str, salt: str) -> None:
    """Fan out generation for every exit of `room` that doesn't exist yet, in parallel, so
    whichever way the player heads next it's already there — the Flash-burst story: the world
    builds itself ahead of you. Fire-and-forget; never blocks the caller's move/look response."""
    missing = [(d, dest_id) for d, dest_id in room.exits.items() if not world.room(dest_id)]
    if not missing:
        return
    await asyncio.gather(*(
        _generate_and_link(dest_id, theme, campaign_id, salt, entry_from=d, back_to_id=room.id)
        for d, dest_id in missing
    ), return_exceptions=True)


@mcp.tool()
async def move(player_id: str, direction: str) -> str:
    """Move north/south/east/west. World-builds the next room if unexplored. The world persists."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    if dead := _dead_gate(ch):
        return dead
    camp = _require_campaign(ch.campaign_id)
    direction = direction.strip().lower()
    here = _require_room(ch.location_id)
    if direction not in here.exits:
        return f"There's no exit {direction}. Exits: {', '.join(here.exits) or 'none'}."
    dest_id = here.exits[direction]
    if not world.room(dest_id):
        # BIDIRECTIONAL LINK: the procedural generator computes its own back-exit id as
        # f"{dest_id}:{opposite}", which is NOT here.id — that would silently create a
        # duplicate room on backtrack instead of returning you home. _generate_and_link
        # forces the real link.
        await _generate_and_link(dest_id, camp.theme, camp.id, camp.salt,
                                 entry_from=direction, back_to_id=here.id)
    world.set_location(player_id, dest_id)
    world.mark_visited(dest_id)
    world.discover(player_id, dest_id)
    dest = _require_room(dest_id)
    world.log("player.moved", f"{ch.name} moved {direction} into {dest.name}", player_id=player_id)
    asyncio.create_task(_prefetch_frontier(dest, camp.theme, camp.id, camp.salt))  # fire-and-forget
    return _render_scene(dest, player_id=player_id)


@mcp.tool()
def roll_dice(expression: str = "1d20") -> str:
    """Roll dice, e.g. '1d20+3', '2d6'. The honest random heart of the game."""
    try:
        r = game.roll(expression)
    except ValueError as e:
        return f"⚠ {e}"
    return f"🎲 {expression} → rolls {r['rolls']} {'+' if r['modifier']>=0 else ''}{r['modifier']} = **{r['total']}**"


@mcp.tool()
def remember(player_id: str, note: str) -> str:
    """Record a continuity note you (the DM) want to recall later — an NPC's true motive, a
    lie you told, a promise made, a detail that should stay consistent. This is YOUR
    self-managed memory: unlike room/character state (which is rigid on purpose, for game
    mechanics), notes are free-form — write whatever's actually worth remembering, in
    whatever shape fits. get_state surfaces recent notes back to you."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    world.log("memory.noted", note, player_id=player_id)
    return "Noted."


@mcp.tool()
def log_event(player_id: str, text: str, subject_type: str | None = None,
             subject_id: str | None = None) -> str:
    """Record something the player did that no other tool covers — reading a diary,
    examining something closely, discovering a clue, any ad-hoc noteworthy moment. Unlike
    remember() (your own private continuity notes), this becomes a STIGMERGIC TRACE: later
    players who visit the same room/item/entity will see it surfaced as "Traces of those who
    came before" (same mechanism _render_scene already uses), and it appears live on the
    world event stream immediately. Call this whenever something world-changing or
    noteworthy happens that move/attack/pick_up_item/talk_to don't already cover — it's what
    makes an ad-hoc moment durable and visible to others, not just narrated and forgotten.
    subject_type/subject_id default to the player's CURRENT ROOM if omitted (the common
    case — most ad-hoc moments are about where the player currently is)."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    st, sid = (subject_type, subject_id) if subject_type else ("room", ch.location_id)
    world.log("world.event", text, player_id=player_id, subject_type=st, subject_id=sid)
    return "Recorded."


@mcp.tool()
def attack(player_id: str, weapon_bonus: int = 3, damage_dice: str = "1d8") -> str:
    """Attack the monster in your current room. Resolves d20 vs AC + damage, updates HP."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    if dead := _dead_gate(ch):
        return dead
    room = _require_room(ch.location_id)
    monster = next((c for c in room.contents if c["type"] == "monster"), None)
    if not monster:
        return "Nothing here to attack."
    # rules-accurate: attack vs the monster's REAL SRD armor class
    res = game.resolve_attack(weapon_bonus, monster.get("ac", 12), damage_dice)
    if not res["hit"]:
        out = [f"🎲 You swing at the {monster['name']} (rolled {res['attack_roll']} vs AC {monster.get('ac',12)}) — **miss**."]
    else:
        monster["hp"] -= res["damage"]
        crit = " **CRITICAL!**" if res["crit"] else ""
        out = [f"🎲 You strike the {monster['name']} for {res['damage']} damage!{crit}"]
        if monster["hp"] <= 0:
            room.contents = [c for c in room.contents if c is not monster]
            out.append(f"💀 The {monster['name']} falls!")
        else:
            out.append(f"The {monster['name']} has {monster['hp']} HP left.")
    # monster strikes back with its REAL attack (bonus + damage dice from the SRD)
    if monster["hp"] > 0:
        matk = game.resolve_attack(monster.get("attack_bonus", 3), ch.ac,
                                   monster.get("damage_dice", "1d6"))
        atk_name = monster.get("attack_name", "attack")
        if matk["hit"]:
            new_hp = world.damage(player_id, matk["damage"])
            out.append(f"⚔ The {monster['name']}'s {atk_name} hits you for {matk['damage']}. You have {new_hp} HP.")
            if new_hp <= 0:
                out.append("☠ You have fallen. The dark claims another...")
        else:
            out.append(f"⚔ The {monster['name']}'s {atk_name} misses you (rolled {matk['attack_roll']} vs AC {ch.ac}).")
    world.upsert_room(room_id=room.id, name=room.name, description=room.description,
                      exits=room.exits, contents=room.contents, features=room.features)
    world.log("combat.resolved", out[0], player_id=player_id, subject_type="room", subject_id=room.id)
    return "\n".join(out)


@mcp.tool()
async def pick_up_item(player_id: str, item_name: str | None = None) -> str:
    """Pick up something from your current room and add it to your inventory. Omit item_name
    to grab whatever pre-seeded loot is here. Pass a description to disambiguate OR to try
    picking up something that ISN'T pre-seeded loot (e.g. "the chair in the corner",
    "the child's doll") — the world-builder adjudicates whether that's actually plausible to
    carry (most furniture/fixtures aren't) and fleshes out what it is if so."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    if dead := _dead_gate(ch):
        return dead
    room = _require_room(ch.location_id)
    loot = [c for c in room.contents if c["type"] == "loot"]

    match = None
    if item_name:
        match = next((c for c in loot if item_name.lower() in c["name"].lower()), None)
    elif loot:
        match = loot[0]

    if match:
        room.contents = [c for c in room.contents if c is not match]
        world.upsert_room(room_id=room.id, campaign_id=ch.campaign_id, name=room.name,
                          description=room.description, exits=room.exits,
                          contents=room.contents, features=room.features)
        # Pre-seeded loot only ever carries a name (game.py/worldgen.py room-gen never fills
        # in flavor text) — reuse the same Flash-backed adjudicator to get a description,
        # keeping the pre-seeded name rather than whatever name it might also return.
        camp = _require_campaign(ch.campaign_id)
        flavor = await worldgen.generate_item_content(match["name"], camp.theme,
                                                       room_context=room.description)
        desc = flavor.get("description", "")
        world.add_item(player_id, {"id": match.get("id") or uuid.uuid4().hex[:8],
                                   "name": match["name"], "description": desc})
        world.log("item.picked_up", f"{ch.name} picked up {match['name']}.", player_id=player_id,
                  subject_type="room", subject_id=room.id)
        detail = f" {desc}" if desc else ""
        return f"✦ You take {match['name']}.{detail} Added to your inventory."

    if not item_name:
        return "There's nothing here to pick up."

    # Not pre-seeded loot — ask the world-builder (same procedural+Flash pattern as room-gen)
    # to adjudicate plausibility and flesh out what it actually is.
    camp = _require_campaign(ch.campaign_id)
    item = await worldgen.generate_item_content(item_name, camp.theme, room_context=room.description)
    if not item["portable"]:
        reason = f" ({item['reason']})" if item.get("reason") else ""
        return f"You can't take that{reason}."
    world.add_item(player_id, {"id": item.get("id") or uuid.uuid4().hex[:8],
                               "name": item["name"], "description": item["description"]})
    world.log("item.picked_up", f"{ch.name} picked up {item['name']}.", player_id=player_id,
              subject_type="room", subject_id=room.id)
    detail = f" {item['description']}" if item["description"] else ""
    return f"✦ You take {item['name']}.{detail} Added to your inventory."


@mcp.tool()
def drop_item(player_id: str, item_name: str) -> str:
    """Leave something from your inventory in your current room. This is the other half of
    the stigmergic model this world runs on: players never talk to or see each other directly
    (see the "ghosts" framing — you see their live position on the map, nothing more), but a
    room is shared state, so whatever you drop here is really there for the NEXT player (or
    you, later) to find and pick_up_item. No Flash call needed — the item already has its
    description from whenever it was first picked up or generated."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    room = _require_room(ch.location_id)
    match = next((i for i in ch.inventory if item_name.lower() in i["name"].lower()), None)
    if not match:
        return f"You aren't carrying anything called {item_name!r}."
    item_id = match.get("id") or match["name"]  # matches remove_item's own fallback key
    world.remove_item(player_id, item_id)
    room.contents.append({"type": "loot", "id": match.get("id") or uuid.uuid4().hex[:8],
                          "name": match["name"]})
    world.upsert_room(room_id=room.id, campaign_id=ch.campaign_id, name=room.name,
                      description=room.description, exits=room.exits,
                      contents=room.contents, features=room.features)
    world.log("item.dropped", f"{ch.name} left {match['name']} here.", player_id=player_id,
             subject_type="room", subject_id=room.id)
    return f"You set {match['name']} down."


@mcp.tool()
async def talk_to(player_id: str, message: str, npc_name: str | None = None) -> str:
    """Talk to a monster/NPC in your current room. Generates an in-character response.
    Identity and conversation memory live on the entity itself, not on you — since the world
    is shared, a different player who talks to the same NPC later sees the same persona and
    accumulated history."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    if dead := _dead_gate(ch):
        return dead
    room = _require_room(ch.location_id)
    npcs = [c for c in room.contents if c["type"] == "monster"]
    if not npcs:
        return "There's no one here to talk to."
    npc = None
    if npc_name:
        npc = next((c for c in npcs if npc_name.lower() in c["name"].lower()), None)
        if not npc:
            return f"No {npc_name!r} here. You see: {', '.join(c['name'] for c in npcs)}."
    else:
        npc = npcs[0]

    camp = _require_campaign(ch.campaign_id)
    ent = world.entity(npc["id"])
    if not ent:
        # No persona yet — the spawn-time density gate skipped it, or this NPC predates the
        # feature. Generate one lazily now that a player actually cares enough to talk to it.
        kind = npc["name"]  # SRD species name before we overwrite it below
        recent_events = [e.text for e in world.recent_log(
            5, campaign_id=ch.campaign_id, subject_type="room", subject_id=room.id)]
        gen = await worldgen.generate_npc_persona(npc, camp.theme, room.name, room.kind,
                                                  room.description, recent_events=recent_events)
        npc["name"] = gen["name"]
        ent = world.upsert_entity(entity_id=npc["id"], campaign_id=ch.campaign_id, kind=kind,
                                  name=gen["name"], location_id=room.id,
                                  disposition=gen["disposition"], persona=gen["persona"],
                                  goal=gen["goal"])
        world.upsert_room(room_id=room.id, campaign_id=ch.campaign_id, name=room.name,
                          description=room.description, exits=room.exits,
                          contents=room.contents, features=room.features)

    npc_for_llm = {**npc, "persona": ent.persona, "goal": ent.goal,
                  "disposition": ent.disposition, "conversation": ent.memory}
    result = await worldgen.generate_npc_response(npc_for_llm, camp.theme, room.description, message)
    world.append_entity_memory(ent.id, "player", message)
    world.append_entity_memory(ent.id, "npc", result["text"])
    world.log("npc.talked",
             f"{ch.name} talked to {npc['name']}: {message!r} ({result['via']}).",
             player_id=player_id, subject_type="entity", subject_id=ent.id)
    return f"💬 {npc['name']}: \"{result['text']}\""


@mcp.tool()
def delete_world(player_id: str) -> str:
    """Permanently delete YOUR OWN custom world (every room/character/log/entity in it) so you
    can start completely fresh. Two hard guards, not overridable by the caller:
    - Refuses on the shared "main" world — too precious to wipe via self-service (see the
      pod's scripts/reset_world.sh for that explicit, separately-gated admin action).
    - Refuses if any OTHER player_id currently has a character in this campaign — only the
      sole remaining player can wipe their own world; it's never safe to delete out from under
      someone else's game."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    campaign_id = ch.campaign_id
    if campaign_id == MAIN_CAMPAIGN_ID:
        return 'Can\'t delete the shared "main" world — it\'s everyone\'s persistent default.'
    others = [p for p in world.players(campaign_id) if p.player_id != player_id]
    if others:
        return (f"Can't delete this world — {len(others)} other player(s) are still in it. "
                f"Only the sole remaining player can wipe a world.")
    world.delete_campaign(campaign_id)
    return (f"🗑 World {campaign_id!r} deleted. Call start_adventure(campaign_id=\"new\") to "
            f"begin a brand-new one.")


@mcp.tool()
def character_sheet(player_id: str) -> str:
    """Show your character: stats, HP, AC, inventory."""
    ch = world.character(player_id)
    if not ch:
        return "Unknown player_id. Call start_adventure first."
    stats = "  ".join(f"{k} {v}" for k, v in ch.stats.items())
    items = ", ".join(i["name"] for i in ch.inventory) or "empty"
    return (f"**{ch.name}** — level {ch.level} {ch.klass}\n"
            f"HP {ch.hp}/{ch.max_hp}   AC {ch.ac}\n{stats}\n"
            f"Inventory: {items}")


@mcp.tool()
def get_state(player_id: str) -> dict:
    """Full inspectable state for your character — proves the world remembers across turns."""
    return world.snapshot(player_id)


# --- Second world: a task graph, proving the engine generalizes beyond D&D ------------------
# Same shape as the D&D tools (look / traverse-by-relation / one action that mutates + links),
# fully independent state (TicketWorld, its own SQLite file). No player_id/character concept
# here — any agent can inspect or act on any ticket, there's no per-agent position to track.

@mcp.tool()
def seed_demo_tickets() -> str:
    """Seed the ticket graph with a real example task set (tonight's actual DNDMCP work) for
    demoing traversal + completion-triggered generation. Safe to call multiple times —
    overwrites by id."""
    t1 = tickets.new_ticket(ticket_id="t1", title="Wire flash_llm.py to confirmed vLLM endpoint",
                            description="Point the game's LLM calls at the deployed, verified-working endpoint instead of an untested one.",
                            status="done", priority="high")
    t2 = tickets.new_ticket(ticket_id="t2", title="Add multiplayer support to DNDMCP",
                            description="Shared world, per-player character and location, no wipe-on-join.",
                            status="done", priority="high")
    t3 = tickets.new_ticket(ticket_id="t3", title="Build NPC conversation system",
                            description="Stable NPC identity + stored conversation history, generated dialogue via Flash.",
                            status="done", priority="medium")
    t4 = tickets.new_ticket(ticket_id="t4", title="Deploy DNDMCP pod for public demo",
                            description="Stand up a Runpod pod running the MCP server + GUI so it's reachable outside localhost.",
                            status="todo", priority="high")
    t5 = tickets.new_ticket(ticket_id="t5", title="Record hackathon demo video",
                            description="Script + record the submission video showing live play.",
                            status="todo", priority="high")
    t6 = tickets.new_ticket(ticket_id="t6", title="Add monster loot generation on defeat",
                            description="Generate a loot drop when a monster's HP hits 0, instead of it just vanishing.",
                            status="todo", priority="low")
    tickets.link(t1.id, t4.id, "blocks")
    tickets.link(t4.id, t5.id, "blocks")
    tickets.link(t2.id, t3.id, "related_to")
    tickets.link(t3.id, t6.id, "related_to")
    return f"Seeded {len(tickets.all_tickets())} tickets."


@mcp.tool()
def list_tickets() -> str:
    """List all tickets in the task graph with their status."""
    ts = tickets.all_tickets()
    if not ts:
        return "No tickets yet — call seed_demo_tickets first."
    return "\n".join(f"[{t.status:^11}] {t.id}  {t.title}  (priority: {t.priority})" for t in ts)


@mcp.tool()
def look_at_ticket(ticket_id: str) -> str:
    """Describe one ticket: title, description, status, and its related tickets (the graph
    edges) — traverse by calling this again with a neighbor's id."""
    t = tickets.ticket(ticket_id)
    if not t:
        return f"No ticket {ticket_id!r}."
    lines = [f"## {t.title}", f"[{t.status}] priority: {t.priority}", "", t.description, "", "Related:"]
    neighbors = tickets.neighbors(ticket_id)
    if not neighbors:
        lines.append("  (none)")
    for rel, n in neighbors:
        lines.append(f"  {rel} → {n.id}: {n.title} [{n.status}]")
    return "\n".join(lines)


@mcp.tool()
async def complete_ticket(ticket_id: str) -> str:
    """Mark a ticket done, then generate one plausible follow-up ticket informed by its graph
    neighbors, and link it in — the task-graph equivalent of move() generating the next room."""
    t = tickets.ticket(ticket_id)
    if not t:
        return f"No ticket {ticket_id!r}."
    tickets.set_status(ticket_id, "done")
    neighbors = tickets.neighbors(ticket_id)
    gen = await linear_gen.generate_followup_ticket(t, neighbors)
    follow_up = tickets.new_ticket(title=gen["title"], description=gen["description"],
                                   priority=gen["priority"])
    tickets.link(t.id, follow_up.id, "led_to")
    return (f"✅ {t.title} marked done.\n\n"
            f"→ Generated follow-up ({gen['via']}): **{follow_up.title}** ({follow_up.id})\n"
            f"{follow_up.description}")


def main() -> None:
    """stdio locally (Claude Desktop launches it); HTTP on a pod (remote brain).

    DNDMCP_TRANSPORT=http + PORT=8000 → streamable-http on 0.0.0.0 (pod, behind proxy).
    Default = stdio.
    """
    import os

    from mcp.server.transport_security import TransportSecuritySettings

    transport = os.environ.get("DNDMCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "sse"):
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        # FastMCP's default DNS-rebinding protection only allow-lists localhost Host
        # headers (mcp/server/fastmcp/server.py, set at construction time since the
        # default host is 127.0.0.1) — every request through the pod's public proxy
        # domain gets a 421 Misdirected Request. This server is MEANT to be reached at
        # its public URL (that's the whole pod-hosted multiplayer premise), so disable it.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)
        mcp.run(transport="sse" if transport == "sse" else "streamable-http")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
