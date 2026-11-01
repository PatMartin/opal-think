"""Model discovery endpoints (``/api/v1/models/`` and ``/api/v1/models/{model_id}``).

Read-only catalog across configured providers. Model IDs are opaque ``<provider>:<name>``
tokens (model names may themselves contain ``:`` — split on the first only). Unreachable
providers are omitted; when every enabled provider fails, the catalog is 503.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from opal_think.deps import get_providers
from opal_think.errors import OpalHTTPException
from opal_think.providers import DiscoveredModel, ProviderAdapter, ProviderUnavailable
from opal_think.providers.registry import ProviderRegistry
from opal_think.schemas import Model, ModelCapabilities

router = APIRouter(prefix="/api/v1/models", tags=["models"])

ModelId = Path(min_length=1, max_length=200)


def _to_model(adapter: ProviderAdapter, discovered: DiscoveredModel) -> Model:
    return Model(
        id=f"{adapter.provider_id}:{discovered.name}",
        provider=adapter.provider_id,
        name=discovered.name,
        available=discovered.available,
        capabilities=ModelCapabilities(
            streaming=discovered.streaming,
            tool_calling=discovered.tool_calling,
            structured_output=discovered.structured_output,
            vision=discovered.vision,
        ),
        context_window=discovered.context_window,
        description=discovered.description,
    )


@router.get("/", operation_id="list_models", summary="List available and known models")
async def list_models(registry: ProviderRegistry = Depends(get_providers)) -> list[Model]:
    pairs, failures = await registry.catalog()
    if not pairs and failures:
        raise OpalHTTPException(503, "unavailable", "no configured model provider is reachable")
    return sorted((_to_model(adapter, m) for adapter, m in pairs), key=lambda m: m.id)


@router.get("/{model_id}", operation_id="show_model", summary="Show model")
async def show_model(model_id: str = ModelId, registry: ProviderRegistry = Depends(get_providers)) -> Model:
    provider_id, sep, name = model_id.partition(":")
    if not sep or not name:
        raise OpalHTTPException(404, "not_found", f"model not found: {model_id}")
    adapter = registry.adapter_for(provider_id)
    if adapter is None:
        raise OpalHTTPException(404, "not_found", f"model not found: {model_id}")
    try:
        discovered = await adapter.show_model(name)
    except ProviderUnavailable as exc:
        raise OpalHTTPException(503, "unavailable", f"provider {provider_id} is unreachable") from exc
    if discovered is None:
        raise OpalHTTPException(404, "not_found", f"model not found: {model_id}")
    return _to_model(adapter, discovered)
