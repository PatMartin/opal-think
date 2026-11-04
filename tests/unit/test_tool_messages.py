"""Unit tests: provider-native tool-call / tool-result message serialization."""

from __future__ import annotations

import json

from opal_think.providers import Generation, ToolCallRequest
from opal_think.providers.fake import FakeAdapter
from opal_think.providers.ollama import OllamaAdapter
from opal_think.providers.openai import OpenAIAdapter

GEN = Generation(text="", tool_calls=[])
CALLS = [
    ToolCallRequest(action_id="gpu_diagnostics", event={"topic": "t", "data": {"x": 1}}, call_id="c1"),
    ToolCallRequest(action_id="read_sensor", event={"topic": "t2", "data": {}}, call_id="c2"),
]


def test_ollama_tool_messages() -> None:
    adapter = OllamaAdapter("ollama", "http://x")
    msg = adapter.tool_call_message(GEN, CALLS)
    assert msg["role"] == "assistant" and msg["content"] == ""
    assert msg["tool_calls"][0]["function"]["name"] == "gpu_diagnostics"
    assert msg["tool_calls"][0]["function"]["arguments"] == {"topic": "t", "data": {"x": 1}}  # dict, not JSON string
    result = adapter.tool_result_message(CALLS[0], "out")
    assert result == {"role": "tool", "name": "gpu_diagnostics", "content": "out"}


def test_openai_tool_messages() -> None:
    adapter = OpenAIAdapter("openai", "http://x", None)
    msg = adapter.tool_call_message(GEN, CALLS)
    assert msg["role"] == "assistant"
    calls = msg["tool_calls"]
    assert calls[0]["id"] == "c1" and calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "gpu_diagnostics"
    assert json.loads(calls[0]["function"]["arguments"]) == {"topic": "t", "data": {"x": 1}}  # JSON string per OpenAI
    result = adapter.tool_result_message(CALLS[0], "out")
    assert result == {"role": "tool", "tool_call_id": "c1", "content": "out"}


def test_fake_tool_messages() -> None:
    adapter = FakeAdapter("fake")
    msg = adapter.tool_call_message(GEN, CALLS)
    assert msg["role"] == "assistant" and len(msg["tool_calls"]) == 2
    assert adapter.tool_result_message(CALLS[1], "r")["name"] == "read_sensor"


def test_tool_call_result_roundtrip_ids() -> None:
    """Each adapter's call/result pair shares one correlation id or name."""
    for adapter in (OllamaAdapter("o", "http://x"), OpenAIAdapter("oa", "http://x", None), FakeAdapter("f")):
        call = CALLS[0]
        msg = adapter.tool_call_message(GEN, [call])
        result = adapter.tool_result_message(call, "x")
        if isinstance(adapter, OpenAIAdapter):
            assert msg["tool_calls"][0]["id"] == result["tool_call_id"]
        else:
            assert msg["tool_calls"][0]["function"]["name"] == result["name"]
