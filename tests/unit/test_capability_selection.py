"""Unit tests: capability-verified discovery, default model choice, tools gating."""

from __future__ import annotations

import httpx

from opal_think.engine import TaskEngine
from opal_think.event_client import FakeEventClient
from opal_think.providers import DiscoveredModel
from opal_think.providers.ollama import OllamaAdapter
from opal_think.providers.openai import OpenAIAdapter
from opal_think.settings_registry import SettingsRegistry, SettingsStore
from opal_think.store import ThinkStore


def _ollama_transport(models, shows):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": models})
        if request.url.path == "/api/show":
            name = (request.read() and __import__("json").loads(request.read()).get("model")) or ""
            return httpx.Response(200, json=shows.get(name, {}))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class _StubProviders:
    def __init__(self, adapter):
        self._adapter = adapter

    async def catalog(self):
        return [(self._adapter, m) for m in await self._adapter.list_models()], 0

    def adapter_for(self, provider_id):
        return self._adapter if provider_id == self._adapter.provider_id else None


def _engine(tmp_path, adapter, with_event=False) -> TaskEngine:
    engine = TaskEngine(
        ThinkStore(tmp_path / "t.db"),
        SettingsRegistry(SettingsStore(tmp_path / "s.db")),
        _StubProviders(adapter),
    )
    engine.event_client_factory = lambda: FakeEventClient() if with_event else None
    return engine


async def test_default_picks_tool_capable_via_show(tmp_path) -> None:
    """Names-only /api/tags + per-model /api/show: default prefers the model that
    actually reports tools, not the alphabetically-first one."""
    adapter = OllamaAdapter(
        "ollama",
        "http://ollama.test",
        transport=_ollama_transport(
            models=[{"name": "aaa-textonly"}, {"name": "zzz-tools"}],
            shows={
                "aaa-textonly": {"capabilities": []},
                "zzz-tools": {"capabilities": ["tools"]},
            },
        ),
    )
    _, _, resolved, discovered = await _engine(tmp_path, adapter, with_event=True).resolve_model(None)
    assert resolved == "ollama:zzz-tools"
    assert discovered.tool_calling is True


async def test_default_without_event_picks_ollama_first_alphabetical(tmp_path) -> None:
    adapter = OllamaAdapter(
        "ollama",
        "http://ollama.test",
        transport=_ollama_transport(models=[{"name": "bbb"}, {"name": "aaa"}], shows={}),
    )
    _, _, resolved, _ = await _engine(tmp_path, adapter, with_event=False).resolve_model(None)
    assert resolved == "ollama:aaa"  # no EVENT -> first generative candidate wins


async def test_default_skips_embedding_only_model(tmp_path) -> None:
    """all-minilm-style embedding models advertise no 'completion' capability and must
    never become the task default — alphabetically first or not."""
    adapter = OllamaAdapter(
        "ollama",
        "http://ollama.test",
        transport=_ollama_transport(
            models=[{"name": "all-minilm:22m"}, {"name": "qwen2.5-coder:7b"}],
            shows={
                "all-minilm:22m": {"capabilities": ["embedding"], "details": {"families": ["bert"]}},
                "qwen2.5-coder:7b": {"capabilities": ["completion", "tools"]},
            },
        ),
    )
    _, _, resolved, discovered = await _engine(tmp_path, adapter, with_event=False).resolve_model(None)
    assert resolved == "ollama:qwen2.5-coder:7b"
    assert discovered.generative is True


async def test_ollama_show_marks_embedding_model_non_generative() -> None:
    adapter = OllamaAdapter(
        "ollama",
        "http://ollama.test",
        transport=_ollama_transport(
            models=[{"name": "all-minilm:22m"}],
            shows={"all-minilm:22m": {"capabilities": ["embedding"], "details": {"families": ["bert"]}}},
        ),
    )
    model = await adapter.show_model("all-minilm:22m")
    assert model.generative is False
    assert model.streaming is False and model.tool_calling is False and model.structured_output is False


async def test_tools_allowed_gate() -> None:
    verified_tools = DiscoveredModel(name="m", available=True, tool_calling=True)
    verified_plain = DiscoveredModel(name="m", available=True, tool_calling=False)
    unverified = DiscoveredModel(name="m", available=True, capabilities_verified=False)
    assert TaskEngine._tools_allowed(verified_tools) is True
    assert TaskEngine._tools_allowed(verified_plain) is False  # verified incapable -> no attempt
    assert TaskEngine._tools_allowed(unverified) is True  # unknown -> attempt, provider decides


async def test_openai_catalog_is_honest() -> None:
    adapter = OpenAIAdapter(
        "openai",
        "http://api.test",
        None,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"id": "gpt-x"}, {"id": "text-embedding-3"}]})
        ),
    )
    models = await adapter.list_models()
    assert all(m.tool_calling is False for m in models)
    assert all(m.structured_output is False for m in models)
    assert all(m.capabilities_verified is False for m in models)


async def test_ollama_catalog_names_only_unverified() -> None:
    adapter = OllamaAdapter(
        "ollama",
        "http://ollama.test",
        transport=_ollama_transport(models=[{"name": "m1"}], shows={}),
    )
    models = await adapter.list_models()
    assert models[0].capabilities_verified is False and models[0].tool_calling is False
