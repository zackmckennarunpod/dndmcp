"""World-builder — the PRIMARY Flash use. Generates the world ahead of the player.

Traversal-driven: when the player heads toward unexplored space, a Flash model cheaply
generates the next room's rich content (vivid description, specific features, what dwells
there) and we store it in the graph. By the time the agent enters and queries the room,
it's ready. Burst to generate, scale to zero when exploration pauses.

Stub→real: with Flash off (default), falls back to the procedural generator (game.py) so the
world still builds — just template-rich instead of model-rich. Flip FLASH_WORLDGEN=1 to use Flash.
"""

from __future__ import annotations

import json
import random

from . import compendium, flash_llm, game, setting

# Theme → real SRD creatures that fit (so spawns are rules-accurate AND on-theme).
_THEME_CREATURES = {
    "gothic horror": ["Ghoul", "Skeleton", "Zombie", "Ghost", "Specter", "Shadow",
                      "Wight", "Swarm of Rats", "Cultist", "Giant Rat"],
    "default": ["Goblin", "Kobold", "Giant Spider", "Skeleton", "Giant Rat",
                "Bandit", "Wolf", "Stirge"],
}


def _creatures_for(theme: str) -> list[str]:
    for key in _THEME_CREATURES:
        if key in (theme or "").lower():
            return _THEME_CREATURES[key]
    return _THEME_CREATURES["default"]


# SKILL: describe_room — directional/spatial room generation, STRUCTURED JSON out.
_ROOM_JSON = ('{"name": short evocative room name, "kind": one or two words, '
              '"look": {"ahead": "...", "left": "...", "right": "...", "center": "..."}, '
              '"feature": one specific examinable detail, "has_monster": true or false}')


def _room_messages(theme: str, came_from: str | None, exits: list[str]) -> list[dict]:
    system = (f"{setting.GEN_BRIEF}\n\n"
              f"You are the world-builder for a {theme} dungeon crawl in this setting. You invent "
              f"vivid rooms described SPATIALLY (what is ahead, to the sides, in the center), on-setting, "
              f"and you reply with STRICT JSON only — no text outside the JSON object.")
    enter = f" the player enters from the {came_from}" if came_from else " the player descends into"
    user = (f"Generate the next room{enter}. Exits lead: {', '.join(exits) or 'none'}. "
            f"Describe it directionally so the player knows what is where. "
            f"Return JSON: {_ROOM_JSON}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _compose_look(look: dict) -> str:
    parts = []
    for dir_, key in [("Ahead", "ahead"), ("To your left", "left"),
                      ("To your right", "right"), ("At the center", "center")]:
        v = (look or {}).get(key)
        if v:
            parts.append(f"{dir_}, {v.rstrip('.').lower() if dir_!='Ahead' else v.rstrip('.')}.")
    return " ".join(parts)


async def generate_room_content(room_id: str, theme: str, *, entry_from: str | None = None,
                                neighbors: list[str] | None = None) -> dict:
    """Generate a room's content for the graph. Tries Flash (structured JSON); falls back
    to procedural. Returns game.generate_room's shape + `features` + a `via` marker."""
    rng = game._seeded(room_id)  # deterministic per room id
    base = game.generate_room(room_id, theme, entry_from=entry_from)  # procedural skeleton (exits etc.)

    via = "procedural"
    want_monster = any(c.get("type") == "monster" for c in base["contents"])
    messages = _room_messages(theme, entry_from, list(base["exits"].keys()))
    gen = await flash_llm.generate(messages, max_tokens=280, temperature=0.95)
    if gen:
        try:
            data = json.loads(gen[gen.find("{"): gen.rfind("}") + 1])
            if data.get("name"):
                base["name"] = data["name"]
            look = _compose_look(data.get("look", {}))
            if look:
                base["description"] = look
            elif data.get("description"):
                base["description"] = data["description"]
            if data.get("feature"):
                base.setdefault("features", []).append(data["feature"])
            want_monster = bool(data.get("has_monster", want_monster))
            via = "flash"
        except Exception:
            pass  # malformed JSON → keep procedural content

    # add 1-2 procedural features for texture (the liveness layer)
    t = game._theme(theme)
    feats = base.setdefault("features", [])
    for f in rng.sample(t["features"], k=min(2, len(t["features"]))):
        if f not in feats:
            feats.append(f)

    # place a REAL SRD monster (rules-accurate) if wanted
    base["contents"] = [c for c in base["contents"] if c.get("type") != "monster"]
    if want_monster:
        mon = compendium.encounter_from_names(_creatures_for(theme), rng)
        if mon:
            base["contents"].append(mon)
        elif (rm := compendium.random_encounter(1.0, rng)):
            base["contents"].append(rm)
    base["via"] = via
    return base
