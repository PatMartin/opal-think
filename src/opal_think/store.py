"""SQLite persistence for sessions and tasks.

Sessions are durable context containers; tasks are the unit of work (populated by the
task engine in a later step). Both tables live alongside the settings tables in the same
database file. A short lock serializes writes; reads use fresh short-lived connections.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ACTIVE_TASK_STATUSES = ("queued", "running")
TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled", "timed_out")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class ThinkStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "id TEXT PRIMARY KEY, title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "id TEXT PRIMARY KEY, session_id TEXT, status TEXT NOT NULL, prompt TEXT NOT NULL,"
                " model_id TEXT, max_steps INTEGER, timeout_seconds INTEGER, metadata TEXT,"
                " cancel_requested INTEGER NOT NULL DEFAULT 0, answer TEXT, error TEXT,"
                " steps TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, started_at TEXT,"
                " finished_at TEXT, causation_id TEXT, causation_depth INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at, id)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # -- sessions ------------------------------------------------------------------------------

    @staticmethod
    def _session_row(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT s.id, s.title, s.created_at, s.updated_at,"
            " (SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id) AS task_count"
            " FROM sessions s WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def add_session(self, title: str | None) -> dict[str, Any]:
        now = _now()
        session_id = _new_id("sess")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                (session_id, title, now, now),
            )
            return self._session_row(conn, session_id)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._session_row(conn, session_id)

    def list_sessions(self, limit: int, inner_cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """Newest-updated first, deterministic (updated_at, id) DESC keyset.

        Returns up to ``limit`` items plus the inner cursor for the next page (or None)."""
        with self._connect() as conn:
            if inner_cursor:
                sort_at, last_id = inner_cursor.rsplit("|", 1)
                rows = conn.execute(
                    "SELECT s.id, s.title, s.created_at, s.updated_at,"
                    " (SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id) AS task_count"
                    " FROM sessions s WHERE (s.updated_at < ?) OR (s.updated_at = ? AND s.id < ?)"
                    " ORDER BY s.updated_at DESC, s.id DESC LIMIT ?",
                    (sort_at, sort_at, last_id, limit + 1),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT s.id, s.title, s.created_at, s.updated_at,"
                    " (SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id) AS task_count"
                    " FROM sessions s ORDER BY s.updated_at DESC, s.id DESC LIMIT ?",
                    (limit + 1,),
                ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        next_inner = None
        if len(rows) > limit:
            last = items[-1]
            next_inner = f"{last['updated_at']}|{last['id']}"
        return items, next_inner

    def session_has_active_tasks(self, session_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE session_id = ? AND status IN (?,?)",
                (session_id, *ACTIVE_TASK_STATUSES),
            ).fetchone()
        return row["n"] > 0

    def delete_session(self, session_id: str) -> None:
        """Remove the session and all retained linked task records (irreversible)."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # -- tasks ---------------------------------------------------------------------------------

    def insert_task(self, fields: dict[str, Any]) -> None:
        """Create a queued task row (used by the engine and by busy-check tests)."""
        columns = ",".join(fields)
        placeholders = ",".join("?" for _ in fields)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",
                [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()],
            )

    @staticmethod
    def new_task_id() -> str:
        return _new_id("task")
