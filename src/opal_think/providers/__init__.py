"""Model provider adapters: normalized discovery + inference boundary.

A *provider* is a configured settings entry (``providers.<id>.type|base_url|enabled|
credential_ref``). Adapters translate vendor APIs into the contract's portable model:
``Model`` discovery rows and ``Generation`` results. THINK never calls vendor SDKs from
business logic — only through this boundary. Ollama is the primary local provider;
OpenAI is supported; ``fake`` is a deterministic adapter for tests and local demos.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderUnavailable(Exception):
    """The provider endpoint is unreachable, unauthorized, or returned an error."""


class CapabilityUnsupported(Exception):
    """The adapter/model does not support a requested feature (e.g. tool calling)."""


@dataclass
class DiscoveredModel:
    """Provider-reported model facts; the router maps these to contract ``Model`` rows."""

    name: str
    available: bool
    streaming: bool = True
    tool_calling: bool = False
    structured_output: bool = False
    vision: bool = False
    context_window: int | None = None
    description: str | None = None
    # False when caps came from a names-only listing (ollama /api/tags, openai /v1/models):
    # the engine may still *attempt* tools for an explicitly requested model, but default
    # resolution prefers a verified tool-capable model.
    capabilities_verified: bool = True
    # False for models that cannot serve chat completions at all (e.g. Ollama
    # embedding-only models such as all-minilm) — they are never a valid task default.
    generative: bool = True


@dataclass
class ToolCallRequest:
    """A normalized model-proposed tool invocation (validated against EVENT actions later)."""

    action_id: str
    event: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None  # provider call correlation (OpenAI requires it on tool results)


@dataclass
class Generation:
    """Normalized inference result; provider usage/finish metadata when available."""

    text: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    """Uniform boundary every inference provider implements."""

    provider_id: str
    type: str

    async def list_models(self) -> list[DiscoveredModel]:
        """Discover models; raises :class:`ProviderUnavailable` on failure."""
        ...

    async def show_model(self, name: str) -> DiscoveredModel | None:
        """One model's capabilities; None when unknown to the provider."""
        ...

    async def generate(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> Generation:
        """Non-streaming normalized generation. ``tools`` are vendor-native tool specs.

        Raises :class:`ProviderUnavailable` on transport/API failure and
        :class:`CapabilityUnsupported` when a required capability is missing."""
        ...

    def stream_generate(
        self,
        model: str,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[str]:
        """Incremental user-facing text deltas (task.output events); no tool calls.

        Used only for models that report ``capabilities.streaming``; callers must treat
        stream interruptions as recoverable only via the final Task snapshot."""
        ...

    def tool_call_message(self, generation: Generation, calls: list[ToolCallRequest]) -> dict[str, Any]:
        """Provider-native assistant message carrying the requested tool calls.

        Keeps vendor wire shape out of the task engine (OpenAI ``tool_calls`` entries
        with ids vs Ollama-style function calls)."""
        ...

    def tool_result_message(self, call: ToolCallRequest, content: str) -> dict[str, Any]:
        """Provider-native tool-result message matching ``call`` (``tool_call_id`` for
        OpenAI, ``name``+``content`` for Ollama-style)."""
        ...
