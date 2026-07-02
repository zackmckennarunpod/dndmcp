"""Typed shapes for the persisted world — the type-safety layer over SQLite.

Pydantic validates at the DB boundary (row -> model on read, model -> row on write) so a
wrong key or missing field fails loudly at the point of storage, not three calls later in
a tool handler. Room `contents` (monsters/loot) stays as loose dicts on purpose — that
payload comes from compendium.py/worldgen.py and is mutated in place during combat;
tightening it isn't worth the risk this late.
"""

from __future__ import annotations

from typing import Literal

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
    is_bot: bool = False
    story_cache: str | None = None
    story_cache_seq: int = 0
    story_cache_via: str | None = None


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
    attack_flavor: str = ""  # themed replacement for the raw SRD attack name in combat text


class Item(BaseModel):
    """A first-class, ownable object — the identity/ownership/effects layer for anything a
    player can pick up, drop, or give. Mirrors Entity's own split exactly: the lightweight
    loot dict still living in room.contents/character.inventory (see Room/Character
    docstrings above) is UNCHANGED — that's the mechanical/rendering layer every existing
    read site already uses; this table is the PARALLEL identity/ownership layer, joined by
    the same `id`. `owner_type`/`owner_id` is the same generic (aggregate_type, aggregate_id)
    shape LogEntry.subject_type/subject_id and the `edges` table already use elsewhere in
    this file — a room, a character, or an entity (NPC) can each hold an item, so a single
    fixed foreign key would be wrong."""
    model_config = ConfigDict(extra="forbid")

    id: str
    campaign_id: str = "main"
    name: str
    description: str = ""
    owner_type: Literal["room", "character", "entity"]
    owner_id: str
    portable: bool = True
    identified: bool = True
    properties: dict = {}
    effects: list[dict] = []  # [{"trigger": ..., "narration": ...}, ...] — see WORLD_SCHEMA.md
    created_at: float = 0.0
    acquired_at: float | None = None  # when it first left a room for someone's hands, if ever


class Quest(BaseModel):
    """A trackable objective — an NPC's job, a party goal, a plot thread. Shared world state
    like rooms/entities: any player in the campaign can see and progress one another player
    started. `given_by`/`created_by` are entity_id/player_id references, stored loosely (no
    FK enforcement, same as Entity.location_id) — a stale id just means narration has nothing
    to look up, not a crash. Broader relatedness (WORLD_SCHEMA.md's `involves[]`) lives on
    the generic `edges` table as quest--involves-->entity/location edges, not duplicated
    here — a quest can involve many nodes, which doesn't fit a single scalar field."""
    model_config = ConfigDict(extra="forbid")

    id: str
    campaign_id: str = "main"
    title: str
    description: str = ""
    state: Literal["active", "done", "failed"] = "active"
    steps: list[dict] = []  # [{"text": ..., "done": bool}, ...]
    given_by: str | None = None
    created_by: str | None = None
    created_at: float = 0.0


class WebSession(BaseModel):
    """Durable session_id -> player_id mapping for the browser-chat identity (e0b.4) --
    survives a redeploy even though chat_sessions._sessions (the in-memory DMSession store)
    does not, see state.py's web_session table. session_id is the same value carried by the
    dm_session HttpOnly cookie (chat_sessions.COOKIE_NAME); player_id is None until
    start_adventure mints one for this browser session. message_count doubles as the
    per-session lifetime-turn-cap counter (see chat_sessions.session_cap_exceeded) precisely
    because it lives here, not in the in-memory store."""
    model_config = ConfigDict(extra="forbid")

    session_id: str
    player_id: str | None = None
    campaign_id: str | None = None
    created_at: float = 0.0
    last_seen: float = 0.0
    message_count: int = 0


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
