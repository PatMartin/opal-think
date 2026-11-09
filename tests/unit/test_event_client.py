"""Unit tests: HttpEventClient request shape, auth/correlation headers, error mapping, sanitizer."""

from __future__ import annotations

import httpx
import pytest

from opal_think.errors import OpalHTTPException
from opal_think.event_client import EventUnavailable, HttpEventClient, sanitize_action_input

CTX = {"session_id": "s1", "transaction_id": "t1", "causation_id": "task_1"}


def _client(handler, credential_ref=None):
    return HttpEventClient("http://event.test", credential_ref, transport=httpx.MockTransport(handler))


async def test_list_actions_and_headers() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["causation"] = request.headers.get("x-opal-causation-id")
        return httpx.Response(200, json={"items": [{"id": "a1", "enabled": True}]})

    client = _client(handler)
    items = await client.list_actions(CTX)
    assert items == [{"id": "a1", "enabled": True}]
    assert seen["path"] == "/api/v1/action/"
    assert seen["causation"] == "task_1"
    assert seen["auth"] is None


async def test_execute_posts_event_envelope(monkeypatch) -> None:
    monkeypatch.setenv("EVENT_TOK", "tok123")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(202, json={"id": "exec_9", "status": "queued"})

    client = _client(handler, credential_ref="env:EVENT_TOK")
    result = await client.execute("a1", {"topic": "x", "data": {"k": 1}}, CTX)
    assert result["id"] == "exec_9"
    import json

    assert json.loads(seen["body"]) == {"event": {"topic": "x", "data": {"k": 1}}}
    assert seen["auth"] == "Bearer tok123"


async def test_error_mapping() -> None:
    async def call(status: int):
        return await _client(lambda r: httpx.Response(status, json={})).list_actions(CTX)

    with pytest.raises(OpalHTTPException) as not_found:
        await call(404)
    assert not_found.value.status_code == 404
    with pytest.raises(EventUnavailable):
        await call(503)


async def test_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(EventUnavailable):
        await _client(handler).get_execution("exec_1", CTX)


def test_sanitize_action_input() -> None:
    safe = sanitize_action_input({"event": {"data": {"password": "p", "n": 1}, "topic": "t"}})
    assert safe["event"]["data"]["password"] == "***"
    assert safe["event"]["data"]["n"] == 1
    assert sanitize_action_input({"x": "y" * 5000}) is None


def test_normalize_shell_stdout() -> None:
    from opal_think.event_client import normalize_execution_result

    record = {
        "id": "exec_1", "action_id": "gpu_diagnostics", "status": "succeeded",
        "exit_code": 0, "stdout": "GPU0 temp=63C mem=22%", "stderr": "",
        "result": None, "trigger": "manual", "action_revision": "r1",
    }
    out = normalize_execution_result(record)
    assert out["status"] == "succeeded"
    assert out["stdout"] == "GPU0 temp=63C mem=22%"
    assert out["exit_code"] == 0
    assert "result" not in out  # absent fields are not fabricated


def test_normalize_http_result_structured() -> None:
    from opal_think.event_client import normalize_execution_result

    record = {
        "id": "e2", "status": "succeeded", "http_status": 200,
        "result": {"body": {"temp": 63}, "headers": {"x": "y"}},
    }
    out = normalize_execution_result(record)
    assert out["result"] == {"body": {"temp": 63}, "headers": {"x": "y"}}
    assert out["http_status"] == 200


def test_normalize_failed_with_error_fields() -> None:
    from opal_think.event_client import normalize_execution_result

    record = {
        "id": "e3", "status": "failed", "exit_code": 1,
        "stderr": "permission denied", "error_code": "shell_exit",
        "error_message": "process exited 1",
    }
    out = normalize_execution_result(record)
    assert out["status"] == "failed" and out["error_code"] == "shell_exit"
    assert out["error_message"] == "process exited 1" and out["stderr"] == "permission denied"


def test_normalize_dropped_and_timed_out_reasons() -> None:
    from opal_think.event_client import normalize_execution_result

    assert normalize_execution_result({"id": "e4", "status": "dropped"})["reason"].startswith("execution dropped")
    timed = normalize_execution_result({"id": "e5", "status": "timed_out"})
    assert "deadline" in timed["reason"]


def test_normalize_redacts_secrets_and_bounds_size() -> None:
    from opal_think.event_client import normalize_execution_result

    record = {"id": "e6", "status": "succeeded", "result": {"token": "abc123", "temp": 1}, "stdout": "x" * 5000}
    out = normalize_execution_result(record)
    assert out["result"]["token"] == "***"
    assert len(out["stdout"]) < 5000 and out["stdout"].endswith("[truncated]")
    big = normalize_execution_result({"id": "e7", "status": "succeeded", "result": {"blob": "y" * 6000}})
    assert big["result"]["blob"].endswith("[truncated]")  # per-field bound keeps total under the cap
