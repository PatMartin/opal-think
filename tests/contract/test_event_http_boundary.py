"""Contract tests: EVENT exercised through the real HTTP adapter boundary.

Unlike ``test_event_integration.py`` — which injects ``FakeEventClient`` at
``app.state.event_client`` — every case here drives::

    POST /api/v1/task/  ->  engine  ->  HttpEventClient  ->  httpx.MockTransport
        ->  tests.mock_http_event.MockEventApi  (released opal_event.1.4.0 surface)

so request shape, correlation/auth headers, admission->poll sequencing, error
mapping, and protocol-violation handling are all verified at the boundary THINK
will actually cross in production. The mock transport is in-process: no real
network, shell, or subprocess side effects are possible.
"""

from __future__ import annotations

import json
import time

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient

from opal_think.providers import Generation, ToolCallRequest
from tests.mock_http_event import MockEventApi

# Parity constants derived from released opal_event.1.4.0.yaml:
#   Execution.required = [id, action_id, status, trigger, created_at]
#   Execution.status enum = queued | running | succeeded | failed | timed_out | dropped
#   ListActions.required = [items, next_cursor]
#   OpalError.error.required = [code, message, request_id, transaction_id]
EXECUTION_SCHEMA = {
    "type": "object",
    "required": ["id", "action_id", "status", "trigger", "created_at"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "action_id": {"type": "string", "minLength": 1},
        "status": {"enum": ["queued", "running", "succeeded", "failed", "timed_out", "dropped"]},
        "trigger": {"enum": ["rule", "manual"]},
        "created_at": {"type": "string"},
    },
}
OPAL_ERROR_REQUIRED = {"code", "message", "request_id", "transaction_id"}


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _fake(task_client: TestClient):
    return next(a for a in task_client.app.state.providers.adapters() if a.provider_id == "fake")


def _wire_event(task_client: TestClient, api: MockEventApi, credential_ref: str | None = None) -> None:
    task_client.app.state.event_client = api.client(credential_ref)


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _script_task(client: TestClient, script: list[Generation], headers: dict | None = None) -> dict:
    _fake(client).set_script(script)
    task_id = client.post(
        "/api/v1/task/", json={"prompt": "diagnose the gpu", "model_id": "fake:script"}, headers=headers or {}
    ).json()["id"]
    return _wait_terminal(client, task_id)


def _tool_round2(client: TestClient) -> list[dict]:
    return _fake(client).calls[-1]["messages"]


def _tool_msg(client: TestClient) -> dict:
    return next(m for m in _tool_round2(client) if m["role"] == "tool")


def _requests(api: MockEventApi, method: str, prefix: str) -> list[dict]:
    return [r for r in api.requests if r["method"] == method and r["path"].startswith(prefix)]


def _diag_call(*, topic: str = "gpu.diag", data: dict | None = None) -> ToolCallRequest:
    return ToolCallRequest("gpu_diagnostics", {"topic": topic, "data": data if data is not None else {}})


def test_round_trip_over_http_boundary(task_client: TestClient) -> None:
    """202 admission -> poll queued->running->succeeded; stdout/result reach model turn 2."""
    api = MockEventApi()
    api.set_terminal(
        "gpu_diagnostics",
        status="succeeded",
        exit_code=0,
        stdout="GPU0 temp=63C",
        stderr="",
        result={"gpu_temp_c": 63},
    )
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[_diag_call(data={"level": 1})]),
            Generation("GPU temperature is 63C"),
        ],
    )
    assert task["status"] == "completed" and task["answer"] == "GPU temperature is 63C"
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "succeeded" and step["execution_id"] == "exec_1"
    # the wire exchange actually happened: list -> execute -> polls to terminal
    assert [r["path"] for r in api.requests] == [
        "/api/v1/action/",
        "/api/v1/action/gpu_diagnostics/execute",
        "/api/v1/execution/exec_1",
        "/api/v1/execution/exec_1",
    ]
    post = _requests(api, "POST", "/api/v1/action/")[0]
    assert post["json"] == {"event": {"topic": "gpu.diag", "data": {"level": 1}}}
    payload = json.loads(_tool_msg(task_client)["content"])
    assert payload["status"] == "succeeded"
    assert payload["stdout"] == "GPU0 temp=63C"
    assert payload["exit_code"] == 0
    assert payload["result"] == {"gpu_temp_c": 63}


def test_fixture_parity_with_released_event_contract(task_client: TestClient) -> None:
    """The fixture's actual wire payloads validate against released-contract invariants."""
    assert MockEventApi.provenance == "mock_http_event"
    api = MockEventApi()
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [Generation("", tool_calls=[_diag_call()]), Generation("done")],
    )
    assert task["status"] == "completed"
    list_actions, admission, *polls = api.requests
    # ListActions wire body (required: items, next_cursor)
    body = list_actions["response_json"]
    assert set(body) == {"items", "next_cursor"} and isinstance(body["items"], list)
    assert all({"id", "type", "enabled"} <= set(a) for a in body["items"])
    # ExecutionRequest wire body and 202 Execution admission record
    assert set(admission["json"]) == {"event"}
    assert admission["status"] == 202
    jsonschema.validate(admission["response_json"], EXECUTION_SCHEMA)
    assert admission["response_json"]["status"] == "queued"
    # every poll returned a valid Execution record ending in a terminal status
    assert len(polls) >= 2
    for poll in polls:
        assert poll["status"] == 200
        jsonschema.validate(poll["response_json"], EXECUTION_SCHEMA)
    assert polls[-1]["response_json"]["status"] in {"succeeded", "failed", "timed_out", "dropped"}
    # error path emits the OpalError envelope
    error = api.handler(httpx.Request("POST", "http://event.test/api/v1/action/nope/execute", json={"event": {}}))
    assert error.status_code == 404
    assert set(error.json()["error"]) >= OPAL_ERROR_REQUIRED


