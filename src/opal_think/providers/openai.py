"""OpenAI-compatible provider adapter.

Discovery via ``GET /v1/models`` works today; tool-capable generation lands with the
task-engine integration step. Credentials resolve from the configured secret reference
(``env:NAME``); raw keys are never read from settings.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from opal_think.providers import DiscoveredModel, Generation, ProviderUnavailable

_DISCOVERY_TIMEOUT = 5.0


def _resolve_credential(credential_ref: str | None) -> str | None:
    if not credential_ref:
        return None
    name = credential_ref.removeprefix("env:")
    return os.environ.get(name)


class OpenAIAdapter:
    def __init__(
        self,
        provider_id: str,
        base_url: str | None,
        credential_ref: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.type = "openai"
        self._base = (base_url or "https://api.openai.com").rstrip("/")
        self._api_key = _resolve_credential(credential_ref)
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def list_models(self) -> list[DiscoveredModel]:
        try:
            async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT, transport=self._transport) as client:
                response = await client.get(f"{self._base}/v1/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"openai discovery failed: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderUnavailable(f"openai discovery failed: HTTP {response.status_code}")
        return [
            DiscoveredModel(
                name=m["id"],
                available=True,
                streaming=True,
                tool_calling=True,  # chat-completions models generally support tools; refined per-model later
                structured_output=True,
                vision=False,
            )
            for m in response.json().get("data", [])
            if m.get("id")
        ]

    async def show_model(self, name: str) -> DiscoveredModel | None:
        return next((m for m in await self.list_models() if m.name == name), None)

    async def generate(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> Generation:
        raise ProviderUnavailable("openai generation is not yet implemented (lands with the task engine step)")

    async def stream_generate(self, model: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        raise ProviderUnavailable("openai streaming is not yet implemented")
        yield  # pragma: no cover - keep async-generator shape
