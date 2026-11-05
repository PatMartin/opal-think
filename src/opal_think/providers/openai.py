"""OpenAI-compatible provider adapter.

Discovery via ``GET /v1/models`` works today; tool-capable generation lands with the
task-engine integration step. Credentials resolve from the configured secret reference
(``env:NAME``); raw keys are never read from settings.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from opal_think.providers import (
    CapabilityUnsupported,
    DiscoveredModel,
    Generation,
    ProviderUnavailable,
    ToolCallRequest,
)

_DISCOVERY_TIMEOUT = 5.0
_UNSUPPORTED_HINTS = (
    "model_not_found",
    "does not exist",
    "does not support",
    "unknown model",
    "unsupported",
    "invalid model",
    "invalid_model",
)


def _resolve_credential(credential_ref: str | None) -> str | None:
    if not credential_ref:
        return None
    name = credential_ref.removeprefix("env:")
    return os.environ.get(name)


def _chat_error(response: httpx.Response) -> Exception:
    """Classify a failed chat-completions call: unsupported capability vs outage.

    A listed model may still not support chat completions or tools: 404 (unknown
    model/endpoint) and 400 bodies that explicitly report an unknown/unsupported
    model or feature raise :class:`CapabilityUnsupported`. Everything else —
    401/429/5xx, transport errors — is :class:`ProviderUnavailable`. The provider's
    error detail is preserved (bounded) so the distinction stays auditable.
    """
    detail = ""
    try:
        error = (response.json() or {}).get("error") or {}
        detail = " ".join(str(error.get(k) or "") for k in ("code", "type", "message")).strip()
    except ValueError:
        detail = response.text
    message = f"openai chat failed: HTTP {response.status_code} {detail[:300]}".strip()
    if response.status_code == 404 or (
        response.status_code == 400 and any(hint in detail.lower() for hint in _UNSUPPORTED_HINTS)
    ):
        return CapabilityUnsupported(message)
    return ProviderUnavailable(message)


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
        # /v1/models reports names only (includes embeddings/moderation models) — do not
        # advertise tools/structured output we cannot verify; explicitly requested models
        # still attempt the calls and surface provider errors honestly.
        return [
            DiscoveredModel(
                name=m["id"],
                available=True,
                streaming=True,
                tool_calling=False,
                structured_output=False,
                vision=False,
                capabilities_verified=False,
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
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=120.0, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base}/v1/chat/completions", headers=self._headers(), json=payload
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"openai chat failed: {exc}") from exc
        if response.status_code >= 400:
            raise _chat_error(response)
        data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderUnavailable("openai chat returned malformed response: missing choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderUnavailable("openai chat returned malformed response: missing message")
        tool_calls = [
            ToolCallRequest(
                action_id=(call.get("function") or {}).get("name", "unknown"),
                event=_parse_args((call.get("function") or {}).get("arguments")),
                call_id=call.get("id"),
            )
            for call in message.get("tool_calls") or []
        ]
        return Generation(
            text=message.get("content") or message.get("refusal") or "",
            tool_calls=tool_calls,
            finish_reason=choices[0].get("finish_reason"),
            usage=data.get("usage") or {},
        )

    async def stream_generate(self, model: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        payload = {"model": model, "messages": messages, "stream": True}
        try:
            async with (
                httpx.AsyncClient(timeout=120.0, transport=self._transport) as client,
                client.stream("POST", f"{self._base}/v1/chat/completions", headers=self._headers(), json=payload) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()  # streaming bodies are unread until consumed
                    raise _chat_error(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    for choice in json.loads(data).get("choices") or []:
                        if content := (choice.get("delta") or {}).get("content"):
                            yield content
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"openai chat failed: {exc}") from exc


    def tool_call_message(self, generation: Generation, calls: list[ToolCallRequest]) -> dict[str, Any]:
        """OpenAI assistant turn: tool_calls entries with ids and JSON-string arguments."""
        return {
            "role": "assistant",
            "content": generation.text or None,
            "tool_calls": [
                {
                    "id": c.call_id,
                    "type": "function",
                    "function": {"name": c.action_id, "arguments": json.dumps(c.event)},
                }
                for c in calls
            ],
        }

    def tool_result_message(self, call: ToolCallRequest, content: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call.call_id, "content": content}


def _parse_args(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"data": raw}
