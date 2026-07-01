"""Persistent campaign state for DNDMCP — the world that remembers.

SQLite, zero-ops, one file. Holds the campaign, the character, the rooms/world graph,
the current position, and the session log. Survives across the whole session (and across
reconnects, since the file persists). Reskin of the kit's registry pattern.

Reads/writes are validated at the boundary via the Pydantic models in models.py — a wrong
key or missing field fails loudly here, not three calls later in a tool handler.
"""

from __future__ import annotations

import contextvars
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .models import Campaign, Character, Entity, LogEntry, Quest, Room

# Set by the transport layer (server.py's ASGI middleware, web.py's request handlers) for the
# duration of one inbound request, read by World.log() as its default for ip/session_id — a
# single choke point so the ~15 existing world.log(...) call sites in server.py need zero
# changes to start carrying request provenance. Metrics-only (see EVENT_STREAM_SPEC.md); never
# read for gameplay logic.
_request_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar("_request_ip", default=None)
_request_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_request_session_id", default=None)


@contextmanager
def request_context(ip: str | None, session_id: str | None = None):
    """Wrap one inbound request so every world.log() call made while handling it — however
    deep in server.py's tool-handler call stack — is tagged with where it came from."""
    ip_token = _request_ip.set(ip)
    session_token = _request_session_id.set(session_id)
    try:
        yield
    finally:
        _request_ip.reset(ip_token)
        _request_session_id.reset(session_token)


