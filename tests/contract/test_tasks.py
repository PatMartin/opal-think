"""Contract tests: POST /api/v1/task/ admission + GET show/list."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    """Deterministic model path: fake provider enabled, ollama off the dead default."""
    client.app.state.settings.store.set_many(
        {
            "providers.ollama.enabled": False,
            "providers.fake.type": "fake",
            "providers.fake.enabled": True,
        }
    )
    return client


def _create(client: TestClient, prompt: str = "hello", **extra) -> TestClient:
    body = {"prompt": prompt}
    body.update(extra)
    return client.post("/api/v1/task/", json=body)


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def test_create_task_accepted(task_client: TestClient) -> None:
    response = _create(task_client, metadata={"evalset": "smoke", "case_id": "c1"})
    assert response.status_code == 202, response.text
    assert "/api/v1/task/" in response.headers["Location"]
    body = response.json()
    assert body["id"].startswith("task_")
    assert body["status"] in {"queued", "running", "completed"}
    assert body["metadata"] == {"evalset": "smoke", "case_id": "c1"}
    assert body["model_id"].startswith("fake:")  # resolved default
    for field in ("steps_completed", "cancel_requested", "answer", "error", "steps", "created_at", "started_at", "finished_at"):
        assert field in body


def test_task_completes_with_answer(task_client: TestClient) -> None:
    task_id = _create(task_client, "ping").json()["id"]
    task = _wait_terminal(task_client, task_id)
    assert task["status"] == "completed"
    assert task["answer"] == "echo: ping"
    assert task["finished_at"]
    assert task["steps_completed"] >= 1
    assert task["steps"][0]["kind"] == "reasoning"
    assert task["steps"][0]["status"] == "succeeded"


def test_create_task_session_not_found(task_client: TestClient) -> None:
    assert _create(task_client, session_id="sess_missing").status_code == 404


def test_create_task_session_busy_conflict(task_client: TestClient) -> None:
    session = task_client.post("/api/v1/session/", json={}).json()
    task_client.app.state.store.insert_task(
        {"id": "task_busy", "session_id": session["id"], "status": "running", "prompt": "x", "created_at": "2026-01-01"}
    )
    response = _create(task_client, session_id=session["id"])
    assert response.status_code == 409
    assert "busy" in response.json()["error"]["message"] or "queued or running" in response.json()["error"]["message"]


def test_idempotency_key_replay(task_client: TestClient) -> None:
    body = {"prompt": "same"}
    first = task_client.post("/api/v1/task/", json=body, headers={"Idempotency-Key": "k-1"})
    second = task_client.post("/api/v1/task/", json=body, headers={"Idempotency-Key": "k-1"})
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    conflict = task_client.post("/api/v1/task/", json={"prompt": "different"}, headers={"Idempotency-Key": "k-1"})
    assert conflict.status_code == 409


def test_concurrency_limit_429(task_client: TestClient) -> None:
    task_client.app.state.settings.store.set_many({"task.max_concurrent": 1})
    task_client.app.state.store.insert_task(
        {"id": "task_active", "status": "running", "prompt": "x", "created_at": "2026-01-01"}
    )
    assert _create(task_client).status_code == 429


def test_unknown_model_rejected(task_client: TestClient) -> None:
    assert _create(task_client, model_id="nope").status_code == 422
    assert _create(task_client, model_id="fake:missing").status_code == 422
    assert _create(task_client, model_id="ollama:x").status_code == 422  # provider disabled


def test_no_model_available_503(client: TestClient) -> None:
    client.app.state.settings.store.set_many({"providers.ollama.base_url": "http://127.0.0.1:1"})
    assert client.post("/api/v1/task/", json={"prompt": "x"}).status_code == 503


def test_causation_depth_bound(task_client: TestClient) -> None:
    task_client.app.state.settings.store.set_many({"task.max_causation_depth": 1})
    parent = _create(task_client).json()  # depth 0
    child = task_client.post(  # depth 1 -> allowed
        "/api/v1/task/", json={"prompt": "child"}, headers={"X-Opal-Causation-Id": parent["id"]}
    )
    assert child.status_code == 202
    grandchild = task_client.post(  # depth 2 > limit 1 -> rejected
        "/api/v1/task/", json={"prompt": "grandchild"}, headers={"X-Opal-Causation-Id": child.json()["id"]}
    )
    assert grandchild.status_code == 422
    assert grandchild.json()["error"]["code"] == "causation_depth_exceeded"


def test_show_task_404(task_client: TestClient) -> None:
    assert task_client.get("/api/v1/task/task_missing").status_code == 404


def test_list_tasks_filters_and_pagination(task_client: TestClient) -> None:
    session = task_client.post("/api/v1/session/", json={}).json()
    _wait_terminal(task_client, _create(task_client, "a").json()["id"])
    busy = task_client.app.state.store
    busy.insert_task(
        {
            "id": "task_q",
            "session_id": session["id"],
            "status": "queued",
            "prompt": "q",
            "model_id": "fake:echo",
            "max_steps": 8,
            "timeout_seconds": 120,
            "created_at": "2999-01-01",
        }
    )

    queued = task_client.get("/api/v1/task/?status=queued").json()
    assert [t["id"] for t in queued["items"]] == ["task_q"]

    by_session = task_client.get(f"/api/v1/task/?session_id={session['id']}").json()
    assert {t["id"] for t in by_session["items"]} == {"task_q"}

    page = task_client.get("/api/v1/task/?limit=1").json()
    assert page["has_more"] is True
    page2 = task_client.get(f"/api/v1/task/?limit=1&cursor={page['next_cursor']}").json()
    assert page["items"][0]["id"] != page2["items"][0]["id"]

    # cursor bound to filters: reuse under a different filter -> 422
    assert task_client.get("/api/v1/task/", params={"cursor": page["next_cursor"], "status": "failed"}).status_code == 422
    assert task_client.get("/api/v1/task/", params={"cursor": "junk"}).status_code == 422


def test_task_prompt_validation(task_client: TestClient) -> None:
    assert _create(task_client, prompt="").status_code == 422
    too_long = task_client.post("/api/v1/task/", json={"prompt": "x" * 40000})
    assert too_long.status_code == 422
