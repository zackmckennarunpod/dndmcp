"""Typed shapes for the persisted world — the type-safety layer over SQLite.

Pydantic validates at the DB boundary (row -> model on read, model -> row on write) so a
wrong key or missing field fails loudly at the point of storage, not three calls later in
a tool handler. Room `contents` (monsters/loot) stays as loose dicts on purpose — that
payload comes from compendium.py/worldgen.py and is mutated in place during combat;
tightening it isn't worth the risk this late.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Campaign(BaseModel):
    """One world. `id` is the shareable join code — "main" is the well-known default world
    everyone lands in without specifying one; anything else is a world someone created and
    can share the id/link for others to join. `salt` seeds room generation (game.py._seeded)
    so two different worlds of the same theme don't generate identical rooms — see the
    room-repeat bug this fixes."""
    model_config = ConfigDict(extra="forbid")

    id: str = "main"
    name: str = ""
    theme: str
    premise: str
    created_at: float
    start_room: str
    turn: int = 0
    salt: str = ""


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: str
    campaign_id: str = "main"
    name: str
    klass: str
    level: int = 1
    hp: int
    max_hp: int
    ac: int = 12
    stats: dict[str, int]
    inventory: list[dict] = []  # {"id": ..., "name": ..., "description": ...} — id is stable
    # across the item's move from room.contents to here (same id, new owner) — see
    # pick_up_item/generate_item_content and log.subject_type="item".
    location_id: str


class Room(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    exits: dict[str, str]  # direction -> room_id
    contents: list[dict]  # monster/loot payloads — see module docstring
    visited: bool = False
    image_ref: str | None = None
    features: list[str] = []
    kind: str = ""  # one/two-word room type (LLM-picked, e.g. "attic", "great hall") —
                    # informs exit-count feel and gives nearby-region context to later gens


class Entity(BaseModel):
    """A first-class NPC/monster identity — persona, goal, disposition, and conversation
    memory for one specific spawned instance. Mechanical combat stats (hp/ac/attack/damage)
    stay on the room.contents dict as before (attack()/damage() already mutate that in
    place); this table is the narrative half only, joined to that dict by `id`."""
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str = ""  # SRD species/type, e.g. "Goblin" — the compendium anchor
    name: str
    location_id: str | None = None
    disposition: str = "neutral"  # hostile | neutral | ally
    alive: bool = True
    persona: str = ""
    goal: str = ""
    memory: list[dict] = []  # [{"role": "player"|"npc", "content": ...}, ...]


class LogEntry(BaseModel):
    """A domain event. `kind` is dotted-namespace (e.g. "player.moved", "room.generated",
    "combat.resolved", "memory.noted") so the stream is filterable by category, not just by
    player. `player_id` is null for world-level events with no single actor."""
    model_config = ConfigDict(extra="forbid")

    kind: str
    text: str
    player_id: str | None = None
    campaign_id: str = "main"
    subject_type: str | None = None  # "room" | "item" | "entity" | ... — see state.py.log()
    subject_id: str | None = None
    ts: float = 0.0
