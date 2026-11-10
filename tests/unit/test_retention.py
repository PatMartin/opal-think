"""Unit tests: retention sweeps (the only task deletion path)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opal_think.store import ThinkStore


def _row(tid, session_id=None, status="completed", finished=None, created=None):
    now = datetime.now(UTC).isoformat()
    return {
        "id": tid, "session_id": session_id, "status": status, "prompt": "p",
        "model_id": "fake:echo", "max_steps": 8, "timeout_seconds": 120,
        "created_at": created or now, "finished_at": finished or now,
    }


def test_task_sweep_removes_old_terminal_only(tmp_path) -> None:
    store = ThinkStore(tmp_path / "t.db")
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    store.insert_task(_row("task_old", finished=old, created=old))
    store.insert_task(_row("task_new"))                    # recent -> keep
    store.insert_task(_row("task_running", status="running", finished=None))  # active -> keep
    removed, _ = store.sweep_retention(task_days=30, session_days=90)
    assert removed == 1
    assert store.get_task("task_old") is None
    assert store.get_task("task_new") is not None
    assert store.get_task("task_running") is not None


def test_session_sweep_cascades_when_no_active(tmp_path) -> None:
    store = ThinkStore(tmp_path / "t.db")
    old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    store.add_session(title="old")
    with store._lock, store._connect() as conn:
        conn.execute("UPDATE sessions SET updated_at = ?", (old,))
    sid = store.list_sessions(10)[0]["id"]
    store.insert_task(_row("task_hist", session_id=sid, finished=old, created=old))
    store.add_session(title="fresh")
    _, sessions_removed = store.sweep_retention(task_days=30, session_days=90)
    assert sessions_removed == 1
    assert store.get_session(sid) is None
    assert store.get_task("task_hist") is None
