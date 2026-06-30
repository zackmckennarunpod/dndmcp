"""Persistent campaign state for DNDMCP — the world that remembers.

SQLite, zero-ops, one file. Holds the campaign, the character, the rooms/world graph,
the current position, and the session log. Survives across the whole session (and across
reconnects, since the file persists). Reskin of the kit's registry pattern.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path


def _state_dir() -> Path:
    path = Path(os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp")))
    path.mkdir(parents=True, exist_ok=True)
    return path


class World:
    """One save file = one campaign. Single active campaign at a time (solo RPG)."""

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else _state_dir() / "campaign.db"
        self._c = sqlite3.connect(str(self.path))
        self._c.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self._c.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaign (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                theme TEXT, premise TEXT, created_at REAL, current_room TEXT, turn INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS character (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT, klass TEXT, level INTEGER DEFAULT 1,
                hp INTEGER, max_hp INTEGER, ac INTEGER DEFAULT 12,
                stats TEXT, inventory TEXT
            );
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY, name TEXT, description TEXT,
                exits TEXT, contents TEXT, visited INTEGER DEFAULT 0, image_ref TEXT,
                features TEXT DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, text TEXT
            );
            """
        )
        self._c.commit()

    # --- campaign -------------------------------------------------------------
    def new_campaign(self, *, theme: str, premise: str, start_room: str) -> None:
        self._c.executescript("DELETE FROM campaign; DELETE FROM character; DELETE FROM rooms; DELETE FROM log;")
        self._c.execute(
            "INSERT INTO campaign (id, theme, premise, created_at, current_room, turn) VALUES (1,?,?,?,?,0)",
            (theme, premise, time.time(), start_room),
        )
        self._c.commit()

    def campaign(self) -> dict | None:
        r = self._c.execute("SELECT * FROM campaign WHERE id=1").fetchone()
        return dict(r) if r else None

    def set_room(self, room_id: str) -> None:
        self._c.execute("UPDATE campaign SET current_room=?, turn=turn+1 WHERE id=1", (room_id,))
        self._c.commit()

    # --- character ------------------------------------------------------------
    def set_character(self, *, name: str, klass: str, hp: int, ac: int,
                      stats: dict, inventory: list[str]) -> None:
        self._c.execute(
            "INSERT OR REPLACE INTO character (id,name,klass,level,hp,max_hp,ac,stats,inventory)"
            " VALUES (1,?,?,1,?,?,?,?,?)",
            (name, klass, hp, hp, ac, json.dumps(stats), json.dumps(inventory)),
        )
        self._c.commit()

    def character(self) -> dict | None:
        r = self._c.execute("SELECT * FROM character WHERE id=1").fetchone()
        if not r:
            return None
        d = dict(r)
        d["stats"] = json.loads(d["stats"] or "{}")
        d["inventory"] = json.loads(d["inventory"] or "[]")
        return d

    def damage(self, amount: int) -> int:
        cur = self.character()
        new_hp = max(0, (cur["hp"] if cur else 0) - amount)
        self._c.execute("UPDATE character SET hp=? WHERE id=1", (new_hp,))
        self._c.commit()
        return new_hp

    def add_item(self, item: str) -> None:
        cur = self.character()
        inv = (cur["inventory"] if cur else []) + [item]
        self._c.execute("UPDATE character SET inventory=? WHERE id=1", (json.dumps(inv),))
        self._c.commit()

    # --- rooms ----------------------------------------------------------------
    def upsert_room(self, *, room_id: str, name: str, description: str, exits: dict,
                    contents: list, image_ref: str | None = None,
                    features: list | None = None) -> None:
        self._c.execute(
            "INSERT INTO rooms (id,name,description,exits,contents,visited,image_ref,features)"
            " VALUES (?,?,?,?,?,0,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description,"
            " exits=excluded.exits, contents=excluded.contents,"
            " image_ref=COALESCE(excluded.image_ref, rooms.image_ref),"
            " features=excluded.features",
            (room_id, name, description, json.dumps(exits), json.dumps(contents),
             image_ref, json.dumps(features or [])),
        )
        self._c.commit()

    def room(self, room_id: str) -> dict | None:
        r = self._c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["exits"] = json.loads(d["exits"] or "{}")
        d["contents"] = json.loads(d["contents"] or "[]")
        d["features"] = json.loads(d["features"] or "[]")
        return d

    def mark_visited(self, room_id: str) -> None:
        self._c.execute("UPDATE rooms SET visited=1 WHERE id=?", (room_id,))
        self._c.commit()

    def set_room_image(self, room_id: str, image_ref: str) -> None:
        self._c.execute("UPDATE rooms SET image_ref=? WHERE id=?", (image_ref, room_id))
        self._c.commit()

    # --- log ------------------------------------------------------------------
    def log(self, kind: str, text: str) -> None:
        self._c.execute("INSERT INTO log (ts,kind,text) VALUES (?,?,?)", (time.time(), kind, text))
        self._c.commit()

    def recent_log(self, n: int = 10) -> list[dict]:
        rows = self._c.execute("SELECT kind,text FROM log ORDER BY seq DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def snapshot(self) -> dict:
        """Full inspectable state — the 'world remembers' proof."""
        return {"campaign": self.campaign(), "character": self.character(),
                "current_room": self.room((self.campaign() or {}).get("current_room", "")),
                "log": self.recent_log(8)}
