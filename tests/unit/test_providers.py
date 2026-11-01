"""Unit tests for provider adapters and the registry (mocked transports, no live services)."""

from __future__ import annotations

import json

import httpx

from opal_think.providers import Generation, ToolCallRequest
from opal_think.providers.fake import FakeAdapter
from opal_think.providers.ollama import OllamaAdapter
from opal_think.providers.registry import ProviderRegistry, provider_configs
from opal_think.settings_registry import SettingsRegistry, SettingsStore


def _ollama_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}, {"name": "qwen2.5:7b"}]})
    if request.url.path == "/api/show":
        name = json.loads(request.content)["model"]
        if name == "missing":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "capabilities": ["completion", "tools"],
                "model_info": {"llama.context_length": 131072},
                "details": {"parameter_size": "3B", "quantization_level": "Q4_K_M", "families": ["llama"]},
            },
        )
    if request.url.path == "/api/chat":
        body = json.loads(request.content)
        if body.get("stream"):
            lines = [
                json.dumps({"message": {"content": "hello "}, "done": False}),
                json.dumps({"message": {"content": "world"}, "done": True, "done_reason": "stop"}),
            ]
            return httpx.Response(200, text="\n".join(lines) + "\n")
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "gpu_diagnostics", "arguments": {"topic": "gpu.check", "data": {}}}}],
                },
                "done_reason": "stop",
                "eval_count": 9,
            },
        )
    return httpx.Response(404)


def _ollama(provider_id: str = "ollama") -> OllamaAdapter:
    return OllamaAdapter(provider_id, "http://ollama.test", transport=httpx.MockTransport(_ollama_handler))


async def test_ollama_list_models() -> None:
    models = await _ollama().list_models()
    assert [m.name for m in models] == ["llama3.2:latest", "qwen2.5:7b"]


async def test_ollama_show_model_capabilities() -> None:
    model = await _ollama().show_model("llama3.2:latest")
    assert model.tool_calling is True
    assert model.streaming is True and model.vision is False
    assert model.context_window == 131072
    assert "3B" in model.description
    assert await _ollama().show_model("missing") is None


async def test_ollama_generate_tool_call() -> None:
    result = await _ollama().generate("llama3.2", [{"role": "user", "content": "check gpu"}])
    assert result.tool_calls == [ToolCallRequest(action_id="gpu_diagnostics", event={"topic": "gpu.check", "data": {}})]
    assert result.finish_reason == "stop" and result.usage["eval_count"] == 9


async def test_ollama_stream_deltas() -> None:
    chunks = [c async for c in _ollama().stream_generate("m", [{"role": "user", "content": "hi"}])]
    assert "".join(chunks) == "hello world"


async def test_fake_adapter_echo_and_script() -> None:
    fake = FakeAdapter("fake")
    result = await fake.generate("echo", [{"role": "user", "content": "ping"}])
    assert result.text == "echo: ping"
    fake.set_script([Generation(text="one"), Generation(text="two")])
    assert (await fake.generate("script", [])).text == "one"
    assert (await fake.generate("script", [])).text == "two"
    assert fake.calls[0]["model"] == "echo"


def test_provider_configs_grouping(tmp_path) -> None:
    registry = SettingsRegistry(SettingsStore(tmp_path / "t.db"))
    configs = provider_configs(registry)
    assert configs["ollama"]["type"] == "ollama" and configs["ollama"]["enabled"] is True
    assert configs["openai"]["enabled"] is False
    registry.store.set_many({"providers.gpu2.type": "fake", "providers.gpu2.enabled": True})
    assert provider_configs(registry)["gpu2"] == {"type": "fake", "enabled": True}


async def test_registry_catalog_and_health_count(tmp_path) -> None:
    settings = SettingsRegistry(SettingsStore(tmp_path / "t.db"))
    settings.store.set_many(
        {
            "providers.ollama.enabled": False,  # keep the suite deterministic: no live ollama
            "providers.fake.type": "fake",
            "providers.fake.enabled": True,
        }
    )
    registry = ProviderRegistry(settings)
    pairs, failures = await registry.catalog()
    assert failures == 0
    assert {m.name for _, m in pairs} == {"echo", "script"}
    assert await registry.available_model_count() == 2


async def test_registry_event_status(tmp_path) -> None:
    settings = SettingsRegistry(SettingsStore(tmp_path / "t.db"))
    registry = ProviderRegistry(settings)
    assert await registry.event_status() == "not_configured"
    settings.store.set_many({"event.base_url": "http://127.0.0.1:1"})
    assert await registry.event_status() == "unavailable"
