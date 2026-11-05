"""Restart/cancellation honesty: orphan reaping keeps EVENT execution links."""

from __future__ import annotations

from opal_think.engine import TaskEngine
from opal_think.store import ThinkStore


def _store(tmp_path):
    return ThinkStore(tmp_path / "t.db")


def _task(store: ThinkStore, status: str = "running") -> str:
    tid = ThinkStore.new_task_id()
    store.insert_task(
        {"id": tid, "prompt": "p", "model_id": "fake:echo", "status": "queued", "created_at": "2026-01-01T00:00:00Z"}
    )
    if status != "queued":
        store.update_task(tid, status=status)
    return tid


def test_reap_marks_active_tasks_failed(tmp_path) -> None:
    store = _store(tmp_path)
    tid = _task(store, "running")
    engine = TaskEngine(store, None, None)
    assert engine.reap_orphans() == 1
    task = store.get_task(tid)
    assert task["status"] == "failed" and task["error"]["code"] == "service_restarted"


def test_reap_preserves_last_event_execution_link(tmp_path) -> None:
    store = _store(tmp_path)
    tid = _task(store, "running")
    store.append_task_step(tid, {"kind": "action", "action_id": "act1", "execution_id": "exec_abc123", "status": "running"})
    engine = TaskEngine(store, None, None)
    engine.reap_orphans()
    error = store.get_task(tid)["error"]
    assert error["event_execution_id"] == "exec_abc123"
    assert "not rolled back" in error["message"]


def test_reap_without_execution_has_no_link(tmp_path) -> None:
    store = _store(tmp_path)
    tid = _task(store, "queued")
    TaskEngine(store, None, None).reap_orphans()
    error = store.get_task(tid)["error"]
    assert "event_execution_id" not in error


def test_reap_idempotent_on_terminal_tasks(tmp_path) -> None:
    store = _store(tmp_path)
    tid = _task(store, "completed")
    assert TaskEngine(store, None, None).reap_orphans() == 0
    assert store.get_task(tid)["status"] == "completed"
