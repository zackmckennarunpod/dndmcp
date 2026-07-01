"""Game engine — dice, procedural rooms, combat, ASCII rendering.

The server is the RULES ENGINE (structure, dice, state); the agent is the STORYTELLER
(narration on top). Deterministic-ish given a seed so a session is reproducible.
"""

from __future__ import annotations

import hashlib
import random
import re
import uuid

DIRECTIONS = ["north", "south", "east", "west", "up", "down"]
OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east",
            "up": "down", "down": "up"}


def opposite_of(direction: str) -> str:
    """Best-effort reverse of an exit label. Falls back to a generic 'back' for anything
    outside the known set — e.g. a future free-form label like 'through the broken wall'
    that has no natural single-word opposite."""
    return OPPOSITE.get(direction, "back")

_THEMES = {
    "gothic horror": {
        "rooms": ["crypt", "candlelit chapel", "flooded ossuary", "portrait gallery",
                  "moonlit cloister", "blood-stained study", "iron-barred cell"],
        "monsters": [("ghoul", 12, 4), ("revenant", 18, 6), ("swarm of rats", 7, 2),
                     ("cloaked cultist", 10, 4)],
        "loot": ["a tarnished silver locket", "a vial of holy water", "a rusted iron key",
                 "a page torn from a forbidden tome"],
        # specific, examinable details that make a room feel like a PLACE
        "features": ["a cracked stone sarcophagus, its lid shifted askew",
                     "name-plates worn smooth by centuries of damp",
                     "a draft hissing from a hairline crack in the wall",
                     "wax stalactites where candles once guttered and died",
                     "a faded fresco of robed figures bowing to something unseen",
                     "claw-marks gouged deep into the doorframe",
                     "a puddle reflecting torchlight that shouldn't be there",
                     "a child's doll, seated upright, facing the corner"],
        # ambient events — the world doing things on its own
        "ambient": ["Somewhere far below, chanting rises and falls, then stops.",
                    "Your torch gutters; for a heartbeat the shadows lunge inward.",
                    "A cold draft carries the smell of grave-soil and old incense.",
                    "Something skitters across the ceiling and is gone.",
                    "You hear a single, distant bell — though no bell hangs here.",
                    "The walls seem to breathe, very slowly, and you tell yourself it's the dark.",
                    "A whisper brushes your ear — your own name, in a voice you almost know."],
    },
    "default": {
        "rooms": ["stone chamber", "dripping cavern", "collapsed hall", "torchlit corridor",
                  "mushroom grotto", "ancient vault", "rope bridge over a chasm"],
        "monsters": [("goblin", 7, 3), ("giant spider", 11, 4), ("skeleton", 13, 4),
                     ("kobold", 5, 2)],
        "loot": ["a pouch of gold", "a healing potion", "an old map fragment", "a glowing gem"],
        "features": ["a collapsed pillar half-blocking the way",
                     "moss glowing faintly blue along the seams of the stone",
                     "a dried-up fountain choked with rubble",
                     "scratch-tally marks scored into the wall by some prisoner",
                     "a rusted iron grate in the floor, dark air rising through it",
                     "bones picked clean, arranged in a deliberate spiral",
                     "an old campfire, ashes cold, a bedroll rotted to threads"],
        "ambient": ["Water drips somewhere in the dark, steady as a heartbeat.",
                    "A low rumble passes through the stone and fades.",
                    "Far off, something heavy drags itself across rock.",
                    "The air shifts — for a moment it smells of open sky, impossibly.",
                    "Pebbles trickle down from the ceiling. Something disturbed them.",
                    "Your torchlight catches eyes in a far tunnel. They blink, and vanish."],
    },
}


def ambient_event(theme: str, rng: random.Random | None = None) -> str:
    """An atmospheric thing the world does on its own (not player-triggered)."""
    choice = rng.choice if rng is not None else random.choice
    return choice(_theme(theme)["ambient"])


def _theme(theme: str) -> dict:
    for key in _THEMES:
        if key in (theme or "").lower():
            return _THEMES[key]
    return _THEMES["default"]


def roll(expr: str) -> dict:
    """Roll dice like '1d20+3', '2d6', 'd20'. Returns rolls + total."""
    m = re.fullmatch(r"\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*", (expr or "").lower())
    if not m:
        raise ValueError(f"bad dice expression: {expr!r} (try '1d20+3')")
    count = int(m.group(1) or 1)
    sides = int(m.group(2))
    mod = int((m.group(3) or "0").replace(" ", ""))
    if count < 1 or count > 100 or sides < 2 or sides > 1000:
        raise ValueError("dice out of range")
    rolls = [random.randint(1, sides) for _ in range(count)]
    return {"expr": expr, "rolls": rolls, "modifier": mod, "total": sum(rolls) + mod}


