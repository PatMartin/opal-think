"""Health, liveness, and readiness endpoints (``/api/v1/health*``).

Readiness requires at least one configured, available model provider (probed live with a
short TTL cache). EVENT availability is a degradation signal and never blocks readiness.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from opal_think import API_VERSION, __version__
from opal_think.deps import get_providers, get_settings, get_store
from opal_think.providers.registry import ProviderRegistry
from opal_think.schemas import Health, HealthProbe
from opal_think.settings_registry import SettingsRegistry
from opal_think.store import ThinkStore

router = APIRouter(prefix="/api/v1/health", tags=["health"])


def _service_status(models_available: int, event_status: str) -> str:
    if models_available <= 0:
        return "unavailable"
    if event_status in {"degraded", "unavailable"}:
        return "degraded"
    return "ok"


@router.get("/", operation_id="opal_health_get", summary="Service health")
async def get_health(
    providers: ProviderRegistry = Depends(get_providers),
    settings: SettingsRegistry = Depends(get_settings),
    store: ThinkStore = Depends(get_store),
) -> Health:
    models_available = await providers.available_model_count()
    event_status = await providers.event_status()
    _, _, pending_restart = settings.snapshot()
    return Health(
        status=_service_status(models_available, event_status),
        ready=models_available > 0,
        models_available=models_available,
        event=event_status,  # type: ignore[arg-type]
        active_tasks=store.count_active_now(),
        pending_restart_fields=pending_restart,
    )


@router.get("/live", operation_id="opal_health_live_get", summary="Liveness probe")
def get_liveness() -> HealthProbe:
    return HealthProbe(status="ok", ready=True, details={"service": "opal-think", "version": __version__})


@router.get(
    "/ready",
    operation_id="opal_health_ready_get",
    summary="Readiness probe",
    response_model=HealthProbe,
    responses={503: {"model": HealthProbe, "description": "No usable model or service cannot accept tasks."}},
)
async def get_readiness(providers: ProviderRegistry = Depends(get_providers)) -> HealthProbe | JSONResponse:
    models_available = await providers.available_model_count()
    if models_available > 0:
        event_status = await providers.event_status()
        return HealthProbe(
            status="degraded" if event_status in {"degraded", "unavailable"} else "ok",
            ready=True,
            details={"models_available": models_available, "api_version": API_VERSION},
        )
    probe = HealthProbe(
        status="unavailable",
        ready=False,
        details={"reason": "no usable model provider is available", "models_available": 0},
    )
    return JSONResponse(status_code=503, content=probe.model_dump(mode="json"))
