"""Throwaway seed script for manually testing the graph-enrichment work (feat/graph-
enrichment) — NOT part of the app, never imported by it. Populates a small "main" world with
~10 rooms/edges, mixed loot/monster contents (including one room with >3 items to exercise
the "+n" overflow pip), a couple of undiscovered rooms, and a handful of characters (one
`is_bot=1`, two rooms with multiple occupants to exercise the ghost-dot stacking) directly
into whatever DNDMCP_STATE_DIR points at.

Usage (from this worktree root, against the isolated dev_worktree.sh state dir):

    DNDMCP_STATE_DIR=~/.dndmcp_worktrees/<worktree-name> \
      .venv/bin/python -m scripts.seed_demo_world

Run this AFTER dev_worktree.sh has started at least once (so the state dir/DB exists and the
schema is initialized) or BEFORE (State() below creates+initializes the DB itself either way).
Safe to re-run: upsert_room/new_character are both idempotent (ON CONFLICT / INSERT OR
REPLACE), though `discover` edges could technically double up across runs — harmless (only
ever read as a set/EXISTS check).

Art: every image_ref below is a made-up ref with NO file under DNDMCP_STATE_DIR/art/ — the
/art/{ref}.png route 404s, which is exactly what exercises the medallion's onerror fallback
to the plain colored circle. That's intentional, not a bug in this script.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dndmcp.state import World, MAIN_CAMPAIGN_ID  # noqa: E402


def main() -> None:
    world = World()
    print(f"seeding into: {world.path}")

    if not world.campaign_exists(MAIN_CAMPAIGN_ID):
        world.create_campaign(
            MAIN_CAMPAIGN_ID,
            theme="clockwork ruins",
            premise="A dead civilization's arcane-tech collapse -- now ruins, ghosts, and the "
                    "faint tick of gears that shouldn't still be turning.",
            start_room="r0",
            name="The Sundered Weave",
        )
        print("created campaign 'main'")
    else:
        print("campaign 'main' already exists -- reusing it")

    def loot(name: str) -> dict:
        return {"type": "loot", "name": name, "id": f"item-{name[:12].replace(' ', '_')}"}

    def monster(name: str, hp: int, ac: int = 12) -> dict:
        return {"type": "monster", "name": name, "hp": hp, "max_hp": hp, "ac": ac, "cr": 1,
                "traits": [], "attack_bonus": 3, "damage_dice": "1d6", "attack_name": "claw"}

    rooms = [
        dict(room_id="r0", name="Sundered Atrium", kind="atrium",
             description="A vaulted entry hall, its brass dome cracked open to the grey sky.",
             features=["a fallen chandelier of fused gears", "moss climbing the support struts"],
             exits={"north": "r1", "east": "r2", "south": "r6"},
             contents=[loot("a cracked brass compass")],
             image_ref="r0", visited=True),
        dict(room_id="r1", name="Cracked Observatory", kind="observatory",
             description="A shattered glass dome once used to track something no longer in the sky.",
             features=["a bent brass telescope"],
             exits={"south": "r0", "north": "r3"},
             contents=[monster("Rust Wraith", 14, 11)],
             image_ref=None, visited=True),
        dict(room_id="r2", name="Rusted Forge", kind="forge",
             description="Cold furnaces line the walls, their bellows long since seized shut.",
             features=["a cold anvil", "scattered tongs"],
             exits={"west": "r0", "east": "r4"},
             contents=[loot("a tarnished gearwheel"), loot("a vial of oil"),
                       monster("Cog Sentinel", 20, 14)],
             image_ref="r2", visited=True),
        dict(room_id="r3", name="Collapsed Archive", kind="archive",
             description="Shelves of waterlogged scrolls slump against a caved-in far wall.",
             features=["a locked iron cabinet"],
             exits={"south": "r1", "east": "r8"},
             contents=[loot("a bent key"), loot("a shard of blue glass"),
                       loot("an oil-stained journal"), loot("a spool of copper wire")],
             image_ref=None, visited=True),
        dict(room_id="r4", name="Ember Vault", kind="vault",
             description="A sealed chamber still faintly warm, gear-teeth glowing dull orange.",
             features=["a bank of dead furnace-hearts"],
             exits={"west": "r2", "south": "r5"},
             contents=[monster("Ashfall Hound", 16, 13), loot("a warm ember stone")],
             image_ref="r4", visited=True),
        dict(room_id="r5", name="Buried Reliquary", kind="reliquary",
             description="Not yet discovered.",
             features=["something ticking, faintly, under the rubble"],
             exits={"north": "r4"},
             contents=[monster("Deep Cog Wyrm", 26, 15)],
             image_ref=None, visited=False),
        dict(room_id="r6", name="Whispering Cistern", kind="cistern",
             description="Still black water reflects a ceiling of corroded pipework.",
             features=["a rope ladder, half-rotted"],
             exits={"north": "r0", "east": "r7"},
             contents=[loot("a waterlogged ledger")],
             image_ref="r6", visited=True),
        dict(room_id="r7", name="Flooded Undercroft", kind="undercroft",
             description="Not yet discovered.",
             features=[],
             exits={"west": "r6"},
             contents=[],
             image_ref=None, visited=False),
        dict(room_id="r8", name="Gearwork Sanctum", kind="sanctum",
             description="A ring of dormant automatons kneel in a circle around a dead altar.",
             features=["a dais of interlocking brass rings"],
             exits={"west": "r3", "north": "r9"},
             contents=[monster("Gearwork Sentinel", 30, 16), loot("a cog-shaped amulet")],
             image_ref="r8", visited=True),
        dict(room_id="r9", name="Sundering Threshold", kind="threshold",
             description="The last intact doorway before the ruin gives out entirely into fog.",
             features=["a threshold rune, still faintly lit"],
             exits={"south": "r8", "east": "r9:frontier"},  # dangling exit -> frontier dashed line
             contents=[],
             image_ref=None, visited=True),
    ]

    visited_room_ids: list[str] = []
    for r in rooms:
        exits = r.pop("exits")
        visited = r.pop("visited")
        room = world.upsert_room(campaign_id=MAIN_CAMPAIGN_ID, exits=exits, **r)
        if visited:
            world.mark_visited(room.id)
            visited_room_ids.append(room.id)
        print(f"  room {room.id}: {room.name!r} visited={visited} image_ref={room.image_ref!r} "
              f"contents={len(room.contents)}")

    characters = [
        dict(name="Aria Voss", klass="Tinker Ranger", location_id="r0", is_bot=False),
        dict(name="Kex-7", klass="Automaton Scout", location_id="r0", is_bot=True),
        dict(name="Bramwell Tott", klass="Ember Cleric", location_id="r4", is_bot=False),
        dict(name="Nyra Quill", klass="Gearlock Rogue", location_id="r4", is_bot=False),
        dict(name="Doln-3", klass="Salvage Automaton", location_id="r4", is_bot=True),
    ]
    player_ids = {}
    for i, c in enumerate(characters):
        player_id = f"demo{i:02d}{'0' * 6}"[:12]
        is_bot = c.pop("is_bot")
        location_id = c.pop("location_id")
        ch = world.new_character(
            player_id, MAIN_CAMPAIGN_ID,
            hp=20, ac=13, stats={"STR": 14, "DEX": 13, "CON": 12, "INT": 11, "WIS": 12, "CHA": 10},
            inventory=[{"name": "a worn traveler's cloak"}],
            location_id=location_id, **c,
        )
        if is_bot:
            world.mark_bot(player_id)
        world.discover(player_id, location_id)
        player_ids[ch.name] = player_id
        print(f"  character {player_id}: {c['name']} ({c['klass']}) in {location_id} "
              f"bot={is_bot}")

    # /state's per-VIEWING-PLAYER discovery (only used when ?player= is given -- an anonymous
    # spectator view falls back to the global `visited` flag instead, see /state's own
    # comment) means a character normally only "knows" the rooms they've personally walked
    # through. For a useful ?player=<Aria> demo (not just her own starting room showing real
    # detail and everything else "???"), give her credit for having explored every VISITED
    # room, as if she'd been playing a while -- same discover() mechanism a real playthrough
    # would build up one room at a time.
    aria_id = player_ids["Aria Voss"]
    for room_id in visited_room_ids:
        world.discover(aria_id, room_id)
    print(f"  (also marked {aria_id} as having discovered every visited room, for a richer "
          f"?player= demo)")

    print()
    print("Seed complete. Try, e.g.:")
    print(f"  <gui-url>/?player={player_ids['Aria Voss']}   "
          "(plays as Aria -- teal 'mine' ring on r0)")
    print("  <gui-url>/                                     (anonymous spectator view)")


if __name__ == "__main__":
    main()
