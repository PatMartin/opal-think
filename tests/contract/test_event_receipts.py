"""Contract tests: durable EVENT receipts survive interruption.

The execution_id + submitted state persist to the task record BEFORE any progress
event or poll, then update in place to the terminal outcome — one action call is
one step record. Submissions carry a stable caller-scoped ``Idempotency-Key``
(EVENT 1.7.1): a retry of the same logical call returns the original receipt, so
a lost admission response can never produce a second execution.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from opal_think.providers import Generation, ToolCallRequest
from tests.mock_http_event import MockEventApi


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _fake(client: TestClient):
    return next(a for a in client.app.state.providers.adapters() if a.provider_id == "fake")


def _wire(client: TestClient, api: MockEventApi) -> None:
    client.app.state.event_client = api.client()
    client.app.state.settings.store.set_many({"event.action_allowlist": [a["id"] for a in api.actions]})


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 8.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _post_scripted(client: TestClient, script: list[Generation]) -> str:
    _fake(client).set_script(script)
    return client.post("/api/v1/task/", json={"prompt": "act", "model_id": "fake:script"}).json()["id"]


def _wait_for_execution(api: MockEventApi, seconds: float = 5.0) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if api.executions:
            return next(iter(api.executions))
        time.sleep(0.02)
    raise AssertionError("no EVENT execution was admitted")


def _action_steps(task: dict) -> list[dict]:
    return [s for s in task["steps"] if s["kind"] == "action"]


def test_receipt_persisted_before_poll_then_completed(task_client: TestClient) -> None:
    """Mid-poll, the task record already carries execution_id + submitted status;
    the same step record is updated in place at terminal — still one step."""
    api = MockEventApi()
    api.set_pending("gpu_diagnostics", "running", "running", "running", "running")
    _wire(task_client, api)
    task_id = _post_scripted(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
            Generation("done"),
        ],
    )
    execution_id = _wait_for_execution(api)
    # While polling is still in flight the durable receipt is already retrievable.
    mid = task_client.get(f"/api/v1/task/{task_id}").json()
    in_flight = [s for s in mid["steps"] if s.get("execution_id") == execution_id]
    assert in_flight, f"no receipt recorded mid-poll: {mid['steps']}"
    assert in_flight[0]["status"] == "started" and in_flight[0]["kind"] == "action"
    task = _wait_terminal(task_client, task_id)
    assert task["status"] == "completed"
    steps = _action_steps(task)
    assert len(steps) == 1 and steps[0]["execution_id"] == execution_id
    assert steps[0]["status"] == "succeeded"


def test_lost_response_retries_once_single_admission(task_client: TestClient) -> None:
    """Admission committed but the response was lost: the keyed retry returns the
    SAME receipt — one execution, two POSTs, identical keyed payloads."""
    api = MockEventApi()
    api.drop_execute_responses = 1
    _wire(task_client, api)
    task = _wait_terminal(
        task_client,
        _post_scripted(
            task_client,
            [
                Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
                Generation("done"),
            ],
        ),
    )
    assert task["status"] == "completed"
    posts = [r for r in api.requests if r["method"] == "POST" and r["path"].endswith("/execute")]
    assert len(posts) == 2, f"expected exactly one retry, got {len(posts)}"
    keys = {r["headers"].get("idempotency-key") for r in posts}
    assert len(keys) == 1 and None not in keys, f"unstable submission keys: {keys}"
    assert posts[0]["json"] == posts[1]["json"]  # byte-identical payload
    assert len(api.executions) == 1, f"lost response produced {len(api.executions)} admissions"
    step = _action_steps(task)[0]
    assert step["execution_id"] == next(iter(api.executions)) and step["status"] == "succeeded"


def test_4xx_rejection_is_not_retried(task_client: TestClient) -> None:
    """A definitive 4xx (schema/scope) is not ambiguous: no retry, no admission."""
    api = MockEventApi()
    api.set_execute_override("gpu_diagnostics", 422)
    _wire(task_client, api)
    task = _wait_terminal(
        task_client,
        _post_scripted(
            task_client,
            [
                Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
                Generation("rejected"),
            ],
        ),
    )
    assert task["status"] == "completed"
    posts = [r for r in api.requests if r["method"] == "POST" and r["path"].endswith("/execute")]
    assert len(posts) == 1
    assert api.executions == {}
    assert _action_steps(task)[0]["status"] == "failed"


def test_cancel_after_receipt_keeps_receipt(task_client: TestClient) -> None:
    api = MockEventApi()
    api.forced_poll_status = "running"  # poll loop until cancelled
    _wire(task_client, api)
    task_id = _post_scripted(
        task_client,
        [Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})])],
    )
    execution_id = _wait_for_execution(api)
    assert task_client.post(f"/api/v1/task/{task_id}/cancel").status_code == 202
    task = _wait_terminal(task_client, task_id)
    assert task["status"] == "cancelled"
    step = _action_steps(task)[0]
    # The downstream receipt is kept and honestly marked: EVENT-side work continues.
    assert step["execution_id"] == execution_id and step["status"] == "cancelled"
    assert "not cancelled" in step["summary"]


def test_poll_timeout_defined(task_client: TestClient) -> None:
    api = MockEventApi()
    api.forced_poll_status = "running"
    _wire(task_client, api)
    _fake(task_client).set_script(
        [Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})])]
    )
    task_id = task_client.post(
        "/api/v1/task/", json={"prompt": "act", "model_id": "fake:script", "timeout_seconds": 2}
    ).json()["id"]
    task = _wait_terminal(task_client, task_id, seconds=8)
    assert task["status"] == "timed_out"
    step = _action_steps(task)[0]
    assert step["execution_id"] == "exec_1" and step["status"] == "timed_out"
    assert "may still run" in step["summary"]


def test_restart_surfaces_durable_receipt(task_client: TestClient) -> None:
    """Service restart mid-poll: orphan reaping keeps the receipt retrievable in
    the task record (error.event_execution_id + the in-flight step)."""
    api = MockEventApi()
    api.forced_poll_status = "running"
    _wire(task_client, api)
    task_id = _post_scripted(
        task_client,
        [Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})])],
    )
    execution_id = _wait_for_execution(api)
    reaped = task_client.app.state.engine.reap_orphans()
    assert reaped == 1
    task = task_client.get(f"/api/v1/task/{task_id}").json()
    assert task["status"] == "failed" and task["error"]["code"] == "service_restarted"
    # Contract TaskFailure is {code, message}: the receipt is carried by the
    # message text and, structurally, by the retained action step's execution_id.
    assert execution_id in task["error"]["message"] and "not rolled back" in task["error"]["message"]
    step = _action_steps(task)[0]
    assert step["execution_id"] == execution_id and step["status"] == "started"
    # durable store keeps the link as a field too (operator access)
    assert task_client.app.state.store.get_task(task_id)["error"]["event_execution_id"] == execution_id


def test_chain_depth_propagated_on_execute(task_client: TestClient) -> None:
    """The derived action declares its hop depth: task depth + 1."""
    api = MockEventApi()
    _wire(task_client, api)
    parent = task_client.post("/api/v1/task/", json={"prompt": "root"}).json()
    _fake(task_client).set_script(
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
            Generation("done"),
        ]
    )
    child = task_client.post(
        "/api/v1/task/",
        json={"prompt": "act", "model_id": "fake:script"},
        headers={"X-Opal-Causation-Id": parent["id"]},
    )
    task = _wait_terminal(task_client, child.json()["id"])
    assert task["status"] == "completed"
    post = next(r for r in api.requests if r["method"] == "POST" and r["path"].endswith("/execute"))
    assert post["headers"].get("x-opal-chain-depth") == "2"  # child depth 1 -> action hop 2
    assert post["headers"].get("x-opal-causation-id") == task["id"]