def new_character(name: str, klass: str) -> dict:
    """Roll a starting character (4d6-drop-lowest-ish, simplified)."""
    def stat() -> int:
        return sum(sorted(random.randint(1, 6) for _ in range(4))[1:])
    stats = {s: stat() for s in ["STR", "DEX", "CON", "INT", "WIS", "CHA"]}
    hp = 8 + (stats["CON"] - 10) // 2
    starting_kit = [
        {"name": "a torch", "description": "casts a small circle of light; burns for hours yet."},
        {"name": "a worn dagger", "description": "unremarkable, but sharp enough to matter."},
        {"name": "rations", "description": "a few days' worth of dry, plain food."},
    ]
    return {"name": name, "klass": klass, "hp": max(hp, 4), "ac": 12 + (stats["DEX"] - 10) // 2,
            "stats": stats, "inventory": starting_kit}


def _seeded(room_id: str, salt: str = "") -> random.Random:
    """Deterministic per (room_id, salt) — same room_id in the SAME world always resolves the
    same way (reproducible within a session/world), but different worlds (different salt,
    minted once per campaign) don't generate identical rooms even from the same starting
    room_id ("r0"). Without a salt, every fresh world was hashing the exact same string and
    landing on the exact same "random" room name every time — the bug this fixes."""
    return random.Random(int(hashlib.sha1(f"{salt}:{room_id}".encode()).hexdigest()[:8], 16))


def generate_room(room_id: str, theme: str, *, entry_from: str | None = None, salt: str = "") -> dict:
    """Procedurally build a room: name, exits, and contents (monster/loot/nothing)."""
    rng = _seeded(room_id, salt)
    t = _theme(theme)
    name = rng.choice(t["rooms"])
    # 1-3 exits, always include the way back if we entered from somewhere
    n_exits = rng.randint(1, 3)
    exits = set(rng.sample(DIRECTIONS, k=min(n_exits, len(DIRECTIONS))))
    if entry_from:
        exits.add(opposite_of(entry_from))
        # vertical continuity: a passage that's taking you down (or up) has a real chance of
        # continuing the same way — the cellar keeps going down, not just dead-ending sideways.
        if entry_from in ("up", "down") and rng.random() < 0.5:
            exits.add(entry_from)
    exit_map = {d: f"{room_id}:{d}" for d in exits}

    contents = []
    roll_kind = rng.random()
    if roll_kind < 0.45:
        mon, hp, dmg = rng.choice(t["monsters"])
        contents.append({"type": "monster", "id": uuid.uuid4().hex[:8], "name": mon,
                         "hp": hp, "max_hp": hp, "damage": dmg})
    elif roll_kind < 0.75:
        contents.append({"type": "loot", "id": uuid.uuid4().hex[:8], "name": rng.choice(t["loot"])})
    return {"id": room_id, "name": name, "exits": exit_map, "contents": contents,
            "description": f"You stand in {name_with_article(name)}.", "kind": ""}


def name_with_article(name: str) -> str:
    return ("an " if name[0] in "aeiou" else "a ") + name


def resolve_attack(attacker_bonus: int, target_ac: int, damage_dice: str) -> dict:
    """A single attack: d20+bonus vs AC, then damage on hit."""
    atk = roll(f"1d20+{attacker_bonus}")
    hit = atk["total"] >= target_ac or atk["rolls"][0] == 20
    crit = atk["rolls"][0] == 20
    dmg = 0
    if hit:
        d = roll(damage_dice)
        dmg = d["total"] * (2 if crit else 1)
    return {"attack_roll": atk["total"], "natural": atk["rolls"][0], "hit": hit,
            "crit": crit, "damage": dmg, "target_ac": target_ac}


def ascii_map(room: dict | None) -> str:
    """Tiny ASCII minimap around the given room (terminal render)."""
    if not room:
        return "(no map yet)"
    cell = "[*]"  # current
    around = {d: ("[ ]" if d in room["exits"] else "   ") for d in DIRECTIONS}
    return (f"        {around['north']}\n"
            f"     {around['west']} {cell} {around['east']}\n"
            f"        {around['south']}\n"
            f"  exits: {', '.join(room['exits']) or 'none'}")
