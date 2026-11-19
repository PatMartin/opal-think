"""Contract tests: explicit action permission via ``event.action_allowlist``.

Ordinary ask/chat/task traffic is model-only by default — even with an EVENT
client configured, no action may be advertised or invoked until the operator
permits it. A non-empty allowlist permits exactly those action ids, intersected
with what the EVENT credential can see and what is enabled; the boundary is
enforced at tool advertisement AND at execution time.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from opal_think.event_client import FakeEventClient
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


def _allow(client: TestClient, ids: list[str] | None) -> None:
    client.app.state.settings.store.set_many({"event.action_allowlist": ids})


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _script_task(client: TestClient, script: list[Generation]) -> dict:
    _fake(client).set_script(script)
    task_id = client.post(
        "/api/v1/task/", json={"prompt": "diagnose the gpu", "model_id": "fake:script"}
    ).json()["id"]
    return _wait_terminal(client, task_id)


def _round2_tool_msg(client: TestClient) -> dict:
    return next(m for m in _fake(client).calls[-1]["messages"] if m["role"] == "tool")


def test_no_allowlist_is_model_only(task_client: TestClient) -> None:
    """EVENT configured but no permission granted: nothing is advertised, and a
    model that calls anyway is rejected before submission."""
    event = FakeEventClient()
    task_client.app.state.event_client = event
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
            Generation("no actions permitted; answering directly"),
        ],
    )
    assert task["status"] == "completed"
    assert _fake(task_client).calls[0].get("tools") is None  # nothing advertised
    assert event.calls == []  # nothing submitted
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "failed" and step["action_input"] is None
    assert "unknown_or_disabled_action" in _round2_tool_msg(task_client)["content"]


def test_empty_allowlist_is_model_only(task_client: TestClient) -> None:
    """An explicit empty list is a permission denial, not 'permit everything' —
    and discovery is skipped entirely (no catalog fetch)."""
    api = MockEventApi()
    task_client.app.state.event_client = api.client()
    _allow(task_client, [])
    task = _script_task(task_client, [Generation("plain answer")])
    assert task["status"] == "completed"
    assert api.requests == []  # no discovery call was made at all
    assert _fake(task_client).calls[0].get("tools") is None


def test_allowlist_permits_and_advertises(task_client: TestClient) -> None:
    event = FakeEventClient(
        actions=[
            {"id": "gpu_diagnostics", "type": "shell", "enabled": True},
            {"id": "remove_gpu_driver", "type": "shell", "enabled": True},
        ]
    )
    task_client.app.state.event_client = event
    _allow(task_client, ["gpu_diagnostics"])
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("gpu_diagnostics", {"topic": "t", "data": {}})]),
            Generation("diag done"),
        ],
    )
    assert task["status"] == "completed"
    advertised = {t["function"]["name"] for t in _fake(task_client).calls[0]["tools"]}
    assert advertised == {"gpu_diagnostics"}  # non-permitted action never advertised
    hint = next(
        m["content"] for m in _fake(task_client).calls[0]["messages"] if "Permitted actions:" in m["content"]
    )
    assert "gpu_diagnostics" in hint and "remove_gpu_driver" not in hint
    assert [c["action_id"] for c in event.calls] == ["gpu_diagnostics"]
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "completed" and step["execution_id"]


def test_action_outside_allowlist_rejected(task_client: TestClient) -> None:
    """Enabled on EVENT but absent from the allowlist -> rejected at execution,
    never submitted, and never advertised."""
    event = FakeEventClient(
        actions=[
            {"id": "gpu_diagnostics", "type": "shell", "enabled": True},
            {"id": "remove_gpu_driver", "type": "shell", "enabled": True},
        ]
    )
    task_client.app.state.event_client = event
    _allow(task_client, ["gpu_diagnostics"])
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("remove_gpu_driver", {"topic": "gpu.remove", "data": {}})]),
            Generation("that action is not permitted"),
        ],
    )
    assert task["status"] == "completed"
    assert event.calls == []  # never reached EVENT
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "failed" and step["action_id"] == "remove_gpu_driver"
    assert "unknown_or_disabled_action" in _round2_tool_msg(task_client)["content"]


def test_allowlist_composes_with_credential_narrowing(task_client: TestClient) -> None:
    """An allowlisted id the EVENT credential cannot see (absent from the
    discovery projection) stays unadvertised and uninvocable."""
    api = MockEventApi(actions=[{"id": "gpu_diagnostics", "type": "shell", "enabled": True}])
    task_client.app.state.event_client = api.client()
    _allow(task_client, ["gpu_diagnostics", "secret_admin_action"])
    task = _script_task(
        task_client,
        [
            Generation("", tool_calls=[ToolCallRequest("secret_admin_action", {"topic": "t", "data": {}})]),
            Generation("not permitted by the credential"),
        ],
    )
    assert task["status"] == "completed"
    advertised = {t["function"]["name"] for t in _fake(task_client).calls[0]["tools"]}
    assert advertised == {"gpu_diagnostics"}  # allowlisted ∩ credential-visible ∩ enabled
    assert api.calls == []
    step = next(s for s in task["steps"] if s["kind"] == "action")
    assert step["status"] == "failed" and step["action_id"] == "secret_admin_action"


def test_event_down_with_allowlist_degrades_observably(task_client: TestClient) -> None:
    """Tool-dependent workflow + EVENT unreachable: the task still completes
    model-only, but the degraded action policy is recorded on the task."""
    api = MockEventApi(available=False)
    task_client.app.state.event_client = api.client()
    _allow(task_client, ["gpu_diagnostics"])
    task = _script_task(task_client, [Generation("answering without tools")])
    assert task["status"] == "completed" and task["answer"] == "answering without tools"
    assert [r["path"] for r in api.requests] == ["/api/v1/action/"]  # discovery was attempted
    degraded = [
        s for s in task["steps"]
        if s["kind"] == "reasoning" and s["status"] == "failed" and "discovery failed" in s["summary"]
    ]
    assert degraded, f"no degradation marker in steps: {task['steps']}"
    assert _fake(task_client).calls[0].get("tools") is None


def test_allowlist_setting_validation(task_client: TestClient) -> None:
    etag = task_client.get("/api/v1/setting/event.action_allowlist").headers["ETag"]
    assert task_client.get("/api/v1/setting/event.action_allowlist").json()["value"] is None
    bad = task_client.put(
        "/api/v1/setting/event.action_allowlist", json={"value": "gpu_diagnostics"}, headers={"If-Match": etag}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_settings"
    etag = task_client.get("/api/v1/setting/event.action_allowlist").headers["ETag"]
    ok = task_client.put(
        "/api/v1/setting/event.action_allowlist",
        json={"value": ["gpu_diagnostics"]},
        headers={"If-Match": etag},
    )
    assert ok.status_code == 200 and ok.json()["value"] == ["gpu_diagnostics"]
