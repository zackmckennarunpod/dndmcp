"""Persistent campaign state for DNDMCP — the world that remembers.

SQLite, zero-ops, one file. Holds the campaign, the character, the rooms/world graph,
the current position, and the session log. Survives across the whole session (and across
reconnects, since the file persists). Reskin of the kit's registry pattern.

Reads/writes are validated at the boundary via the Pydantic models in models.py — a wrong
key or missing field fails loudly here, not three calls later in a tool handler.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .models import Campaign, Character, LogEntry, Room


def _state_dir() -> Path:
    path = Path(os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp")))
    path.mkdir(parents=True, exist_ok=True)
    return path


# Bump on ANY schema change (new/renamed/removed column, new table). This is pre-launch dev
# state, not a real player's save — a version mismatch means "start clean," not "write a
# bespoke ALTER migration and hope every edge case is covered." One number, one source of
# truth: SQLite's own `PRAGMA user_version`, no separate tracking table to drift out of sync.
SCHEMA_VERSION = 5


class World:
    """One save file = one shared campaign world. Multiple players (characters) explore it
    concurrently; each character has its own location. player_id is caller-supplied (minted
    by start_adventure, threaded through every other tool call)."""

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else _state_dir() / "campaign.db"
        self._c = sqlite3.connect(str(self.path))
        self._c.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        on_disk_version = self._c.execute("PRAGMA user_version").fetchone()[0]
        if on_disk_version != SCHEMA_VERSION:
            self._c.executescript(
                "DROP TABLE IF EXISTS campaign; DROP TABLE IF EXISTS character;"
                "DROP TABLE IF EXISTS rooms; DROP TABLE IF EXISTS log;"
                "DROP TABLE IF EXISTS edges;"
            )
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
            -- location_id: which room this event happened in, if any (world-level events like
            -- "adventure.started" have none). This is what makes stigmergy possible — a LATER
            -- player entering this room can query "what happened here before I arrived,"
            -- distinct from player_id (whose events these are) and kind_prefix (what category).
            CREATE TABLE IF NOT EXISTS log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, text TEXT,
                player_id TEXT, location_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_log_location ON log(location_id);
            """
        )
        self._c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._c.commit()

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

    # --- campaign (shared world) -----------------------------------------------
    def new_campaign(self, *, theme: str, premise: str, start_room: str) -> None:
        """Only called when no campaign exists yet — does NOT wipe existing players' progress."""
        self._c.execute(
            "INSERT INTO campaign (id, theme, premise, created_at, start_room, turn) VALUES (1,?,?,?,?,0)",
            (theme, premise, time.time(), start_room),
        )
        self._c.commit()

    def campaign(self) -> Campaign | None:
        r = self._c.execute("SELECT * FROM campaign WHERE id=1").fetchone()
        return Campaign.model_validate(dict(r)) if r else None

    # --- character (one row per player_id) -------------------------------------
    def new_character(self, player_id: str, *, name: str, klass: str, hp: int, ac: int,
                      stats: dict[str, int], inventory: list[dict], location_id: str) -> Character:
        ch = Character(player_id=player_id, name=name, klass=klass, hp=hp, max_hp=hp, ac=ac,
                       stats=stats, inventory=inventory, location_id=location_id)
        self._c.execute(
            "INSERT OR REPLACE INTO character"
            " (player_id,name,klass,level,hp,max_hp,ac,stats,inventory,location_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ch.player_id, ch.name, ch.klass, ch.level, ch.hp, ch.max_hp, ch.ac,
             json.dumps(ch.stats), json.dumps(ch.inventory), ch.location_id),
        )
        self._c.commit()
        return ch

    def character(self, player_id: str) -> Character | None:
        r = self._c.execute("SELECT * FROM character WHERE player_id=?", (player_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["stats"] = json.loads(d["stats"] or "{}")
        d["inventory"] = json.loads(d["inventory"] or "[]")
        return Character.model_validate(d)

    def set_location(self, player_id: str, room_id: str) -> None:
        self._c.execute("UPDATE character SET location_id=? WHERE player_id=?", (room_id, player_id))
        self._c.execute("UPDATE campaign SET turn=turn+1 WHERE id=1")
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

    def players(self) -> list[Character]:
        """All characters currently in the shared world (for the GUI's 'other players' view)."""
        rows = self._c.execute("SELECT * FROM character").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stats"] = json.loads(d["stats"] or "{}")
            d["inventory"] = json.loads(d["inventory"] or "[]")
            out.append(Character.model_validate(d))
        return out

    # --- rooms ----------------------------------------------------------------
    def upsert_room(self, *, room_id: str, name: str, description: str, exits: dict[str, str],
                    contents: list[dict], image_ref: str | None = None,
                    features: list[str] | None = None, kind: str = "") -> Room:
        room = Room(id=room_id, name=name, description=description, exits=exits,
                   contents=contents, image_ref=image_ref, features=features or [], kind=kind)
        self._c.execute(
            "INSERT INTO rooms (id,name,description,contents,visited,image_ref,features,kind)"
            " VALUES (?,?,?,?,0,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description,"
            " contents=excluded.contents,"
            " image_ref=COALESCE(excluded.image_ref, rooms.image_ref),"
            " features=excluded.features,"
            " kind=CASE WHEN excluded.kind != '' THEN excluded.kind ELSE rooms.kind END",
            (room.id, room.name, room.description, json.dumps(room.contents),
             room.image_ref, json.dumps(room.features), room.kind),
        )
        self._c.commit()
        # exits are edges now, not a JSON column — sync separately. Delete-then-insert: the
        # caller always passes the room's complete current exit set (same semantics the old
        # JSON column had).
        self.set_edges("room", room_id, "room", room.exits)
        saved = self.room(room_id)  # re-read: picks up visited/COALESCE'd image_ref from the DB
        assert saved is not None, f"room {room_id} vanished immediately after its own upsert"
        return saved

    def room(self, room_id: str) -> Room | None:
        r = self._c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["exits"] = self.room_exits(room_id)
        d["contents"] = json.loads(d["contents"] or "[]")
        d["features"] = json.loads(d["features"] or "[]")
        d["visited"] = bool(d["visited"])
        d["kind"] = d.get("kind") or ""
        return Room.model_validate(d)

    def mark_visited(self, room_id: str) -> None:
        self._c.execute("UPDATE rooms SET visited=1 WHERE id=?", (room_id,))
        self._c.commit()

    def set_room_image(self, room_id: str, image_ref: str) -> None:
        self._c.execute("UPDATE rooms SET image_ref=? WHERE id=?", (image_ref, room_id))
        self._c.commit()

    # --- log (domain events) ---------------------------------------------------
    def log(self, kind: str, text: str, *, player_id: str | None = None) -> None:
        """Emit a domain event. `kind` should be dotted-namespace: "player.moved",
        "room.generated", "combat.resolved", "memory.noted", "adventure.started" — so the
        stream is filterable by category as well as by player."""
        self._c.execute("INSERT INTO log (ts,kind,text,player_id) VALUES (?,?,?,?)",
                        (time.time(), kind, text, player_id))
        self._c.commit()

    def recent_log(self, n: int = 10, *, player_id: str | None = None,
                   kind_prefix: str | None = None) -> list[LogEntry]:
        """Recent events, optionally filtered to one player (their own events + world-level
        ones with no actor) and/or one event category (e.g. kind_prefix="combat")."""
        where, params = [], []
        if player_id is not None:
            where.append("(player_id = ? OR player_id IS NULL)")
            params.append(player_id)
        if kind_prefix is not None:
            where.append("kind LIKE ?")
            params.append(f"{kind_prefix}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._c.execute(
            f"SELECT ts,kind,text,player_id FROM log {clause} ORDER BY seq DESC LIMIT ?",
            (*params, n),
        ).fetchall()
        return [LogEntry.model_validate(dict(r)) for r in reversed(rows)]

    def snapshot(self, player_id: str) -> dict:
        """Full inspectable state for one player — the 'world remembers' proof. Dict-shaped at
        the boundary (this is what get_state hands back over MCP) but built from validated models."""
        ch = self.character(player_id)
        camp = self.campaign()
        room = self.room(ch.location_id) if ch else None
        return {"campaign": camp.model_dump() if camp else None,
                "character": ch.model_dump() if ch else None,
                "current_room": room.model_dump() if room else None,
                "log": [entry.model_dump() for entry in self.recent_log(8, player_id=player_id)]}
