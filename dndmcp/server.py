"""DNDMCP — a solo-RPG Dungeon Master as an MCP server (stdio).

Install once, play from any harness. The server is the rules engine + persistent world;
your agent is the storyteller. All through MCP tools; output is text/ASCII (any terminal
harness) + optional GPU art (GUI harnesses).

Run / install (Claude Desktop config):
    "dndmcp": { "command": "/abs/.venv/bin/python", "args": ["-m", "dndmcp.server"] }
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import art, compendium, game, worldgen
from .state import World

# Shipped WITH the server so connecting DNDMCP makes the agent assume the DM role.
DM_PERSONA = """You are the Dungeon Master for a solo tabletop RPG running on DNDMCP. The
terminal IS the game. When this server is connected, BECOME a vivid, fair Dungeon Master.

How to run the game:
- Begin by greeting the player and offering to start an adventure (ask for a theme + character,
  or pick something evocative). Call start_adventure to begin.
- Set each scene richly from the tool output, then ALWAYS end your turn with "What do you do?"
- The player explores by telling you their intent. Translate intent into tool calls:
    move there        -> move(direction)
    any check/attack  -> roll_dice / attack  (NEVER invent dice — always call the tool)
    look around       -> look      check self -> character_sheet     recap -> get_state
- Narrate results dramatically but keep mechanics HONEST: use the exact numbers the tools return.
- The world is PERSISTENT — the tools remember. Refer back to what happened; the world is real.
- Keep it terminal-friendly: short paragraphs, show the ASCII map/art from tools, give clear choices.
- Be a fair DM: let dice and rules decide; build tension; reward clever play."""

mcp = FastMCP("dndmcp", instructions=DM_PERSONA)
world = World()


@mcp.prompt()
def be_the_dm() -> str:
    """Invoke to make your agent assume the Dungeon Master role and start a session."""
    return DM_PERSONA + "\n\nGreet me and offer to begin an adventure."


def _render_scene(room: dict, *, ambient: bool = True, with_art: bool = True) -> str:
    """Text/ASCII render of a room — the universal (terminal) output."""
    lines = [f"## {room['name'].title()}", "", room["description"]]
    for f in room.get("features", []):
        lines.append(f"  • {f}")
    for c in room["contents"]:
        if c["type"] == "monster":
            cr = f", CR {c.get('cr')}" if c.get("cr") is not None else ""
            traits = f" [{', '.join(c['traits'])}]" if c.get("traits") else ""
            lines.append(f"\n⚔  A {c['name']} is here (AC {c.get('ac','?')}, HP {c['hp']}{cr}).{traits} It looks hostile.")
        elif c["type"] == "loot":
            lines.append(f"\n✦  You notice {c['name']}.")
    if ambient:
        camp = world.campaign()
        lines.append(f"\n_{game.ambient_event(camp['theme'] if camp else 'default')}_")
    lines.append("")
    lines.append(game.ascii_map(world))
    if with_art:
        a = art.generate(f"{room['name']}: {room['description']}", kind="scene")
        lines.append("\n" + a["ascii"])
        if not a["enabled"]:
            lines.append("(art: stubbed — GPU image gen not yet wired)")
    return "\n".join(lines)


@mcp.tool()
async def start_adventure(theme: str = "gothic horror", character_name: str = "Wanderer",
                          character_class: str = "Fighter") -> str:
    """Begin a new solo RPG. Generates a premise, your character, and the opening room
    (world-builder: Flash-generated when enabled, else procedural). Wipes any previous campaign."""
    ch = game.new_character(character_name, character_class)
    start_id = "r0"
    room = await worldgen.generate_room_content(start_id, theme)
    premise = (f"A {theme} adventure. {ch['name']} the {ch['klass']} descends into the dark, "
               f"seeking what others feared to find.")
    world.new_campaign(theme=theme, premise=premise, start_room=start_id)
    world.set_character(name=ch["name"], klass=ch["klass"], hp=ch["hp"], ac=ch["ac"],
                        stats=ch["stats"], inventory=ch["inventory"])
    world.upsert_room(room_id=start_id, name=room["name"], description=room["description"],
                      exits=room["exits"], contents=room["contents"], features=room.get("features"))
    world.mark_visited(start_id)
    world.log("start", premise)
    return (f"# {premise}\n\nYou are **{ch['name']}**, a level 1 {ch['klass']} "
            f"(HP {ch['hp']}, AC {ch['ac']}).\n\n" + _render_scene(room))


@mcp.tool()
def look() -> str:
    """Describe the current room again (scene, exits, contents, map)."""
    camp = world.campaign()
    if not camp:
        return "No active adventure. Call start_adventure first."
    room = world.room(camp["current_room"])
    return _render_scene(room)


@mcp.tool()
async def move(direction: str) -> str:
    """Move north/south/east/west. World-builds the next room if unexplored. The world persists."""
    camp = world.campaign()
    if not camp:
        return "No active adventure. Call start_adventure first."
    direction = direction.strip().lower()
    here = world.room(camp["current_room"])
    if direction not in here["exits"]:
        return f"There's no exit {direction}. Exits: {', '.join(here['exits']) or 'none'}."
    dest_id = here["exits"][direction]
    if not world.room(dest_id):
        new_room = await worldgen.generate_room_content(
            dest_id, camp["theme"], entry_from=direction, neighbors=[here["name"]])
        world.upsert_room(room_id=dest_id, name=new_room["name"], description=new_room["description"],
                          exits=new_room["exits"], contents=new_room["contents"],
                          features=new_room.get("features"))
    world.set_room(dest_id)
    world.mark_visited(dest_id)
    world.log("move", f"moved {direction} into {world.room(dest_id)['name']}")
    return _render_scene(world.room(dest_id))


@mcp.tool()
def roll_dice(expression: str = "1d20") -> str:
    """Roll dice, e.g. '1d20+3', '2d6'. The honest random heart of the game."""
    try:
        r = game.roll(expression)
    except ValueError as e:
        return f"⚠ {e}"
    return f"🎲 {expression} → rolls {r['rolls']} {'+' if r['modifier']>=0 else ''}{r['modifier']} = **{r['total']}**"


@mcp.tool()
def attack(weapon_bonus: int = 3, damage_dice: str = "1d8") -> str:
    """Attack the monster in the current room. Resolves d20 vs AC + damage, updates HP."""
    camp = world.campaign()
    if not camp:
        return "No active adventure."
    room = world.room(camp["current_room"])
    monster = next((c for c in room["contents"] if c["type"] == "monster"), None)
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
            room["contents"] = [c for c in room["contents"] if c is not monster]
            out.append(f"💀 The {monster['name']} falls!")
        else:
            out.append(f"The {monster['name']} has {monster['hp']} HP left.")
    # monster strikes back with its REAL attack (bonus + damage dice from the SRD)
    if monster["hp"] > 0:
        ch = world.character()
        matk = game.resolve_attack(monster.get("attack_bonus", 3), ch["ac"],
                                   monster.get("damage_dice", "1d6"))
        atk_name = monster.get("attack_name", "attack")
        if matk["hit"]:
            new_hp = world.damage(matk["damage"])
            out.append(f"⚔ The {monster['name']}'s {atk_name} hits you for {matk['damage']}. You have {new_hp} HP.")
            if new_hp <= 0:
                out.append("☠ You have fallen. The dark claims another...")
        else:
            out.append(f"⚔ The {monster['name']}'s {atk_name} misses you (rolled {matk['attack_roll']} vs AC {ch['ac']}).")
    world.upsert_room(room_id=room["id"], name=room["name"], description=room["description"],
                      exits=room["exits"], contents=room["contents"], features=room.get("features"))
    world.log("combat", out[0])
    return "\n".join(out)


@mcp.tool()
def character_sheet() -> str:
    """Show your character: stats, HP, AC, inventory."""
    ch = world.character()
    if not ch:
        return "No character yet. Call start_adventure."
    stats = "  ".join(f"{k} {v}" for k, v in ch["stats"].items())
    return (f"**{ch['name']}** — level {ch['level']} {ch['klass']}\n"
            f"HP {ch['hp']}/{ch['max_hp']}   AC {ch['ac']}\n{stats}\n"
            f"Inventory: {', '.join(ch['inventory']) or 'empty'}")


@mcp.tool()
def get_state() -> dict:
    """Full inspectable campaign state — proves the world remembers across turns."""
    return world.snapshot()


def main() -> None:
    """stdio locally (Claude Desktop launches it); HTTP on a pod (remote brain).

    DNDMCP_TRANSPORT=http + PORT=8000 → streamable-http on 0.0.0.0 (pod, behind proxy).
    Default = stdio.
    """
    import os

    transport = os.environ.get("DNDMCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "sse"):
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        mcp.run(transport="sse" if transport == "sse" else "streamable-http")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