def _state_dir() -> Path:
    path = Path(os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp")))
    path.mkdir(parents=True, exist_ok=True)
    return path


# Bump on ANY schema change (new/renamed/removed column, new table). One number, one source
# of truth: SQLite's own `PRAGMA user_version`, no separate tracking table to drift out of
# sync. A live campaign now runs on a persistent pod volume — migrations from here on MUST
# be additive (CREATE TABLE IF NOT EXISTS / ALTER TABLE ADD COLUMN), never a blanket drop.
# If a genuinely breaking change is ever needed, write an explicit versioned migration step
# instead of wiping; do not restore the old "wipe on any mismatch" behavior.
SCHEMA_VERSION = 9

MAIN_CAMPAIGN_ID = "main"


class World:
    """One save file = one shared campaign world. Multiple players (characters) explore it
    concurrently; each character has its own location. player_id is caller-supplied (minted
    by start_adventure, threaded through every other tool call)."""

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else _state_dir() / "campaign.db"
        self._local = threading.local()
        self._init()

    @property
    def _c(self) -> sqlite3.Connection:
        # One connection PER THREAD, created lazily. A single shared connection breaks the
        # moment World is used off its creating thread: the pod runs MCP on the main thread
        # and the GUI on a daemon thread (app.py), and the browser-DM chat path (web.py ->
        # dm_loop -> server.py tool functions) drives THIS object from the GUI thread —
        # sqlite3's default check_same_thread=True makes that an instant ProgrammingError.
        # Thread-local connections + WAL make cross-thread use safe without a global lock:
        # WAL lets a reader and a writer proceed concurrently instead of blocking each other
        # (the GUI polls every 1.5s while the game writes constantly), and the 5s busy
        # timeout absorbs the rare write-write collision instead of surfacing "database is
        # locked" mid-turn. WAL is a persistent property of the DB file; setting it on every
        # connection is an idempotent no-op after the first.
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        # Additive only — every statement below is idempotent (IF NOT EXISTS), so re-running
        # this on an existing live DB just fills in whatever's new without touching existing
        # rows. PRAGMA user_version is bumped at the end purely for bookkeeping/visibility.
        self._c.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaign (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                theme TEXT, premise TEXT, created_at REAL, start_room TEXT, turn INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS character (
                player_id TEXT PRIMARY KEY,
                name TEXT, klass TEXT, level INTEGER DEFAULT 1,
                hp INTEGER, max_hp INTEGER, ac INTEGER DEFAULT 12,
                stats TEXT, inventory TEXT, location_id TEXT
            );
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY, name TEXT, description TEXT,
                contents TEXT, visited INTEGER DEFAULT 0, image_ref TEXT,
                features TEXT DEFAULT '[]', kind TEXT DEFAULT ''
            );
            -- Generic graph relationships (same pattern as the Context DB's own `edges`
            -- table): any node type can relate to any other, distinguished by edge_type.
            -- Room exits are just one edge_type ("north", "down", ...) among possibly many
            -- future ones (entity -is_in-> location, item -owned_by-> character, etc. —
            -- see WORLD_SCHEMA.md). No embedded JSON blobs on the node row for this.
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_type TEXT NOT NULL, from_id TEXT NOT NULL,
                to_type TEXT NOT NULL, to_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                metadata TEXT,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_type, from_id);
            CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_type, to_id);
            CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
            -- subject_type/subject_id: a generic (aggregate_type, aggregate_id) pair, same
            -- shape as edges.from_type/from_id — what this event is ABOUT, distinct from
            -- player_id (who caused it). "room"+room_id is what makes stigmergy possible (a
            -- LATER player entering a room can query "what happened here"); "item"+item_id /
            -- "entity"+entity_id are the same query for a specific object/monster once those
            -- have stable ids (see game.py/compendium.py/worldgen.py content-dict "id" field).
            -- One pair, not a column per aggregate type — a fixed enum of columns here would
            -- be exactly the rigid-schema problem WORLD_SCHEMA.md's loose-envelope principle
            -- argues against; new subject types need zero schema change to start using.
            CREATE TABLE IF NOT EXISTS log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, text TEXT,
                player_id TEXT, subject_type TEXT, subject_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_log_subject ON log(subject_type, subject_id);
            -- First-class NPC identity — persona/goal/disposition/memory for one spawned
            -- instance, joined by id to the mechanical monster dict still living in
            -- room.contents (see models.Entity docstring for why the split).
            CREATE TABLE IF NOT EXISTS entity (
                id TEXT PRIMARY KEY, kind TEXT DEFAULT '', name TEXT,
                location_id TEXT, disposition TEXT DEFAULT 'neutral',
                alive INTEGER DEFAULT 1, persona TEXT DEFAULT '', goal TEXT DEFAULT '',
                memory TEXT DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_entity_location ON entity(location_id);
            -- Multi-world: "main" is the well-known default world (what the old singleton
            -- `campaign` row becomes below); any other id is a world someone created and can
            -- share the id/link for others to join. `salt` seeds room generation so two
            -- worlds of the same theme don't generate identical rooms (game.py._seeded).
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY, name TEXT DEFAULT '', theme TEXT, premise TEXT,
                created_at REAL, start_room TEXT, turn INTEGER DEFAULT 0, salt TEXT DEFAULT ''
            );
            -- Trackable objective: an NPC's job, a party goal, a plot thread (WORLD_SCHEMA.md's
            -- "BUILD NOW: quest minimal"). Shared world state, same as rooms/entities — any
            -- player in campaign_id can see and progress one another player started.
            -- given_by/created_by are entity_id/player_id references, stored loosely like
            -- Entity.location_id — no FK enforcement, a stale id just means narration has
            -- nothing to look up. Broader relatedness (which OTHER entities/locations this
            -- quest touches) lives on the generic `edges` table as quest--involves-->X, not
            -- here — a quest can involve many nodes, doesn't fit a scalar column.
            CREATE TABLE IF NOT EXISTS quest (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL DEFAULT 'main',
                title TEXT, description TEXT DEFAULT '', state TEXT DEFAULT 'active',
                steps TEXT DEFAULT '[]', given_by TEXT, created_by TEXT, created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_quest_campaign ON quest(campaign_id);
            """
        )
        # rooms/character/log/entity predate multi-world and had no campaign_id column —
        # backfill everything that already exists to "main" (the world they were always
        # implicitly part of) via ALTER's DEFAULT, which SQLite applies to existing rows too.
        for table in ("rooms", "character", "log", "entity"):
            self._add_column_if_missing(table, "campaign_id", f"TEXT DEFAULT '{MAIN_CAMPAIGN_ID}'")
        # Request provenance for metrics (EVENT_STREAM_SPEC.md) — nullable, populated only for
        # events logged from here on; old rows just read back as NULL, same backfill-free
        # pattern as every other additive column here.
        self._add_column_if_missing("log", "ip", "TEXT")
        self._add_column_if_missing("log", "session_id", "TEXT")
        # Themed replacement for the raw SRD attack name (e.g. "static-charged prod" instead
        # of "Scimitar") — the SRD is fantasy-only, so a sci-fi/steampunk/etc world's monster
        # keeps rules-accurate mechanics but shouldn't narrate a medieval weapon mid-combat.
        self._add_column_if_missing("entity", "attack_flavor", "TEXT DEFAULT ''")
        # Migrate the old singleton `campaign` row (id=1) into campaigns/"main", once — INSERT
        # OR IGNORE makes re-running this on every startup a no-op after the first time.
        self._c.execute(
            "INSERT OR IGNORE INTO campaigns (id, name, theme, premise, created_at, start_room, turn, salt)"
            " SELECT ?, '', theme, premise, created_at, start_room, turn, ?"
            " FROM campaign WHERE id=1",
            (MAIN_CAMPAIGN_ID, secrets.token_hex(4)),
        )
        self._c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._c.commit()

    def _add_column_if_missing(self, table: str, column: str, coldef: str) -> None:
        """SQLite has no `ADD COLUMN IF NOT EXISTS` — guard manually so this stays safe to
        run on every startup against a live DB that may already have the column."""
        cols = {r["name"] for r in self._c.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self._c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")

    # --- edges (generic graph relationships) ------------------------------------
    def set_edges(self, from_type: str, from_id: str, to_type: str,
                 edges: dict[str, str], *, metadata: dict[str, str] | None = None) -> None:
        """Replace ALL edges of `to_type` from (from_type, from_id) with the given
        {edge_type: to_id} mapping — e.g. a room's full exits dict. Delete-then-insert
        because callers always pass the complete current set (same pattern upsert_room
        already used for the old JSON column)."""
        self._c.execute("DELETE FROM edges WHERE from_type=? AND from_id=? AND to_type=?",
                        (from_type, from_id, to_type))
        meta = metadata or {}
        for edge_type, to_id in edges.items():
            self._c.execute(
                "INSERT INTO edges (from_type,from_id,to_type,to_id,edge_type,metadata,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (from_type, from_id, to_type, to_id, edge_type,
                 json.dumps(meta.get(edge_type)) if meta.get(edge_type) else None, time.time()),
            )
        self._c.commit()

    def edges_from(self, from_type: str, from_id: str, *, edge_type: str | None = None) -> list[dict]:
        q = "SELECT to_type,to_id,edge_type,metadata FROM edges WHERE from_type=? AND from_id=?"
        params = [from_type, from_id]
        if edge_type:
            q += " AND edge_type=?"; params.append(edge_type)
        return [dict(r) for r in self._c.execute(q, params).fetchall()]

    def edges_to(self, to_type: str, to_id: str) -> list[dict]:
        """Reverse lookup — 'what points at this node' — the thing a JSON blob on the node
        row could never answer without scanning every other row."""
        rows = self._c.execute(
            "SELECT from_type,from_id,edge_type,metadata FROM edges WHERE to_type=? AND to_id=?",
            (to_type, to_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def room_exits(self, room_id: str) -> dict[str, str]:
        return {e["edge_type"]: e["to_id"] for e in self.edges_from("room", room_id, )
                if e["to_type"] == "room"}

    def room_exit_descriptions(self, room_id: str) -> dict[str, str]:
        """Per-exit threshold descriptor (door/archway/stairwell text), stored in the same
        edges as the exit link itself — the `metadata` column set_edges already supports,
        previously unused. Safe to reveal even for undiscovered destinations (see discover()
        below) since it describes THIS room's doorway, not what's beyond it."""
        out = {}
        for e in self.edges_from("room", room_id, ):
            if e["to_type"] == "room" and e["metadata"]:
                out[e["edge_type"]] = json.loads(e["metadata"])
        return out

    # --- per-player discovery (fog of war) --------------------------------------
    # `world.mark_visited`/`Room.visited` is a single GLOBAL flag — in a shared multiplayer
    # world that means a room visited by player A shows as "known"/named to player B who has
    # never been there, the instant _prefetch_frontier world-builds it in the background
    # (which happens well before any player actually looks through that doorway). This is a
    # separate, per-(player, room) fact: has THIS character actually arrived here.
    def discover(self, player_id: str, room_id: str) -> None:
        if self.has_discovered(player_id, room_id):
            return
        self._c.execute(
            "INSERT INTO edges (from_type,from_id,to_type,to_id,edge_type,metadata,created_at)"
            " VALUES ('character',?,'room',?,'discovered',NULL,?)",
            (player_id, room_id, time.time()),
        )
        self._c.commit()

    def has_discovered(self, player_id: str, room_id: str) -> bool:
        return self._c.execute(
            "SELECT 1 FROM edges WHERE from_type='character' AND from_id=? AND to_type='room'"
            " AND to_id=? AND edge_type='discovered'",
            (player_id, room_id),
        ).fetchone() is not None

    # --- campaign (one world; "main" is the default, others are created/joined by id) ------
    def create_campaign(self, campaign_id: str, *, theme: str, premise: str, start_room: str,
                        name: str = "") -> Campaign:
        """Create a NEW world with this id. Caller picks the id — MAIN_CAMPAIGN_ID for the
        well-known default, or a fresh random one (see server.py's create_world) for a
        shareable new world. `salt` is generated once here and never changes — it's what
        makes this world's room generation distinct from every other world of the same theme."""
        camp = Campaign(id=campaign_id, name=name, theme=theme, premise=premise,
                        created_at=time.time(), start_room=start_room, turn=0,
                        salt=secrets.token_hex(4))
        self._c.execute(
            "INSERT INTO campaigns (id, name, theme, premise, created_at, start_room, turn, salt)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (camp.id, camp.name, camp.theme, camp.premise, camp.created_at, camp.start_room,
             camp.turn, camp.salt),
        )
        self._c.commit()
        return camp

    def campaign(self, campaign_id: str = MAIN_CAMPAIGN_ID) -> Campaign | None:
        r = self._c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        return Campaign.model_validate(dict(r)) if r else None

    def campaign_exists(self, campaign_id: str) -> bool:
        return self._c.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone() is not None

    def delete_campaign(self, campaign_id: str) -> None:
        """Wipe one world's rooms/characters/entities/log/edges + the campaign row itself.
        Caller (server.py's delete_world) owns the "main"-guard and sole-player-check safety
        rules — this method trusts it's already safe to call."""
        player_ids = [r["player_id"] for r in self._c.execute(
            "SELECT player_id FROM character WHERE campaign_id=?", (campaign_id,)).fetchall()]
        if player_ids:
            placeholders = ",".join("?" * len(player_ids))
            self._c.execute(f"DELETE FROM edges WHERE from_id IN ({placeholders})", player_ids)
        # quest ids are bare uuid.uuid4().hex[:8] (like item/entity ids), NOT campaign-
        # prefixed the way room ids are — neither the player_ids clause above nor the
        # to_id LIKE clause below would ever catch a quest--involves-->X edge, so it needs
        # its own explicit cleanup, both directions.
        quest_ids = [r["id"] for r in self._c.execute(
            "SELECT id FROM quest WHERE campaign_id=?", (campaign_id,)).fetchall()]
        if quest_ids:
            placeholders = ",".join("?" * len(quest_ids))
            self._c.execute(f"DELETE FROM edges WHERE from_id IN ({placeholders})", quest_ids)
            self._c.execute(f"DELETE FROM edges WHERE to_id IN ({placeholders})", quest_ids)
        # room ids are namespaced "<campaign_id>:..." (game.py's room-id scheme), so this
        # catches every "discovered" edge pointing into this world without a campaign_id
        # column on edges itself.
        self._c.execute("DELETE FROM edges WHERE to_id LIKE ?", (f"{campaign_id}:%",))
        for table in ("rooms", "character", "log", "entity", "quest"):
            self._c.execute(f"DELETE FROM {table} WHERE campaign_id=?", (campaign_id,))
        self._c.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
        self._c.commit()

    # --- character (one row per player_id) -------------------------------------
    def new_character(self, player_id: str, campaign_id: str, *, name: str, klass: str, hp: int,
                      ac: int, stats: dict[str, int], inventory: list[dict],
                      location_id: str) -> Character:
        ch = Character(player_id=player_id, campaign_id=campaign_id, name=name, klass=klass,
                       hp=hp, max_hp=hp, ac=ac, stats=stats, inventory=inventory,
                       location_id=location_id)
        self._c.execute(
            "INSERT OR REPLACE INTO character"
            " (player_id,campaign_id,name,klass,level,hp,max_hp,ac,stats,inventory,location_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ch.player_id, ch.campaign_id, ch.name, ch.klass, ch.level, ch.hp, ch.max_hp, ch.ac,
             json.dumps(ch.stats), json.dumps(ch.inventory), ch.location_id),
        )
        self._c.commit()
        return ch

    def character(self, player_id: str) -> Character | None:
        r = self._c.execute("SELECT * FROM character WHERE player_id=?", (player_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["campaign_id"] = d.get("campaign_id") or MAIN_CAMPAIGN_ID
        d["stats"] = json.loads(d["stats"] or "{}")
        d["inventory"] = json.loads(d["inventory"] or "[]")
        return Character.model_validate(d)

    def set_location(self, player_id: str, room_id: str) -> None:
        self._c.execute("UPDATE character SET location_id=? WHERE player_id=?", (room_id, player_id))
        self._c.execute(
            "UPDATE campaigns SET turn=turn+1"
            " WHERE id=(SELECT campaign_id FROM character WHERE player_id=?)",
            (player_id,),
        )
        self._c.commit()

    def damage(self, player_id: str, amount: int) -> int:
        cur = self.character(player_id)
        new_hp = max(0, (cur.hp if cur else 0) - amount)
        self._c.execute("UPDATE character SET hp=? WHERE player_id=?", (new_hp, player_id))
        self._c.commit()
        return new_hp

    def add_item(self, player_id: str, item: dict) -> None:
        """`item`: {"name": ..., "description": ...} — see pick_up_item/generate_item_content."""
        cur = self.character(player_id)
        inv = (cur.inventory if cur else []) + [item]
        self._c.execute("UPDATE character SET inventory=? WHERE player_id=?", (json.dumps(inv), player_id))
        self._c.commit()

    def remove_item(self, player_id: str, item_id: str) -> dict | None:
        """Remove one item from inventory by id (see drop_item). Falls back to matching by
        name for items with no "id" — the starting kit predates stable ids on inventory
        items; live characters created before that fix still carry id-less items. Returns
        the removed item dict, or None if no match was found."""
        cur = self.character(player_id)
        if not cur:
            return None
        key = lambda i: i.get("id") or i.get("name")
        removed = next((i for i in cur.inventory if key(i) == item_id), None)
        if not removed:
            return None
        inv = [i for i in cur.inventory if key(i) != item_id]
        self._c.execute("UPDATE character SET inventory=? WHERE player_id=?", (json.dumps(inv), player_id))
        self._c.commit()
        return removed

    def players(self, campaign_id: str = MAIN_CAMPAIGN_ID) -> list[Character]:
        """All characters currently in ONE world (for the GUI's 'other players' view) — must
        be scoped, not global, now that more than one world can exist."""
        rows = self._c.execute("SELECT * FROM character WHERE campaign_id=?", (campaign_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["campaign_id"] = d.get("campaign_id") or MAIN_CAMPAIGN_ID
            d["stats"] = json.loads(d["stats"] or "{}")
            d["inventory"] = json.loads(d["inventory"] or "[]")
            out.append(Character.model_validate(d))
        return out

    # --- rooms ----------------------------------------------------------------
    def upsert_room(self, *, room_id: str, campaign_id: str = MAIN_CAMPAIGN_ID, name: str,
                    description: str, exits: dict[str, str], contents: list[dict],
                    image_ref: str | None = None, features: list[str] | None = None,
                    kind: str = "", exit_descriptions: dict[str, str] | None = None) -> Room:
        room = Room(id=room_id, name=name, description=description, exits=exits,
                   contents=contents, image_ref=image_ref, features=features or [], kind=kind)
        self._c.execute(
            "INSERT INTO rooms (id,campaign_id,name,description,contents,visited,image_ref,features,kind)"
            " VALUES (?,?,?,?,?,0,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description,"
            " contents=excluded.contents,"
            " image_ref=COALESCE(excluded.image_ref, rooms.image_ref),"
            " features=excluded.features,"
            " kind=CASE WHEN excluded.kind != '' THEN excluded.kind ELSE rooms.kind END",
            (room.id, campaign_id, room.name, room.description, json.dumps(room.contents),
             room.image_ref, json.dumps(room.features), room.kind),
        )
        self._c.commit()
        # exits are edges now, not a JSON column — sync separately. Delete-then-insert: the
        # caller always passes the room's complete current exit set (same semantics the old
        # JSON column had). exit_descriptions rides along as edge metadata (see
        # room_exit_descriptions) — physical threshold text, safe to show pre-discovery.
        #
        # exit_descriptions=None vs {} are NOT the same thing, and callers rely on that.
        # Most call sites (attack/pick_up_item/drop_item/talk_to in server.py) re-upsert a
        # room to persist an unrelated change (HP, inventory, a new NPC persona) and pass
        # `exits=room.exits` unchanged — they never think about descriptors at all, so they
        # leave exit_descriptions at its None default. Because set_edges is a full
        # delete-then-reinsert of this room's exit edges, treating "None" as "no metadata"
        # would silently null out every descriptor (e.g. "a warped iron door") on every one
        # of those calls, forever — there's no other path that ever restores them. So: None
        # means "this caller didn't touch descriptors, preserve whatever's already there" —
        # fetch the existing set BEFORE clobbering the edges. A caller that DOES pass a dict
        # (including an explicitly empty {}) is asserting "this is the complete, authoritative
        # descriptor set now" and wins outright, same as it always has.
        if exit_descriptions is None:
            exit_descriptions = self.room_exit_descriptions(room_id)
        self.set_edges("room", room_id, "room", room.exits, metadata=exit_descriptions)
        saved = self.room(room_id)  # re-read: picks up visited/COALESCE'd image_ref from the DB
        assert saved is not None, f"room {room_id} vanished immediately after its own upsert"
        return saved

    def room(self, room_id: str) -> Room | None:
        r = self._c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d.pop("campaign_id", None)  # scoping column, not part of Room's own shape (extra="forbid")
        d["exits"] = self.room_exits(room_id)
        d["contents"] = json.loads(d["contents"] or "[]")
        d["features"] = json.loads(d["features"] or "[]")
        d["visited"] = bool(d["visited"])
        d["kind"] = d.get("kind") or ""
        return Room.model_validate(d)

    def mark_visited(self, room_id: str) -> None:
        self._c.execute("UPDATE rooms SET visited=1 WHERE id=?", (room_id,))
        self._c.commit()

    def room_ids_in(self, campaign_id: str) -> list[tuple[str, str, str]]:
        """(id, name, kind) for every room in one world — a cheap listing for dev tooling
        (see server.py dev_list_rooms) without loading each room's full contents/exits."""
        rows = self._c.execute(
            "SELECT id, name, kind FROM rooms WHERE campaign_id=?", (campaign_id,)
        ).fetchall()
        return [(r["id"], r["name"], r["kind"] or "") for r in rows]

    def set_room_image(self, room_id: str, image_ref: str) -> None:
        self._c.execute("UPDATE rooms SET image_ref=? WHERE id=?", (image_ref, room_id))
        self._c.commit()

    # --- entity (NPC identity — see models.Entity) -----------------------------
    def upsert_entity(self, *, entity_id: str, kind: str, name: str, location_id: str | None,
                      campaign_id: str = MAIN_CAMPAIGN_ID, disposition: str = "neutral",
                      persona: str = "", goal: str = "", alive: bool = True,
                      attack_flavor: str = "") -> Entity:
        """Create or fully replace an entity's identity fields. Does NOT touch `memory` —
        use append_entity_memory for that, so re-generating a persona never loses history."""
        existing = self.entity(entity_id)
        memory = existing.memory if existing else []
        ent = Entity(id=entity_id, kind=kind, name=name, location_id=location_id,
                    disposition=disposition, alive=alive, persona=persona, goal=goal,
                    memory=memory, attack_flavor=attack_flavor)
        self._c.execute(
            "INSERT INTO entity (id,campaign_id,kind,name,location_id,disposition,alive,persona,goal,memory,attack_flavor)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name,"
            " location_id=excluded.location_id, disposition=excluded.disposition,"
            " alive=excluded.alive, persona=excluded.persona, goal=excluded.goal,"
            " attack_flavor=excluded.attack_flavor",
            (ent.id, campaign_id, ent.kind, ent.name, ent.location_id, ent.disposition,
             int(ent.alive), ent.persona, ent.goal, json.dumps(ent.memory), ent.attack_flavor),
        )
        self._c.commit()
        return ent

    def entity_names_in(self, campaign_id: str) -> list[str]:
        """Every name already used by a spawned NPC identity in this world — passed to
        persona generation so it invents someone new instead of echoing an existing NPC (or,
        worse, the prompt's own few-shot example name) into an unrelated creature."""
        rows = self._c.execute(
            "SELECT DISTINCT name FROM entity WHERE campaign_id=? AND name != ''", (campaign_id,)
        ).fetchall()
        return [r["name"] for r in rows]

    def entity(self, entity_id: str) -> Entity | None:
        r = self._c.execute("SELECT * FROM entity WHERE id=?", (entity_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d.pop("campaign_id", None)  # scoping column, not part of Entity's own shape (extra="forbid")
        d["alive"] = bool(d["alive"])
        d["memory"] = json.loads(d["memory"] or "[]")
        return Entity.model_validate(d)

    def alive_entities_in(self, location_id: str) -> list[Entity]:
        """Full identity rows for every living entity in ONE room — the per-room-granular
        sibling of count_alive_entities_in (which only counts, for the density gate). Used
        by sense_surroundings to report WHAT is nearby, not just whether something is."""
        out = []
        for r in self._c.execute(
            "SELECT * FROM entity WHERE alive=1 AND location_id=?", (location_id,)
        ).fetchall():
            d = dict(r)
            d.pop("campaign_id", None)
            d["alive"] = bool(d["alive"])
            d["memory"] = json.loads(d["memory"] or "[]")
            out.append(Entity.model_validate(d))
        return out

    def append_entity_memory(self, entity_id: str, role: str, content: str) -> None:
        """Append one turn ({"role": "player"|"npc", ...}) to an entity's stored conversation.
        Shared across whoever talks to this NPC next — the whole point of moving memory off
        the room.contents dict and onto a first-class row."""
        ent = self.entity(entity_id)
        if not ent:
            return
        memory = ent.memory + [{"role": role, "content": content}]
        self._c.execute("UPDATE entity SET memory=? WHERE id=?", (json.dumps(memory), entity_id))
        self._c.commit()

    def kill_entity(self, entity_id: str) -> None:
        """Mark an entity dead. No-ops safely if no entity row exists (e.g. a monster the
        density gate never gave a persona to) — combat still works either way, this just
        keeps the identity table in sync with room.contents when one does exist."""
        self._c.execute("UPDATE entity SET alive=0 WHERE id=?", (entity_id,))
        self._c.commit()

    def count_alive_entities_in(self, location_ids: list[str]) -> int:
        """How many living named NPCs already exist in this set of rooms — the deterministic
        density check that decides whether a freshly-spawned monster is worth giving a full
        persona to (see server.py::_maybe_spawn_entity_persona)."""
        if not location_ids:
            return 0
        placeholders = ",".join("?" * len(location_ids))
        row = self._c.execute(
            f"SELECT COUNT(*) AS n FROM entity WHERE alive=1 AND location_id IN ({placeholders})",
            location_ids,
        ).fetchone()
        return row["n"] if row else 0

    # --- quest (see models.Quest) ------------------------------------------------
    def start_quest(self, quest_id: str, campaign_id: str = MAIN_CAMPAIGN_ID, *, title: str,
                    description: str = "", steps: list[dict] | None = None,
                    given_by: str | None = None, created_by: str | None = None) -> Quest:
        q = Quest(id=quest_id, campaign_id=campaign_id, title=title, description=description,
                  state="active", steps=steps or [], given_by=given_by, created_by=created_by,
                  created_at=time.time())
        self._c.execute(
            "INSERT INTO quest (id,campaign_id,title,description,state,steps,given_by,created_by,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (q.id, q.campaign_id, q.title, q.description, q.state, json.dumps(q.steps),
             q.given_by, q.created_by, q.created_at),
        )
        self._c.commit()
        return q

    def quest(self, quest_id: str) -> Quest | None:
        r = self._c.execute("SELECT * FROM quest WHERE id=?", (quest_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["steps"] = json.loads(d["steps"] or "[]")
        return Quest.model_validate(d)

    def active_quests(self, campaign_id: str = MAIN_CAMPAIGN_ID) -> list[Quest]:
        rows = self._c.execute(
            "SELECT * FROM quest WHERE campaign_id=? AND state='active' ORDER BY created_at",
            (campaign_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["steps"] = json.loads(d["steps"] or "[]")
            out.append(Quest.model_validate(d))
        return out

    def update_quest_state(self, quest_id: str, state: str) -> Quest | None:
        self._c.execute("UPDATE quest SET state=? WHERE id=?", (state, quest_id))
        self._c.commit()
        return self.quest(quest_id)

    def complete_quest_step(self, quest_id: str, step_index: int) -> Quest | None:
        q = self.quest(quest_id)
        if not q or not (0 <= step_index < len(q.steps)):
            return None
        steps = [dict(s) for s in q.steps]
        steps[step_index] = {**steps[step_index], "done": True}
        self._c.execute("UPDATE quest SET steps=? WHERE id=?", (json.dumps(steps), quest_id))
        self._c.commit()
        return self.quest(quest_id)

    def add_quest_step(self, quest_id: str, text: str) -> Quest | None:
        q = self.quest(quest_id)
        if not q:
            return None
        steps = q.steps + [{"text": text, "done": False}]
        self._c.execute("UPDATE quest SET steps=? WHERE id=?", (json.dumps(steps), quest_id))
        self._c.commit()
        return self.quest(quest_id)

    def add_quest_involvement(self, quest_id: str, node_type: str, node_id: str) -> None:
        """quest --involves--> entity/location (WORLD_SCHEMA.md). Not set_edges — that
        replaces the FULL {edge_type: to_id} set for one (from_type,from_id,to_type),
        assuming one to_id per edge_type (fine for room exits, one per direction); a quest
        can involve MANY ids under the same edge_type 'involves'. Called both at quest
        creation (given_by) and later via update_quest's involve_entity/involve_location —
        the graph a quest references is often generated lazily, after the quest itself
        already exists as text (see server.py's DM_PERSONA nudge)."""
        self._c.execute(
            "INSERT INTO edges (from_type,from_id,to_type,to_id,edge_type,metadata,created_at)"
            " VALUES ('quest',?,?,?,'involves',NULL,?)",
            (quest_id, node_type, node_id, time.time()),
        )
        self._c.commit()

    # --- log (domain events) ---------------------------------------------------
    def log(self, kind: str, text: str, *, player_id: str | None = None,
           campaign_id: str | None = None, subject_type: str | None = None,
           subject_id: str | None = None, ip: str | None = None,
           session_id: str | None = None) -> None:
        """Emit a domain event, scoped to a world. `campaign_id` is optional when `player_id`
        is given — resolved from that character's own campaign, so the ~15 existing call
        sites keyed by player_id needed zero changes when multi-world landed. Callers with NO
        player_id (system events like "room.generated" during background prefetch) MUST pass
        campaign_id explicitly — there's no character to resolve it from.

        `kind` should be dotted-namespace: "player.moved", "room.generated",
        "combat.resolved", "memory.noted", "adventure.started" — so the stream is filterable
        by category as well as by player. `subject_type`/`subject_id` (e.g. "room"/room_id,
        "item"/item_id, "entity"/entity_id) should be set for anything a later visitor might
        reasonably notice — it's what recent_log(subject_type=..., subject_id=...) surfaces
        as stigmergic traces. Both or neither — a subject_id without its type is ambiguous.

        `ip`/`session_id` default to whatever request_context() currently has set (the
        transport-layer middleware) — existing call sites don't need to pass these."""
        assert (subject_type is None) == (subject_id is None), \
            "subject_type and subject_id must be set together"
        if campaign_id is None:
            ch = self.character(player_id) if player_id else None
            campaign_id = ch.campaign_id if ch else MAIN_CAMPAIGN_ID
        if ip is None:
            ip = _request_ip.get()
        if session_id is None:
            session_id = _request_session_id.get()
        self._c.execute(
            "INSERT INTO log (ts,kind,text,player_id,campaign_id,subject_type,subject_id,ip,session_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), kind, text, player_id, campaign_id, subject_type, subject_id, ip, session_id))
        self._c.commit()

    def recent_log(self, n: int = 10, *, player_id: str | None = None,
                   campaign_id: str | None = None, kind_prefix: str | None = None,
                   subject_type: str | None = None, subject_id: str | None = None,
                   exclude_player_id: str | None = None) -> list[LogEntry]:
        """Recent events, optionally filtered to one world (campaign_id — required in
        practice once more than one world exists, else you'd see every world's traces mixed
        together), one player (their own events + world-level ones with no actor), one event
        category (e.g. kind_prefix="combat"), one subject (subject_type+subject_id — the
        stigmergic-trace query: "what happened to/in this room/item/entity before I
        arrived"), and/or excluding one player's own events (so a trace query doesn't narrate
        the viewer's own last action back at them)."""
        where, params = [], []
        if campaign_id is not None:
            where.append("campaign_id = ?")
            params.append(campaign_id)
        if player_id is not None:
            where.append("(player_id = ? OR player_id IS NULL)")
            params.append(player_id)
        if kind_prefix is not None:
            where.append("kind LIKE ?")
            params.append(f"{kind_prefix}%")
        if subject_type is not None:
            where.append("subject_type = ? AND subject_id = ?")
            params.extend([subject_type, subject_id])
        if exclude_player_id is not None:
            where.append("(player_id IS NULL OR player_id != ?)")
            params.append(exclude_player_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._c.execute(
            f"SELECT ts,kind,text,player_id,campaign_id,subject_type,subject_id FROM log {clause}"
            f" ORDER BY seq DESC LIMIT ?",
            (*params, n),
        ).fetchall()
        return [LogEntry.model_validate(dict(r)) for r in reversed(rows)]

    def snapshot(self, player_id: str) -> dict:
        """Full inspectable state for one player — the 'world remembers' proof. Dict-shaped at
        the boundary (this is what get_state hands back over MCP) but built from validated models."""
        ch = self.character(player_id)
        camp = self.campaign(ch.campaign_id if ch else MAIN_CAMPAIGN_ID)
        room = self.room(ch.location_id) if ch else None
        return {"campaign": camp.model_dump() if camp else None,
                "character": ch.model_dump() if ch else None,
                "current_room": room.model_dump() if room else None,
                "quests": [q.model_dump() for q in
                          self.active_quests(ch.campaign_id if ch else MAIN_CAMPAIGN_ID)],
                "log": [entry.model_dump() for entry in
                        self.recent_log(8, player_id=player_id,
                                        campaign_id=ch.campaign_id if ch else None)]}
