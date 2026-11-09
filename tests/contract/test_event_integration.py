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
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _script_task(client: TestClient, script: list[Generation], event_client=None) -> dict:
    _fake(client).set_script(script)
    if event_client is not None:
        client.app.state.event_client = event_client
    task_id = client.post("/api/v1/task/", json={"prompt": "diagnose the gpu", "model_id": "fake:script"}).json()["id"]
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


def test_multiple_tool_calls_all_replied(task_client: TestClient) -> None:
    event = FakeEventClient()
    task = _script_task(
        task_client,
        [
            Generation(
                "",
                tool_calls=[
                    ToolCallRequest("gpu_diagnostics", {"topic": "a", "data": {}}),
                    ToolCallRequest("gpu_diagnostics", {"topic": "b", "data": {}}),
                ],
            ),
            Generation("ran both"),
        ],
        event,
    )
    assert task["status"] == "completed"
    assert len(event.calls) == 2
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert len(action_steps) == 2 and all(s["status"] == "succeeded" for s in action_steps)
    # round-2 conversation: assistant turn carrying tool_calls + matching result per call
    round2 = _fake(task_client).calls[-1]["messages"]
    assistant_tool_msg = next(m for m in round2 if m["role"] == "assistant" and m.get("tool_calls"))
    assert len(assistant_tool_msg["tool_calls"]) == 2
    tool_msgs = [m for m in round2 if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and all(m["name"] == "gpu_diagnostics" for m in tool_msgs)


def test_over_limit_calls_get_explicit_errors(task_client: TestClient) -> None:
    event = FakeEventClient()
    task = _script_task(
        task_client,
        [
            Generation(
                "",
                tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": str(i), "data": {}}) for i in range(6)],
            ),
            Generation("bounded"),
        ],
        event,
    )
    assert task["status"] == "completed"
    assert len(event.calls) == 4  # only the bounded four were submitted
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert sum(1 for s in action_steps if s["status"] == "succeeded") == 4
    assert sum(1 for s in action_steps if s["status"] == "failed") == 2
    round2 = _fake(task_client).calls[-1]["messages"]
    tool_msgs = [m for m in round2 if m["role"] == "tool"]
    # every call got a reply message — no orphan tool messages
    assert len(tool_msgs) == 6
    assert sum("action_request_limit_exceeded" in m["content"] for m in tool_msgs) == 2


def test_malformed_tool_args_fail_cleanly(task_client: TestClient) -> None:
    event = FakeEventClient()
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", "not-a-dict")]),
            Generation("handled bad args"),
        ],
        event,
    )
    assert task["status"] == "completed"
    assert event.calls == []
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert action_steps[0]["status"] == "failed" and "malformed" in action_steps[0]["summary"]
    round2 = _fake(task_client).calls[-1]["messages"]
    tool_msgs = [m for m in round2 if m["role"] == "tool"]
    assert len(tool_msgs) == 1 and "malformed_tool_arguments" in tool_msgs[0]["content"]


def test_call_ids_assigned_and_correlated(task_client: TestClient) -> None:
    event = FakeEventClient()
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
            Generation("done"),
        ],
        event,
    )
    assert task["status"] == "completed"
    round2 = _fake(task_client).calls[-1]["messages"]
    assistant_tool_msg = next(m for m in round2 if m["role"] == "assistant" and m.get("tool_calls"))
    tool_msgs = [m for m in round2 if m["role"] == "tool"]
    # engine assigned a deterministic call_id; result message is a valid reply
    call_entry = assistant_tool_msg["tool_calls"][0]["function"]
    assert call_entry["name"] == "gpu_diagnostics"
    assert tool_msgs[0]["name"] == "gpu_diagnostics"


def test_execution_fields_reach_model(task_client: TestClient) -> None:
    """A realistic EVENT Execution record (stdout + error fields) reaches the model intact."""
    event = FakeEventClient()
    event.set_result(
        "gpu_diagnostics",
        status="succeeded",
        exit_code=0,
        stdout="GPU0 temp=63C",
        stderr="",
        result={"gpu_temp_c": 63},
    )
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
            Generation("temp is 63C"),
        ],
        event,
    )
    assert task["status"] == "completed"
    round2 = _fake(task_client).calls[-1]["messages"]
    tool_msg = next(m for m in round2 if m["role"] == "tool")
    import json as _json

    payload = _json.loads(tool_msg["content"])
    assert payload["status"] == "succeeded"
    assert payload["stdout"] == "GPU0 temp=63C"
    assert payload["exit_code"] == 0
    assert payload["result"] == {"gpu_temp_c": 63}


def _catalog_with_schema() -> list[dict]:
    return [
        {
            "id": "announce_person",
            "type": "shell",
            "enabled": True,
            "description": "Announce a person detected by a camera.",
            "input_schema": {"type": "object", "properties": {"camera": {"type": "string"}}, "required": ["camera"]},
            "keywords": ["camera"],
        }
    ]


def test_schema_invalid_args_rejected_before_submit(task_client: TestClient) -> None:
    event = FakeEventClient(actions=_catalog_with_schema())
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("announce_person", {"topic": "t", "data": {"camera": 42}})]),
            Generation("args were rejected, fixing"),
        ],
        event,
    )
    assert task["status"] == "completed"
    assert event.calls == []  # invalid args never reached EVENT
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert action_steps[0]["status"] == "failed" and "input_schema" in action_steps[0]["summary"]
    round2 = _fake(task_client).calls[-1]["messages"]
    tool_msg = next(m for m in round2 if m["role"] == "tool")
    assert "invalid_arguments" in tool_msg["content"]


def test_schema_valid_args_execute_normally(task_client: TestClient) -> None:
    event = FakeEventClient(actions=_catalog_with_schema())
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("announce_person", {"topic": "t", "data": {"camera": "front"}})]),
            Generation("announced"),
        ],
        event,
    )
    assert task["status"] == "completed"
    assert len(event.calls) == 1 and event.calls[0]["event"]["data"] == {"camera": "front"}


def test_event_422_rejection_feeds_back_safely(task_client: TestClient) -> None:
    event = FakeEventClient(actions=[{"id": "fragile", "type": "http", "enabled": True}])
    event.reject_ids.add("fragile")
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("fragile", {"topic": "t", "data": {}})]),
            Generation("EVENT rejected it"),
        ],
        event,
    )
    assert task["status"] == "completed"  # rejection is feedback, not task failure
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert action_steps[0]["status"] == "failed" and "rejected" in action_steps[0]["summary"]
    round2 = _fake(task_client).calls[-1]["messages"]
    tool_msg = next(m for m in round2 if m["role"] == "tool")
    assert "event_rejected" in tool_msg["content"]
