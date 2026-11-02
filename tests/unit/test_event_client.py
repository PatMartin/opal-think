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
    assert seen["path"] == "/api/v1/actions/"
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
