"""Provider registry: builds adapters from settings and answers catalog questions.

Adapters are constructed per call from live settings (they hold no connections), so
``settings set providers.*`` takes effect immediately. Discovery runs adapters
concurrently; a provider that fails contributes nothing to the catalog — model entries
for undiscoverable providers are omitted rather than fabricated.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from opal_think.providers import DiscoveredModel, ProviderAdapter
from opal_think.providers.fake import FakeAdapter
from opal_think.providers.ollama import OllamaAdapter
from opal_think.providers.openai import OpenAIAdapter
from opal_think.settings_registry import SettingsRegistry

_HEALTH_TTL_SECONDS = 10.0


def provider_configs(registry: SettingsRegistry) -> dict[str, dict[str, Any]]:
    """Current ``providers.<id>.*`` values grouped by provider id."""
    items, _, _ = registry.snapshot()
    configs: dict[str, dict[str, Any]] = {}
    for setting in items:
        if not setting["key"].startswith("providers."):
            continue
        _, provider_id, field_name = setting["key"].split(".", 2)
        configs.setdefault(provider_id, {})[field_name] = setting["value"]
    return configs


class ProviderRegistry:
    def __init__(self, settings: SettingsRegistry) -> None:
        self._settings = settings
        self._health_cache: tuple[float, int] | None = None
        self._event_cache: tuple[float, str] | None = None

    def adapters(self) -> list[ProviderAdapter]:
        """Enabled providers as fresh adapters (cheap; no connections held)."""
        adapters: list[ProviderAdapter] = []
        for provider_id, cfg in provider_configs(self._settings).items():
            if not cfg.get("enabled"):
                continue
            provider_type = cfg.get("type")
            if provider_type == "ollama":
                adapters.append(OllamaAdapter(provider_id, cfg.get("base_url")))
            elif provider_type == "openai":
                adapters.append(OpenAIAdapter(provider_id, cfg.get("base_url"), cfg.get("credential_ref")))
            elif provider_type == "fake":
                adapters.append(FakeAdapter(provider_id))
        return adapters

    def adapter_for(self, provider_id: str) -> ProviderAdapter | None:
        return next((a for a in self.adapters() if a.provider_id == provider_id), None)

    async def catalog(self) -> tuple[list[tuple[ProviderAdapter, DiscoveredModel]], int]:
        """(adapter, model) pairs across enabled providers, plus the count of failed providers."""
        adapters = self.adapters()
        results = await asyncio.gather(*(a.list_models() for a in adapters), return_exceptions=True)
        pairs: list[tuple[ProviderAdapter, DiscoveredModel]] = []
        failures = 0
        for adapter, result in zip(adapters, results, strict=True):
            if isinstance(result, Exception):
                failures += 1
            else:
                pairs.extend((adapter, m) for m in result)
        return pairs, failures

    async def available_model_count(self) -> int:
        """Models usable right now; cached briefly so /health stays cheap."""
        if self._health_cache and time.monotonic() - self._health_cache[0] < _HEALTH_TTL_SECONDS:
            return self._health_cache[1]
        pairs, _ = await self.catalog()
        count = sum(1 for _, m in pairs if m.available)
        self._health_cache = (time.monotonic(), count)
        return count

    async def event_status(self) -> str:
        """OPAL-EVENT reachability for the health surface (not a capability gate)."""
        base_url = self._settings.get("event.base_url")["value"]
        if not base_url:
            return "not_configured"
        if self._event_cache and time.monotonic() - self._event_cache[0] < _HEALTH_TTL_SECONDS:
            return self._event_cache[1]
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{str(base_url).rstrip('/')}/api/v1/health/")
            status = "available" if response.status_code == 200 else "degraded"
        except httpx.HTTPError:
            status = "unavailable"
        self._event_cache = (time.monotonic(), status)
        return status
