"""Per-task telemetry: counts, usage aggregation, duration, no PII."""

from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _terminal(task_client: TestClient, task_id: str) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        task = task_client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError("task did not terminate")


def _logged_telemetry(caplog, task_id: str) -> dict:
    for record in caplog.records:
        message = record.getMessage()
        if task_id in message and "telemetry=" in message:
            return json.loads(message.split("telemetry=", 1)[1])
    return {}


def test_completed_task_logs_telemetry(task_client, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="opal_think.engine"):
        tid = task_client.post("/api/v1/task/", json={"prompt": "hello"}).json()["id"]
        task = _terminal(task_client, tid)
    assert task["status"] == "completed"
    tel = _logged_telemetry(caplog, tid)
    assert tel, "no structured telemetry logged for finished task"
    assert tel["provider_id"] == "fake" and tel["model_id"].startswith("fake:")
    assert tel["llm_calls"] >= 1 and tel["duration_ms"] >= 0
    assert isinstance(tel["usage"], dict)  # empty is fine — fake adapter reports none
    for key in ("tool_calls", "validation_rejections"):
        assert key in tel


def test_failed_task_still_logs_telemetry(task_client, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="opal_think.engine"):
        tid = task_client.post("/api/v1/task/", json={"prompt": "x", "max_steps": 1}).json()["id"]
        task = _terminal(task_client, tid)
    assert task["status"] in {"completed", "failed"}
    assert _logged_telemetry(caplog, tid), "no telemetry for terminal task"


def test_finish_emits_telemetry_to_live_subscribers(task_client) -> None:
    engine = task_client.app.state.engine
    queue = engine.subscribe("task_probe")
    sample = {"llm_calls": 2, "tool_calls": 1, "duration_ms": 5}
    engine._finish("task_probe", "completed", telemetry=sample)
    frame = queue.get_nowait()
    assert frame["type"] == "task.completed" and frame["data"]["telemetry"] == sample


def test_telemetry_never_contains_prompt(task_client, caplog) -> None:
    secret_prompt = "telemetry-pii-sentinel-zzz"
    with caplog.at_level(logging.INFO, logger="opal_think.engine"):
        tid = task_client.post("/api/v1/task/", json={"prompt": secret_prompt}).json()["id"]
        _terminal(task_client, tid)
    tel = _logged_telemetry(caplog, tid)
    assert secret_prompt not in json.dumps(tel)
