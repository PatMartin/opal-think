"""Contract tests: one absolute task deadline + safe cancellation semantics."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from opal_think.event_client import FakeEventClient
from opal_think.providers import Generation, ToolCallRequest


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _fake(client: TestClient):
    return next(a for a in client.app.state.providers.adapters() if a.provider_id == "fake")


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 10.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _frames(response) -> list[dict]:
    frames, event = [], None
    for line in response.iter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            frames.append(json.loads(line[5:].strip()) | {"_event": event})
    return frames


def test_absolute_deadline_bounds_all_steps(task_client: TestClient) -> None:
    """4 scripted tool-call steps at 0.5s each + 1s task deadline -> timed_out ~1s,
    not 1s per step (per-call budgets would allow ~2.5s of work)."""
    fake = _fake(task_client)
    fake.delay = 0.5
    task_client.app.state.event_client = FakeEventClient()
    script = [Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]) for _ in range(4)]
    script.append(Generation("final answer"))
    fake.set_script(script)
    started = time.monotonic()
    task_id = task_client.post(
        "/api/v1/task/", json={"prompt": "multi", "model_id": "fake:script", "timeout_seconds": 1}
    ).json()["id"]
    task = _wait_terminal(task_client, task_id)
    elapsed = time.monotonic() - started
    assert task["status"] == "timed_out"
    assert elapsed < 2.2, f"deadline exceeded: {elapsed:.2f}s for a 1s task"
    assert task["error"]["code"] == "timed_out"


def test_cancel_during_event_poll(task_client: TestClient) -> None:
    fake = _fake(task_client)
    event = FakeEventClient()
    event.set_result("gpu_diagnostics", status="running")  # never terminal -> poll loop
    task_client.app.state.event_client = event
    fake.set_script([Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})])])
    task_id = task_client.post(
        "/api/v1/task/", json={"prompt": "x", "model_id": "fake:script"}
    ).json()["id"]
    time.sleep(0.3)  # let it reach the poll loop
    assert task_client.post(f"/api/v1/task/{task_id}/cancel").status_code == 202
    task = _wait_terminal(task_client, task_id, seconds=4)
    assert task["status"] == "cancelled"


def test_single_terminal_frame_on_cancel(task_client: TestClient) -> None:
    fake = _fake(task_client)
    fake.delay = 3.0
    task_id = task_client.post("/api/v1/task/", json={"prompt": "slow"}).json()["id"]
    time.sleep(0.1)
    import threading

    frames_holder: list = []
    done = threading.Event()

    def watch():
        with task_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
            frames_holder.extend(_frames(response))
        done.set()

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    time.sleep(0.3)
    task_client.post(f"/api/v1/task/{task_id}/cancel")
    assert done.wait(6)
    cancelled = [f for f in frames_holder if f["type"] == "task.cancelled"]
    assert len(cancelled) == 1, f"expected exactly one terminal frame: {frames_holder}"
    assert frames_holder[-1]["type"] == "task.cancelled"


def test_terminal_task_unaffected_by_late_cancel_flag(task_client: TestClient) -> None:
    task_id = task_client.post("/api/v1/task/", json={"prompt": "quick"}).json()["id"]
    task = _wait_terminal(task_client, task_id)
    assert task["status"] == "completed"
    # router returns 409 on terminal; even a direct flag attempt cannot resurrect
    assert task_client.app.state.store.flag_cancel(task_id) is False
