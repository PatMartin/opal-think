"""Unit tests: OpenAI adapter generate/stream/tool-call mapping (mocked transport)."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from opal_think.providers import (
    CapabilityUnsupported,
    DiscoveredModel,
    Generation,
    ProviderUnavailable,
)
from opal_think.providers.fake import FakeAdapter
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


async def test_stream_error_reads_body_for_classification() -> None:
    """Streaming error bodies are unread until consumed; classification must not crash."""
    adapter = _adapter(lambda r: httpx.Response(404, json={"error": {"code": "model_not_found", "message": "gone"}}))
    with pytest.raises(CapabilityUnsupported):
        async for _ in adapter.stream_generate("m", [{"role": "user", "content": "hi"}]):
            pass


# -- two-turn tool-call conversation shape ---------------------------------------------------

_TOOLS = [{"type": "function", "function": {"name": "gpu_diagnostics", "parameters": {"type": "object"}}}]


def _chat_response(message: dict, finish_reason: str = "stop", **extra) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish_reason}], **extra})


async def test_two_turn_tool_conversation_ordering() -> None:
    """Turn-2 request must carry assistant tool_calls then matching tool results, in order."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read().decode()))
        if len(bodies) == 1:
            return _chat_response(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "gpu_diagnostics", "arguments": '{"topic":"gpu.diag","data":{}}'},
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return _chat_response({"content": "The GPU reads 63C."})

    adapter = _adapter(handler)
    messages = [{"role": "user", "content": "check the gpu"}]
    turn1 = await adapter.generate("gpt-x", messages, tools=_TOOLS)
    assert len(turn1.tool_calls) == 1
    # engine flow: assistant tool_calls turn, then one tool result per call
    messages.append(adapter.tool_call_message(turn1, turn1.tool_calls))
    messages.append(adapter.tool_result_message(turn1.tool_calls[0], '{"status":"succeeded","stdout":"GPU0 temp=63C"}'))
    turn2 = await adapter.generate("gpt-x", messages, tools=_TOOLS)
    assert turn2.text == "The GPU reads 63C."
    sent = bodies[1]["messages"]
    assert sent[-2]["role"] == "assistant" and sent[-2]["tool_calls"][0]["id"] == "call_1"
    assert sent[-1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"status":"succeeded","stdout":"GPU0 temp=63C"}'}


async def test_generate_multiple_tool_calls_keep_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "a1", "arguments": "{}"}},
                    {"id": "call_b", "type": "function", "function": {"name": "a2", "arguments": '{"topic":"t"}'}},
                ],
            },
            finish_reason="tool_calls",
        )

    adapter = _adapter(handler)
    calls = (await adapter.generate("gpt-x", [{"role": "user", "content": "two"}], tools=_TOOLS)).tool_calls
    assert [(c.action_id, c.call_id) for c in calls] == [("a1", "call_a"), ("a2", "call_b")]
    results = [adapter.tool_result_message(c, "out") for c in calls]
    assert results[0]["tool_call_id"] == "call_a" and results[1]["tool_call_id"] == "call_b"


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [None]}, {"choices": [{"message": None}]}])
async def test_malformed_success_body_raises_not_empty_answer(payload) -> None:
    """A 200 without a usable choices[0].message is a provider defect, not an empty answer."""
    adapter = _adapter(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ProviderUnavailable, match="malformed"):
        await adapter.generate("gpt-x", [{"role": "user", "content": "hi"}])


async def test_refusal_is_surfaced_as_answer_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response({"content": None, "refusal": "I can't help with that request."})

    result = await _adapter(handler).generate("gpt-x", [{"role": "user", "content": "hack"}])
    assert result.text == "I can't help with that request."


async def test_rate_limit_error_keeps_provider_detail() -> None:
    body = {"error": {"type": "rate_limit_error", "message": "RPM exceeded"}}
    adapter = _adapter(lambda r: httpx.Response(429, json=body))
    with pytest.raises(ProviderUnavailable, match="HTTP 429.*RPM exceeded"):
        await adapter.generate("gpt-x", [{"role": "user", "content": "hi"}])


@pytest.mark.parametrize(
    "status,payload",
    [
        (404, {"error": {"code": "model_not_found", "message": "no such model"}}),
        (400, {"error": {"code": "model_not_found", "message": "The model `x` does not exist"}}),
        (400, {"error": {"type": "invalid_request_error", "message": "model does not support tools"}}),
    ],
)
async def test_unsupported_capability_is_not_an_outage(status, payload) -> None:
    """A listed-but-unsupported model/endpoint is CapabilityUnsupported, never 'unavailable'."""
    adapter = _adapter(lambda r: httpx.Response(status, json=payload))
    with pytest.raises(CapabilityUnsupported):
        await adapter.generate("gpt-x", [{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_remain_outages(status) -> None:
    adapter = _adapter(lambda r: httpx.Response(status, json={"error": {"message": "boom"}}))
    with pytest.raises(ProviderUnavailable):
        await adapter.generate("gpt-x", [{"role": "user", "content": "hi"}])


async def test_generic_400_is_outage_not_capability() -> None:
    """A 400 that says nothing about model/feature support is not miscast as capability."""
    adapter = _adapter(lambda r: httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "bad json"}}))
    with pytest.raises(ProviderUnavailable):
        await adapter.generate("gpt-x", [{"role": "user", "content": "hi"}])


# -- engine mapping: capability failure is a distinct terminal error code ---------------------


class _UnsupportedAdapter(FakeAdapter):
    """Provider stub whose generate reports a capability miss."""

    def __init__(self) -> None:
        super().__init__("stubopenai")

    async def show_model(self, name: str) -> DiscoveredModel:
        return DiscoveredModel(name=name, available=True, streaming=False)

    async def generate(self, model, messages, *, tools=None, stream=False) -> Generation:
        raise CapabilityUnsupported("model does not support chat completions")


def _wait_terminal(client, task_id: str, seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def test_engine_reports_unsupported_capability_not_unavailable(client, monkeypatch) -> None:
    """Through the real REST path, a capability miss fails as unsupported_capability."""
    client.app.state.settings.store.set_many({"providers.ollama.enabled": False})
    stub = _UnsupportedAdapter()
    monkeypatch.setattr(client.app.state.providers, "adapters", lambda: [stub])
    created = client.post("/api/v1/tasks/", json={"prompt": "hi", "model_id": "stubopenai:echo"})
    assert created.status_code == 202, created.text
    task = _wait_terminal(client, created.json()["id"])
    assert task["status"] == "failed"
    assert task["error"]["code"] == "unsupported_capability"
