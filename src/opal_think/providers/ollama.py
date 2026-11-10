"""Ollama provider adapter — the primary local-inference target.

Discovery: ``GET /api/tags``; capabilities: ``POST /api/show``; inference:
``POST /api/chat`` (single or streamed). Tool calling uses Ollama's native
``tools`` parameter; model-declared capabilities are never assumed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from opal_think.providers import DiscoveredModel, Generation, ProviderUnavailable, ToolCallRequest

_DISCOVERY_TIMEOUT = 5.0
_GENERATE_TIMEOUT = 600.0


class OllamaAdapter:
    def __init__(self, provider_id: str, base_url: str | None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.provider_id = provider_id
        self.type = "ollama"
        self._base = (base_url or "http://127.0.0.1:11434").rstrip("/")
        self._transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def list_models(self) -> list[DiscoveredModel]:
        try:
            async with self._client(_DISCOVERY_TIMEOUT) as client:
                response = await client.get(f"{self._base}/api/tags")
                response.raise_for_status()
                models = response.json().get("models", [])
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"ollama discovery failed: {exc}") from exc
        # /api/tags is names-only: capabilities are unknown until /api/show per model.
        return [
            DiscoveredModel(name=m["name"], available=True, capabilities_verified=False)
            for m in models
            if m.get("name")
        ]

    async def show_model(self, name: str) -> DiscoveredModel | None:
        try:
            async with self._client(_DISCOVERY_TIMEOUT) as client:
                response = await client.post(f"{self._base}/api/show", json={"model": name})
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"ollama show failed: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ProviderUnavailable(f"ollama show failed: HTTP {response.status_code}")
        payload = response.json()
        capabilities = set(payload.get("capabilities") or [])
        # Embedding-only models (e.g. all-minilm) advertise "embedding" and no "completion";
        # they cannot serve /api/chat, so they are non-generative regardless of other flags.
        generative = "completion" in capabilities or "embedding" not in capabilities
        info = payload.get("model_info") or {}
        context_window = next((v for k, v in info.items() if k.endswith("context_length") and isinstance(v, int)), None)
        details = payload.get("details") or {}
        description = ", ".join(filter(None, [details.get("parameter_size"), details.get("quantization_level")])) or None
        families = set(details.get("families") or [])
        return DiscoveredModel(
            name=name,
            available=True,
            generative=generative,
            streaming=generative,
            tool_calling="tools" in capabilities,
            structured_output=generative,  # ollama supports the `format` constraint broadly
            vision="vision" in capabilities or bool(families & {"clip", "mllama"}),
            context_window=context_window,
            description=description,
        )

    async def generate(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> Generation:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        try:
            async with self._client(_GENERATE_TIMEOUT) as client:
                response = await client.post(f"{self._base}/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"ollama generate failed: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderUnavailable(f"ollama generate failed: HTTP {response.status_code} {response.text[:200]}")
        payload = response.json()
        message = payload.get("message") or {}
        tool_calls = [
            ToolCallRequest(action_id=call["function"]["name"], event=dict(call["function"].get("arguments") or {}))
            for call in message.get("tool_calls") or []
            if call.get("function", {}).get("name")
        ]
        return Generation(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=payload.get("done_reason"),
            usage={k: payload[k] for k in ("prompt_eval_count", "eval_count", "total_duration") if k in payload},
        )

    async def stream_generate(self, model: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        body = {"model": model, "messages": messages, "stream": True}
        try:
            async with self._client(_GENERATE_TIMEOUT) as client, client.stream(
                "POST", f"{self._base}/api/chat", json=body
            ) as response:
                    if response.status_code >= 400:
                        raise ProviderUnavailable(f"ollama stream failed: HTTP {response.status_code}")
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if delta := (chunk.get("message") or {}).get("content"):
                            yield delta
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"ollama stream failed: {exc}") from exc

    def tool_call_message(self, generation: Generation, calls: list[ToolCallRequest]) -> dict[str, Any]:
        """Ollama-style assistant turn carrying function calls."""
        return {
            "role": "assistant",
            "content": generation.text or "",
            "tool_calls": [
                {"type": "function", "function": {"name": c.action_id, "arguments": c.event}}
                for c in calls
            ],
        }

    def tool_result_message(self, call: ToolCallRequest, content: str) -> dict[str, Any]:
        return {"role": "tool", "name": call.action_id, "content": content}
