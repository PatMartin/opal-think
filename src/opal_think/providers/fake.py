"""Deterministic fake provider for tests, evaluation fixtures, and offline demos.

``fake:echo`` echoes the last user message; ``fake:script`` plays a scripted sequence of
``Generation`` responses injected by tests via :meth:`set_script` — enough to drive the
task engine, tool loop, and eval harness deterministically without a live model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from opal_think.providers import DiscoveredModel, Generation, ProviderUnavailable

_FAKE_MODELS = (
    DiscoveredModel(
        name="echo",
        available=True,
        streaming=True,
        tool_calling=True,
        structured_output=True,
        vision=False,
        context_window=32768,
        description="Deterministic echo model for tests",
    ),
    DiscoveredModel(
        name="script",
        available=True,
        streaming=False,  # scripted generations may carry tool calls; stream is text-only
        tool_calling=True,
        structured_output=True,
        vision=False,
        context_window=32768,
        description="Scripted model for deterministic evaluations",
    ),
)


class FakeAdapter:
    def __init__(self, provider_id: str, delay: float = 0.0) -> None:
        self.provider_id = provider_id
        self.type = "fake"
        self.delay = delay
        self.script: list[Generation] = []
        self.calls: list[dict[str, Any]] = []

    def set_script(self, generations: list[Generation]) -> None:
        self.script = list(generations)

    async def list_models(self) -> list[DiscoveredModel]:
        return list(_FAKE_MODELS)

    async def show_model(self, name: str) -> DiscoveredModel | None:
        return next((m for m in _FAKE_MODELS if m.name == name), None)

    async def generate(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> Generation:
        import asyncio

        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        if model == "script":
            if not self.script:
                raise ProviderUnavailable("fake:script has no scripted responses left")
            return self.script.pop(0)
        last = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        return Generation(text=f"echo: {last}", finish_reason="stop")

    async def stream_generate(self, model: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        result = await self.generate(model, messages)
        words = result.text.split(" ")
        for i, word in enumerate(words):
            yield word + (" " if i < len(words) - 1 else "")
