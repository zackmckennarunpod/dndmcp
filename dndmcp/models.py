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
    model_config = ConfigDict(extra="forbid")

    id: int = 1
    theme: str
    premise: str
    created_at: float
    start_room: str
    turn: int = 0


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: str
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


class LogEntry(BaseModel):
    """A domain event. `kind` is dotted-namespace (e.g. "player.moved", "room.generated",
    "combat.resolved", "memory.noted") so the stream is filterable by category, not just by
    player. `player_id` is null for world-level events with no single actor."""
    model_config = ConfigDict(extra="forbid")

    kind: str
    text: str
    player_id: str | None = None
    subject_type: str | None = None  # "room" | "item" | "entity" | ... — see state.py.log()
    subject_id: str | None = None
    ts: float = 0.0
