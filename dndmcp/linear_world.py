"""A second, independent world proving the engine generalizes beyond D&D: a task graph
instead of a dungeon. Fully isolated from the D&D `World`/`Room`/`Character` — its own file,
its own schema, its own SQLite database. Same underlying pattern (typed node + generic edges
table + generate-on-traversal via Flash), different domain: `Ticket` instead of `Room`,
relation types (blocks/blocked_by/related_to) instead of compass directions.

See dndmcp/GENERATION_FEATURES.md and the design discussion in this session for why this is
a separate typed model rather than a generic blob shared with Room — collapsing them would
undo the type-safety work done on the D&D side (Pydantic + extra="forbid" + mypy).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = 1


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    status: str = "todo"  # todo | in_progress | done
    priority: str = "medium"  # low | medium | high


def _state_dir() -> Path:
    path = Path(os.environ.get("DNDMCP_STATE_DIR", os.path.expanduser("~/.dndmcp")))
    path.mkdir(parents=True, exist_ok=True)
    return path


class TicketWorld:
    """One save file = one task graph. Independent edges table from the D&D World's — a
    schema-version wipe on one can never touch the other."""

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else _state_dir() / "linear_world.db"
        self._c = sqlite3.connect(str(self.path))
        self._c.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        on_disk_version = self._c.execute("PRAGMA user_version").fetchone()[0]
        if on_disk_version != SCHEMA_VERSION:
            self._c.executescript("DROP TABLE IF EXISTS tickets; DROP TABLE IF EXISTS ticket_edges;")
        self._c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY, title TEXT, description TEXT,
                status TEXT DEFAULT 'todo', priority TEXT DEFAULT 'medium'
            );
            CREATE TABLE IF NOT EXISTS ticket_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id TEXT NOT NULL, to_id TEXT NOT NULL, edge_type TEXT NOT NULL,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ticket_edges_from ON ticket_edges(from_id);
            CREATE INDEX IF NOT EXISTS idx_ticket_edges_to ON ticket_edges(to_id);
            """
        )
        self._c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._c.commit()

    # --- tickets ----------------------------------------------------------------
    def new_ticket(self, *, title: str, description: str, status: str = "todo",
                  priority: str = "medium", ticket_id: str | None = None) -> Ticket:
        t = Ticket(id=ticket_id or uuid.uuid4().hex[:8], title=title, description=description,
                   status=status, priority=priority)
        self._c.execute(
            "INSERT OR REPLACE INTO tickets (id,title,description,status,priority) VALUES (?,?,?,?,?)",
            (t.id, t.title, t.description, t.status, t.priority),
        )
        self._c.commit()
        return t

    def ticket(self, ticket_id: str) -> Ticket | None:
        r = self._c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return Ticket.model_validate(dict(r)) if r else None

    def all_tickets(self) -> list[Ticket]:
        rows = self._c.execute("SELECT * FROM tickets").fetchall()
        return [Ticket.model_validate(dict(r)) for r in rows]

    def set_status(self, ticket_id: str, status: str) -> None:
        self._c.execute("UPDATE tickets SET status=? WHERE id=?", (status, ticket_id))
        self._c.commit()

    # --- edges (relations between tickets) ---------------------------------------
    def link(self, from_id: str, to_id: str, edge_type: str) -> None:
        self._c.execute(
            "INSERT INTO ticket_edges (from_id,to_id,edge_type,created_at) VALUES (?,?,?,?)",
            (from_id, to_id, edge_type, time.time()),
        )
        self._c.commit()

    def edges_from(self, ticket_id: str) -> list[dict]:
        rows = self._c.execute(
            "SELECT to_id, edge_type FROM ticket_edges WHERE from_id=?", (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def edges_to(self, ticket_id: str) -> list[dict]:
        rows = self._c.execute(
            "SELECT from_id, edge_type FROM ticket_edges WHERE to_id=?", (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def neighbors(self, ticket_id: str) -> list[tuple[str, Ticket]]:
        """(edge_type, neighbor_ticket) pairs — both directions, for prompt context."""
        out = []
        for e in self.edges_from(ticket_id):
            t = self.ticket(e["to_id"])
            if t:
                out.append((e["edge_type"], t))
        for e in self.edges_to(ticket_id):
            t = self.ticket(e["from_id"])
            if t:
                out.append((f"{e['edge_type']} (reverse)", t))
        return out
