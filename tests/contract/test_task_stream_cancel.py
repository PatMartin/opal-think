"""Contract tests: GET /tasks/{id}/stream SSE and POST /tasks/{id}/cancel."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _create(client: TestClient, prompt: str = "hi there friend") -> str:
    return client.post("/api/v1/task/", json={"prompt": prompt}).json()["id"]


def _frames(response) -> list[dict]:
    frames, event = [], None
    for line in response.iter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            frames.append(json.loads(line[5:].strip()) | {"_event": event})
    return frames


def _set_delay(client: TestClient, seconds: float) -> None:
    fake = next(a for a in client.app.state.providers.adapters() if a.provider_id == "fake")
    fake.delay = seconds


def test_stream_lifecycle_events(task_client: TestClient) -> None:
    _set_delay(task_client, 0.4)
    task_id = _create(task_client)
    with task_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["X-Opal-Request-Id"]
        frames = _frames(response)
    types = [f["type"] for f in frames]
    assert types[0] == "task.progress"  # connect-time status frame is guaranteed
    assert "task.output" in types
    assert types[-1] == "task.completed"
    for f in frames:
        assert f["task_id"] == task_id and f["timestamp"] and "data" in f
    output_text = "".join(f["data"].get("text", "") for f in frames if f["type"] == "task.output")
    assert "hi there friend" in output_text


def test_stream_late_connect_terminal_snapshot(task_client: TestClient) -> None:
    task_id = _create(task_client)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and task_client.get(f"/api/v1/task/{task_id}").json()["status"] not in {
        "completed",
        "failed",
    }:
        time.sleep(0.05)
    with task_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
        frames = _frames(response)
    assert len(frames) == 1
    assert frames[0]["type"] == "task.completed"
    assert frames[0]["data"]["status"] == "completed"
    assert "answer" in frames[0]["data"]


def test_stream_unknown_task(task_client: TestClient) -> None:
    assert task_client.get("/api/v1/task/task_missing/stream").status_code == 404


def test_cancel_running_task(task_client: TestClient) -> None:
    _set_delay(task_client, 3.0)
    task_id = _create(task_client)
    time.sleep(0.1)  # let the runner start
    response = task_client.post(f"/api/v1/task/{task_id}/cancel")
    assert response.status_code == 202
    assert response.json()["cancel_requested"] is True
    # repeated cancellation is idempotent while in-progress: 202 if still running,
    # 409 if the (fast) cancellation already completed — both contract-valid
    assert task_client.post(f"/api/v1/task/{task_id}/cancel").status_code in {202, 409}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = task_client.get(f"/api/v1/task/{task_id}").json()["status"]
        if status != "running":
            break
        time.sleep(0.05)
    assert status == "cancelled"
    # terminal -> 409
    assert task_client.post(f"/api/v1/task/{task_id}/cancel").status_code == 409


def test_cancel_unknown_task(task_client: TestClient) -> None:
    assert task_client.post("/api/v1/task/task_missing/cancel").status_code == 404
