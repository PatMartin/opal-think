"""Contract tests: the guarded EVENT tool loop inside task execution."""

from __future__ import annotations

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


def _fake(task_client: TestClient):
    return next(a for a in task_client.app.state.providers.adapters() if a.provider_id == "fake")


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _script_task(client: TestClient, script: list[Generation], event_client=None) -> dict:
    _fake(client).set_script(script)
    if event_client is not None:
        client.app.state.event_client = event_client
    task_id = client.post("/api/v1/tasks/", json={"prompt": "diagnose the gpu", "model_id": "fake:script"}).json()["id"]
    return _wait_terminal(client, task_id)


def test_action_round_trip(task_client: TestClient) -> None:
    event = FakeEventClient()
    event.set_result("gpu_diagnostics", status="succeeded", result={"gpu_temp_c": 63})
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "gpu.diag", "data": {"level": 1}})]),
            Generation("GPU temperature is 63C"),
        ],
        event,
    )
    assert task["status"] == "completed"
    assert task["answer"] == "GPU temperature is 63C"
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert len(action_steps) == 1
    step = action_steps[0]
    assert step["status"] == "succeeded"
    assert step["action_id"] == "gpu_diagnostics"
    assert step["execution_id"] == "exec_1"
    assert step["action_input"] == {"event": {"topic": "gpu.diag", "data": {"level": 1}}}
    # the tool result was fed back to the model for the next reasoning iteration
    tool_msgs = [c for c in _fake(task_client).calls[-1]["messages"] if c["role"] == "tool"]
    assert tool_msgs and "63" in tool_msgs[0]["content"]


def test_unknown_action_is_not_executed(task_client: TestClient) -> None:
    event = FakeEventClient(actions=[{"id": "gpu_diagnostics", "type": "shell", "enabled": True}])
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("delete_everything", {"topic": "x", "data": {}})]),
            Generation("I cannot run that action; refusing."),
        ],
        event,
    )
    assert task["status"] == "completed"
    assert event.calls == []  # never submitted to EVENT
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert action_steps[0]["status"] == "failed"
    assert action_steps[0]["action_input"] is None


def test_no_event_client_records_failed_step(task_client: TestClient) -> None:
    task_client.app.state.event_client = None  # model-only deployment
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
            Generation("No action backend is available; answering directly."),
        ],
    )
    assert task["status"] == "completed"
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert action_steps[0]["status"] == "failed"


def test_action_input_redacts_secrets(task_client: TestClient) -> None:
    event = FakeEventClient()
    task = _script_task(
        task_client,
        [
            Generation(
                "",
                tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {"api_key": "SECRET", "fan": 50}})],
            ),
            Generation("done"),
        ],
        event,
    )
    assert task["status"] == "completed"
    step = [s for s in task["steps"] if s["kind"] == "action"][0]
    assert step["action_input"]["event"]["data"]["api_key"] == "***"
    assert step["action_input"]["event"]["data"]["fan"] == 50


def test_execution_failed_step(task_client: TestClient) -> None:
    event = FakeEventClient()
    event.set_result("gpu_diagnostics", status="failed", error={"message": "shell exited 1"})
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
            Generation("The diagnostic failed; here is what I know."),
        ],
        event,
    )
    assert task["status"] == "completed"
    step = [s for s in task["steps"] if s["kind"] == "action"][0]
    assert step["status"] == "failed"


def test_tools_passed_to_model(task_client: TestClient) -> None:
    event = FakeEventClient()
    _script_task(task_client, [Generation("plain answer")], event)
    tools = _fake(task_client).calls[-1].get("tools")
    assert tools and tools[0]["function"]["name"] == "gpu_diagnostics"
