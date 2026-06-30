"""SRD compendium — the rules authority, baked into the brain (offline, self-contained).

Loads the vendored 5e SRD (Monsters, Conditions) and gives the engine REAL stat blocks so
spawned monsters and combat are rules-accurate. Also powers the lookup tools the DM agent
calls so it never hallucinates a creature's abilities.
"""

from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path

_SRD = Path(__file__).parent / "srd"


@lru_cache(maxsize=1)
def _monsters() -> list[dict]:
    p = _SRD / "Monsters.json"
    return json.loads(p.read_text()) if p.exists() else []


@lru_cache(maxsize=1)
def _conditions() -> list[dict]:
    p = _SRD / "Conditions.json"
    return json.loads(p.read_text()) if p.exists() else []


@lru_cache(maxsize=1)
def _by_name() -> dict[str, dict]:
    return {m["name"].lower(): m for m in _monsters()}


def get_monster(name: str) -> dict | None:
    """Exact (then fuzzy) monster lookup."""
    m = _by_name().get((name or "").lower().strip())
    if m:
        return m
    needle = (name or "").lower().strip()
    return next((mon for mon in _monsters() if needle and needle in mon["name"].lower()), None)


def search_monsters(query: str, limit: int = 8) -> list[str]:
    q = (query or "").lower()
    return [m["name"] for m in _monsters() if q in m["name"].lower()][:limit]


def _ac(m: dict) -> int:
    ac = m.get("armor_class")
    if isinstance(ac, list) and ac:
        return ac[0].get("value", 10)
    if isinstance(ac, int):
        return ac
    return 10


def combat_profile(m: dict) -> dict:
    """Reduce a full SRD stat block to what combat needs — with REAL numbers."""
    atk_bonus, dmg_dice, atk_name = 3, "1d6", "Attack"
    for a in m.get("actions", []) or []:
        if a.get("attack_bonus") is not None and a.get("damage"):
            atk_bonus = a["attack_bonus"]
            dmg = a["damage"][0]
            dmg_dice = dmg.get("damage_dice", "1d6")
            atk_name = a["name"]
            break
    traits = [t["name"] for t in (m.get("special_abilities") or [])][:3]
    return {"type": "monster", "name": m["name"], "hp": m.get("hit_points", 10),
            "max_hp": m.get("hit_points", 10), "ac": _ac(m), "attack_bonus": atk_bonus,
            "damage_dice": dmg_dice, "attack_name": atk_name,
            "cr": m.get("challenge_rating"), "traits": traits}


def encounter_from_names(names: list[str], rng: random.Random) -> dict | None:
    """Pick one of these named SRD monsters (theme-curated) and return its combat profile."""
    found = [m for m in (get_monster(n) for n in names) if m]
    if not found:
        return None
    return combat_profile(rng.choice(found))


def random_encounter(max_cr: float, rng: random.Random) -> dict | None:
    pool = [m for m in _monsters()
            if isinstance(m.get("challenge_rating"), (int, float)) and 0 < m["challenge_rating"] <= max_cr]
    return combat_profile(rng.choice(pool)) if pool else None


def statblock(name: str) -> str:
    """Readable stat block for the DM lookup tool."""
    m = get_monster(name)
    if not m:
        sugg = search_monsters(name)
        return f"No SRD monster '{name}'." + (f" Did you mean: {', '.join(sugg)}?" if sugg else "")
    cp = combat_profile(m)
    speed = ", ".join(f"{k} {v}" for k, v in (m.get("speed") or {}).items())
    abilities = "  ".join(f"{a.upper()} {m.get(a, 10)}"
                          for a in ["strength", "dexterity", "constitution",
                                    "intelligence", "wisdom", "charisma"])
    lines = [f"**{m['name']}** — {m.get('size','')} {m.get('type','')}, CR {m.get('challenge_rating')}",
             f"AC {cp['ac']}  HP {m.get('hit_points')} ({m.get('hit_dice','')})  Speed: {speed}",
             abilities]
    if cp["traits"]:
        lines.append("Traits: " + ", ".join(cp["traits"]))
    actions = [f"{a['name']}: {a.get('desc','')[:120]}" for a in (m.get("actions") or [])[:3]]
    if actions:
        lines.append("Actions:\n  " + "\n  ".join(actions))
    return "\n".join(lines)


def condition(name: str) -> str:
    c = next((x for x in _conditions() if x["name"].lower() == (name or "").lower().strip()), None)
    if not c:
        names = [x["name"] for x in _conditions()]
        return f"No condition '{name}'. Known: {', '.join(names)}"
    desc = " ".join(c.get("desc", [])) if isinstance(c.get("desc"), list) else c.get("desc", "")
    return f"**{c['name']}**: {desc}"


def loaded() -> dict:
    return {"monsters": len(_monsters()), "conditions": len(_conditions())}
