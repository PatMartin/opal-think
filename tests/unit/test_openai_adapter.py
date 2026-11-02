"""Unit tests: OpenAI adapter generate/stream/tool-call mapping (mocked transport)."""

from __future__ import annotations

import json

import httpx
import pytest

from opal_think.providers import ProviderUnavailable
from opal_think.providers.openai import OpenAIAdapter


def _adapter(handler, credential_ref=None):
    return OpenAIAdapter("openai", "http://api.test", credential_ref, transport=httpx.MockTransport(handler))


async def test_generate_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.read().decode())
        assert body["model"] == "gpt-x" and body["messages"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 7},
            },
        )

    result = await _adapter(handler).generate("gpt-x", [{"role": "user", "content": "say hi"}])
    assert result.text == "hi there"
    assert result.finish_reason == "stop"
    assert result.usage == {"total_tokens": 7}


async def test_generate_tool_calls_and_call_id() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {"name": "gpu_diagnostics", "arguments": '{"topic":"t","data":{"x":1}}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    adapter = _adapter(handler)
    tools = [{"type": "function", "function": {"name": "gpu_diagnostics", "parameters": {}}}]
    result = await adapter.generate("gpt-x", [{"role": "user", "content": "run diag"}], tools=tools)
    assert seen["body"]["tools"] == tools
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.action_id == "gpu_diagnostics"
    assert call.event == {"topic": "t", "data": {"x": 1}}
    assert call.call_id == "call_abc"


async def test_generate_auth_header(monkeypatch) -> None:
    monkeypatch.setenv("OA_KEY", "sk-test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]})

    await _adapter(handler, credential_ref="env:OA_KEY").generate("m", [{"role": "user", "content": "hi"}])
    assert seen["auth"] == "Bearer sk-test"


async def test_generate_error() -> None:
    adapter = _adapter(lambda r: httpx.Response(429, json={"error": {"message": "rate"}}))
    with pytest.raises(ProviderUnavailable, match="HTTP 429"):
        await adapter.generate("m", [{"role": "user", "content": "hi"}])


async def test_stream_generate_deltas() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.read().decode())["stream"] is True
        return httpx.Response(200, content=sse.encode(), headers={"Content-Type": "text/event-stream"})

    parts = [delta async for delta in _adapter(handler).stream_generate("m", [{"role": "user", "content": "hi"}])]
    assert parts == ["Hel", "lo"]


async def test_stream_error() -> None:
    adapter = _adapter(lambda r: httpx.Response(401, json={}))
    with pytest.raises(ProviderUnavailable):
        async for _ in adapter.stream_generate("m", [{"role": "user", "content": "hi"}]):
            pass
