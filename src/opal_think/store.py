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
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
                " steps TEXT NOT NULL DEFAULT '[]', steps_completed INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL, started_at TEXT,"
                " finished_at TEXT, causation_id TEXT, causation_depth INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at, id)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS idempotency ("
                "key TEXT PRIMARY KEY, task_id TEXT NOT NULL, body_hash TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            self._ensure_column(conn, "tasks", "context", "TEXT")
            self._ensure_column(conn, "tasks", "steps_completed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "tasks", "opal_session_id", "TEXT")
            self._ensure_column(conn, "tasks", "opal_transaction_id", "TEXT")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One locked read-modify-write unit (admission, cancellation, step updates)."""
        with self._lock, self._connect() as conn:
            yield conn

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
        """Remove the session, its task records, and their idempotency keys (irreversible)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM idempotency WHERE task_id IN (SELECT id FROM tasks WHERE session_id = ?)",
                (session_id,),
            )
            conn.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # -- tasks ---------------------------------------------------------------------------------

    @staticmethod
    def new_task_id() -> str:
        return _new_id("task")

    @staticmethod
    def _encode(value: Any) -> Any:
        return json.dumps(value) if isinstance(value, (dict, list)) else value

    @staticmethod
    def _task_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for field_name in ("metadata", "context", "steps", "error"):
            if data.get(field_name) is not None:
                data[field_name] = json.loads(data[field_name])
        return data

    def insert_task(self, fields: dict[str, Any]) -> None:
        """Create a queued task row (used by tests; the engine uses the conn variant)."""
        columns = ",".join(fields)
        placeholders = ",".join("?" for _ in fields)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",
                [self._encode(v) for v in fields.values()],
            )

    def insert_task_conn(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        conn.execute(f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", [self._encode(v) for v in row.values()])
        self.touch_session(conn, row.get("session_id"))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self.get_task_conn(conn, task_id)

    @staticmethod
    def get_task_conn(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return ThinkStore._task_row(row)

    def flag_cancel(self, task_id: str) -> bool:
        """Set cancel_requested only while non-terminal (single-writer guard)."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET cancel_requested = 1 WHERE id = ? AND status IN (?, ?)",
                (task_id, *ACTIVE_TASK_STATUSES),
            )
            return cursor.rowcount > 0

    @staticmethod
    def touch_session(conn: sqlite3.Connection, session_id: str | None) -> None:
        """Bump session.updated_at — ordering and retention reflect recent use."""
        if session_id:
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                [self._encode(v) for v in fields.values()] + [task_id],
            )
            if "finished_at" in fields:
                # terminal transition counts as session activity
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = (SELECT session_id FROM tasks WHERE id = ?)",
                    (fields["finished_at"], task_id),
                )

    def append_task_step(self, task_id: str, step: dict[str, Any]) -> dict[str, Any]:
        """Append a bounded step summary (auto-indexed) and bump steps_completed."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT steps FROM tasks WHERE id = ?", (task_id,)).fetchone()
            steps = json.loads(row["steps"]) if row and row["steps"] else []
            step = {"index": len(steps) + 1, **step}
            steps.append(step)
            conn.execute(
                "UPDATE tasks SET steps = ?, steps_completed = ? WHERE id = ?",
                (json.dumps(steps), len(steps), task_id),
            )
        return step

    def list_tasks(
        self,
        limit: int,
        inner_cursor: str | None,
        status: str | None = None,
        session_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Newest-first (created_at, id) DESC keyset over the given filters."""
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if inner_cursor:
            sort_at, last_id = inner_cursor.rsplit("|", 1)
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([sort_at, sort_at, last_id])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        items = [self._task_row(row) for row in rows[:limit]]
        next_inner = None
        if len(rows) > limit:
            last = items[-1]
            next_inner = f"{last['created_at']}|{last['id']}"
        return items, next_inner

    def session_turns(self, session_id: str, limit: int) -> list[tuple[str, str]]:
        """(prompt, answer) pairs of completed tasks in the session, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT prompt, answer FROM tasks WHERE session_id = ? AND status = 'completed'"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [(row["prompt"], row["answer"] or "") for row in reversed(rows)]

    def sweep_retention(self, task_days: int, session_days: int) -> tuple[int, int]:
        """Retention sweeps (the only task deletion; there is no task DELETE endpoint).

        Terminal tasks older than ``task_days`` are removed. Sessions idle longer than
        ``session_days`` (by ``updated_at``) with no active tasks cascade-delete their
        retained task history. Returns (tasks_removed, sessions_removed).
        """
        task_cutoff = (datetime.now(UTC) - timedelta(days=task_days)).isoformat()
        session_cutoff = (datetime.now(UTC) - timedelta(days=session_days)).isoformat()
        with self._lock, self._connect() as conn:
            tasks_removed = conn.execute(
                "DELETE FROM tasks WHERE status IN ('completed','failed','cancelled','timed_out') AND finished_at < ?",
                (task_cutoff,),
            ).rowcount
            stale = conn.execute(
                "SELECT id FROM sessions WHERE updated_at < ? AND id NOT IN"
                " (SELECT DISTINCT session_id FROM tasks WHERE status IN (?, ?))",
                (session_cutoff, *ACTIVE_TASK_STATUSES),
            ).fetchall()
            for row in stale:
                conn.execute("DELETE FROM tasks WHERE session_id = ?", (row["id"],))
            sessions_removed = conn.execute(
                "DELETE FROM sessions WHERE updated_at < ? AND id NOT IN"
                " (SELECT DISTINCT session_id FROM tasks WHERE status IN (?, ?))",
                (session_cutoff, *ACTIVE_TASK_STATUSES),
            ).rowcount
            # no orphan keys: idempotency entries die with their task records
            conn.execute("DELETE FROM idempotency WHERE task_id NOT IN (SELECT id FROM tasks)")
        return tasks_removed, sessions_removed

    def fail_active_tasks(self, error: dict[str, Any]) -> int:
        """Fail every queued/running task (used to reap orphans at startup).

        When a task's last action step carries an EVENT ``execution_id``, the
        recorded error includes it as ``event_execution_id`` so an operator can
        inspect the downstream execution. We never claim rollback or
        cancellation of an already-admitted EVENT side effect.
        """
        now = _now()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, steps FROM tasks WHERE status IN (?, ?)", tuple(ACTIVE_TASK_STATUSES)
            ).fetchall()
            for row in rows:
                task_error = dict(error)
                steps = json.loads(row["steps"]) if row["steps"] else []
                execution_ids = [s.get("execution_id") for s in steps if s.get("execution_id")]
                if execution_ids:
                    task_error["event_execution_id"] = execution_ids[-1]
                    task_error["message"] += "; a downstream EVENT execution was admitted and is not rolled back"
                conn.execute(
                    "UPDATE tasks SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                    (json.dumps(task_error), now, row["id"]),
                )
            return len(rows)

    # -- admission helpers (call inside transaction()) ------------------------------------------

    def session_busy(self, conn: sqlite3.Connection, session_id: str) -> bool:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE session_id = ? AND status IN (?, ?)",
            (session_id, *ACTIVE_TASK_STATUSES),
        ).fetchone()
        return row["n"] > 0

    def count_active(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status IN (?, ?)", ACTIVE_TASK_STATUSES).fetchone()
        return row["n"]

    def count_active_now(self) -> int:
        with self._connect() as conn:
            return self.count_active(conn)

    def lookup_idempotency(self, conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT task_id, body_hash FROM idempotency WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def insert_idempotency(self, conn: sqlite3.Connection, key: str, task_id: str, body_hash: str) -> None:
        conn.execute(
            "INSERT INTO idempotency (key, task_id, body_hash, created_at) VALUES (?,?,?,?)",
            (key, task_id, body_hash, _now()),
        )