def test_correlation_and_bearer_headers_flow(task_client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("EVENT_TEST_TOKEN", "tok-abc")
    api = MockEventApi()
    _wire_event(task_client, api, credential_ref="env:EVENT_TEST_TOKEN")
    task = _script_task(
        task_client,
        [Generation("", tool_calls=[_diag_call()]), Generation("done")],
        headers={"X-Opal-Session-Id": "sess-1", "X-Opal-Transaction-Id": "txn-1"},
    )
    assert task["status"] == "completed"
    assert len(api.requests) >= 3
    for request in api.requests:
        headers = request["headers"]
        assert headers["authorization"] == "Bearer tok-abc"
        assert headers["x-opal-session-id"] == "sess-1"
        assert headers["x-opal-transaction-id"] == "txn-1"
        assert headers["x-opal-causation-id"] == task["id"]


def test_client_side_schema_check_never_reaches_wire(task_client: TestClient) -> None:
    api = MockEventApi(
        actions=[
            {
                "id": "gpu_diagnostics",
                "type": "shell",
                "enabled": True,
                "input_schema": {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]},
            }
        ]
    )
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[_diag_call(data={"level": "high"})]),
            Generation("arguments were invalid"),
        ],
    )
    assert task["status"] == "completed"
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "failed" and "input_schema" in step["summary"]
    assert _requests(api, "POST", "/api/v1/action/") == []  # rejected before the wire
    assert "invalid_arguments" in _tool_msg(task_client)["content"]


def test_server_side_rejection_422_flows_back(task_client: TestClient) -> None:
    """A contract-level envelope violation THINK does not pre-check gets a real 422."""
    api = MockEventApi()
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[_diag_call(topic="Bad Topic!")]),
            Generation("EVENT rejected the envelope"),
        ],
    )
    assert task["status"] == "completed"  # rejection is feedback, not task failure
    assert len(_requests(api, "POST", "/api/v1/action/")) == 1  # it did reach the wire
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "failed" and "validation_error" in step["summary"]
    content = _tool_msg(task_client)["content"]
    assert "event_rejected" in content and "malformed" in content  # EVENT's envelope message surfaced


def test_event_unavailable_at_discovery_degrades_to_model_only(task_client: TestClient) -> None:
    api = MockEventApi(available=False)
    _wire_event(task_client, api)
    task = _script_task(task_client, [Generation("answering without tools")])
    assert task["status"] == "completed" and task["answer"] == "answering without tools"
    assert [r["path"] for r in api.requests] == ["/api/v1/action/"]  # discovery tried once
    assert _fake(task_client).calls[-1].get("tools") is None  # no tools offered


def test_event_unavailable_at_execute_fails_task_cleanly(task_client: TestClient) -> None:
    api = MockEventApi()
    api.set_execute_override("gpu_diagnostics", 503)
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [Generation("", tool_calls=[_diag_call()]), Generation("unreachable")],
    )
    assert task["status"] == "failed"
    assert task["error"]["code"] == "event_unavailable"  # not an internal error


@pytest.mark.parametrize("payload", [{}, [], {"id": None}, {"status": "queued"}])
def test_malformed_admission_fails_step_not_task(task_client: TestClient, payload) -> None:
    api = MockEventApi()
    api.set_execute_override("gpu_diagnostics", payload)
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [Generation("", tool_calls=[_diag_call()]), Generation("EVENT misbehaved; reporting honestly")],
    )
    assert task["status"] == "completed"
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "failed" and "execution id" in step["summary"]
    assert "event_protocol_error" in _tool_msg(task_client)["content"]
    assert _requests(api, "GET", "/api/v1/execution/") == []  # nothing to poll


def test_transient_poll_failure_fails_task_cleanly(task_client: TestClient) -> None:
    api = MockEventApi()
    api.poll_failures = 1  # first poll gets a real 503
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [Generation("", tool_calls=[_diag_call()]), Generation("unreachable")],
    )
    assert task["status"] == "failed"
    assert task["error"]["code"] == "event_unavailable"
    assert len(_requests(api, "GET", "/api/v1/execution/")) == 1


def test_unknown_execution_status_fails_step_fast(task_client: TestClient) -> None:
    api = MockEventApi()
    api.forced_poll_status = "rebooting"  # outside the released Execution enum
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [Generation("", tool_calls=[_diag_call()]), Generation("EVENT spoke nonsense")],
    )
    assert task["status"] == "completed"
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "failed" and "unrecognized execution status" in step["summary"]
    assert "event_protocol_error" in _tool_msg(task_client)["content"]
    assert len(_requests(api, "GET", "/api/v1/execution/")) == 1  # failed fast, no poll spin


def test_exchange_confined_to_documented_event_surface(task_client: TestClient) -> None:
    """No request leaves the actions/executions contract surface; nothing else exists."""
    api = MockEventApi()
    _wire_event(task_client, api)
    task = _script_task(
        task_client,
        [Generation("", tool_calls=[_diag_call()]), Generation("done")],
    )
    assert task["status"] == "completed"
    assert api.requests
    for request in api.requests:
        assert request["path"].startswith(("/api/v1/action/", "/api/v1/execution/"))
        if request["method"] == "GET":
            assert request["json"] is None
