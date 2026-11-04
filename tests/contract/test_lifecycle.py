"""Contract tests: session activity tracking, retention cascades, idempotency lifecycle."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 10.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not terminate")


def test_task_create_bumps_session_activity(task_client: TestClient) -> None:
    session_id = task_client.post("/api/v1/sessions/", json={"title": "s"}).json()["id"]
    before = task_client.get(f"/api/v1/sessions/{session_id}").json()["updated_at"]
    task_client.post("/api/v1/tasks/", json={"prompt": "hi", "session_id": session_id})
    after = task_client.get(f"/api/v1/sessions/{session_id}").json()["updated_at"]
    assert after > before


def test_task_finish_bumps_session_activity(task_client: TestClient) -> None:
    session_id = task_client.post("/api/v1/sessions/", json={"title": "s"}).json()["id"]
    task_id = task_client.post("/api/v1/tasks/", json={"prompt": "hi", "session_id": session_id}).json()["id"]
    mid = task_client.get(f"/api/v1/sessions/{session_id}").json()["updated_at"]
    _wait_terminal(task_client, task_id)
    after = task_client.get(f"/api/v1/sessions/{session_id}").json()["updated_at"]
    assert after >= mid


def test_retention_preserves_active_and_removes_stale(task_client: TestClient) -> None:
    store = task_client.app.state.store
    fresh = task_client.post("/api/v1/sessions/", json={"title": "fresh"}).json()["id"]
    stale = task_client.post("/api/v1/sessions/", json={"title": "stale"}).json()["id"]
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    with store.transaction() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (old, stale))
    removed_tasks, removed_sessions = store.sweep_retention(task_days=30, session_days=30)
    assert removed_sessions >= 1
    assert task_client.get(f"/api/v1/sessions/{stale}").status_code == 404
    assert task_client.get(f"/api/v1/sessions/{fresh}").status_code == 200


def test_stale_session_with_active_task_survives(task_client: TestClient) -> None:
    store = task_client.app.state.store
    session_id = task_client.post("/api/v1/sessions/", json={"title": "busy"}).json()["id"]
    task_client.app.state.providers.adapters()  # warm
    fake = next(a for a in task_client.app.state.providers.adapters() if a.provider_id == "fake")
    fake.delay = 5.0  # keep the task running during the sweep
    task_id = task_client.post("/api/v1/tasks/", json={"prompt": "long", "session_id": session_id}).json()["id"]
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    with store.transaction() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (old, session_id))
    store.sweep_retention(task_days=30, session_days=30)
    assert task_client.get(f"/api/v1/sessions/{session_id}").status_code == 200  # active task protects it
    task_client.post(f"/api/v1/tasks/{task_id}/cancel")


def test_sweep_removes_orphan_idempotency(task_client: TestClient) -> None:
    store = task_client.app.state.store
    resp = task_client.post("/api/v1/tasks/", json={"prompt": "x"}, headers={"Idempotency-Key": "k-expire"})
    task_id = resp.json()["id"]
    _wait_terminal(task_client, task_id)
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET finished_at = ? WHERE id = ?", (old, task_id))
    store.sweep_retention(task_days=30, session_days=30)
    with store.transaction() as conn:
        assert store.lookup_idempotency(conn, "k-expire") is None


def test_replay_after_task_deleted_is_fresh(task_client: TestClient) -> None:
    session_id = task_client.post("/api/v1/sessions/", json={"title": "s"}).json()["id"]
    first = task_client.post(
        "/api/v1/tasks/",
        json={"prompt": "x", "session_id": session_id},
        headers={"Idempotency-Key": "k-replay"},
    ).json()["id"]
    _wait_terminal(task_client, first)
    assert task_client.delete(f"/api/v1/sessions/{session_id}").status_code == 204
    second = task_client.post(
        "/api/v1/tasks/",
        json={"prompt": "x", "session_id": session_id},
        headers={"Idempotency-Key": "k-replay"},
    )
    # session gone -> the task insert 404s on missing session or creates fresh;
    # either way the stale key must not return a dead task
    assert second.status_code in (202, 404)
    if second.status_code == 202:
        assert second.json()["id"] != first


def test_delete_session_cascades_idempotency(task_client: TestClient) -> None:
    store = task_client.app.state.store
    session_id = task_client.post("/api/v1/sessions/", json={"title": "s"}).json()["id"]
    task_id = task_client.post(
        "/api/v1/tasks/",
        json={"prompt": "x", "session_id": session_id},
        headers={"Idempotency-Key": "k-cascade"},
    ).json()["id"]
    _wait_terminal(task_client, task_id)
    task_client.delete(f"/api/v1/sessions/{session_id}")
    with store.transaction() as conn:
        assert store.lookup_idempotency(conn, "k-cascade") is None
