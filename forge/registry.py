"""SQLite registry of minted tools + call telemetry.

Zero-ops persistence so the agent's minted fleet and the cost dashboard survive
across sessions. One file, no server. Defaults to ./.forge/registry.db (override
with FORGE_STATE_DIR).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path


def _state_dir() -> Path:
    path = Path(os.environ.get("FORGE_STATE_DIR", ".forge"))
    path.mkdir(parents=True, exist_ok=True)
    return path


class Registry:
    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else _state_dir() / "registry.db"
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tools (
                name        TEXT PRIMARY KEY,
                endpoint_id TEXT,
                gpu         TEXT,
                workers_min INTEGER DEFAULT 0,
                workers_max INTEGER DEFAULT 1,
                deps        TEXT,
                created_at  REAL
            );
            CREATE TABLE IF NOT EXISTS calls (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                tool      TEXT,
                ts        REAL,
                seconds   REAL,
                ok        INTEGER,
                worker_id TEXT,
                error     TEXT
            );
            """
        )
        self._conn.commit()

    def upsert_tool(self, *, name: str, gpu: str, workers_min: int, workers_max: int,
                    deps: list[str], endpoint_id: str | None = None) -> None:
        self._conn.execute(
            """INSERT INTO tools (name, endpoint_id, gpu, workers_min, workers_max, deps, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   endpoint_id=COALESCE(excluded.endpoint_id, tools.endpoint_id),
                   gpu=excluded.gpu, workers_min=excluded.workers_min,
                   workers_max=excluded.workers_max, deps=excluded.deps""",
            (name, endpoint_id, gpu, workers_min, workers_max, json.dumps(deps), time.time()),
        )
        self._conn.commit()

    def set_endpoint_id(self, name: str, endpoint_id: str) -> None:
        self._conn.execute("UPDATE tools SET endpoint_id=? WHERE name=?", (endpoint_id, name))
        self._conn.commit()

    def record_call(self, *, tool: str, seconds: float, ok: bool,
                    worker_id: str | None = None, error: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO calls (tool, ts, seconds, ok, worker_id, error) VALUES (?, ?, ?, ?, ?, ?)",
            (tool, time.time(), seconds, int(ok), worker_id, error),
        )
        self._conn.commit()

    def tools(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM tools ORDER BY created_at").fetchall()
        return [dict(r) | {"deps": json.loads(r["deps"] or "[]")} for r in rows]

    def call_records(self, tool: str | None = None) -> list[dict]:
        """Flat records joined with each tool's GPU — ready for cost.summarize()."""
        sql = (
            "SELECT c.tool, c.ts, c.seconds, c.ok, c.worker_id, c.error, t.gpu, t.workers_max "
            "FROM calls c LEFT JOIN tools t ON c.tool = t.name"
        )
        params: tuple = ()
        if tool:
            sql += " WHERE c.tool = ?"
            params = (tool,)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {"tool": r["tool"], "ts": r["ts"], "seconds": r["seconds"], "ok": bool(r["ok"]),
             "worker_id": r["worker_id"], "error": r["error"], "gpu": r["gpu"] or "ANY",
             "workers": r["workers_max"] or 1}
            for r in rows
        ]

    def forget_tool(self, name: str) -> None:
        self._conn.execute("DELETE FROM tools WHERE name=?", (name,))
        self._conn.commit()
