"""Health, liveness, and readiness endpoints (``/api/v1/health*``).

Readiness requires at least one configured, available model provider. EVENT availability is
reported as a degradation signal and never blocks plain reasoning readiness.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from opal_think import API_VERSION, __version__
from opal_think.deps import RuntimeState, get_runtime
from opal_think.schemas import Health, HealthProbe

router = APIRouter(prefix="/api/v1/health", tags=["health"])


def _service_status(runtime: RuntimeState) -> str:
    if runtime.models_available <= 0:
        return "unavailable"
    if runtime.event_status in {"degraded", "unavailable"}:
        return "degraded"
    return "ok"


@router.get("/", operation_id="get_health", summary="Service health")
def get_health(runtime: RuntimeState = Depends(get_runtime)) -> Health:
    return Health(
        status=_service_status(runtime),
        ready=runtime.models_available > 0,
        models_available=runtime.models_available,
        event=runtime.event_status,  # type: ignore[arg-type]
        active_tasks=runtime.active_tasks,
        pending_restart_fields=runtime.pending_restart_fields,
    )


@router.get("/live", operation_id="get_liveness", summary="Liveness probe")
def get_liveness() -> HealthProbe:
    return HealthProbe(status="ok", ready=True, details={"service": "opal-think", "version": __version__})


@router.get(
    "/ready",
    operation_id="get_readiness",
    summary="Readiness probe",
    response_model=HealthProbe,
    responses={503: {"model": HealthProbe, "description": "No usable model or service cannot accept tasks."}},
)
def get_readiness(runtime: RuntimeState = Depends(get_runtime)) -> HealthProbe | JSONResponse:
    if runtime.models_available > 0:
        return HealthProbe(
            status="ok" if runtime.event_status not in {"degraded", "unavailable"} else "degraded",
            ready=True,
            details={"models_available": runtime.models_available, "api_version": API_VERSION},
        )
    probe = HealthProbe(
        status="unavailable",
        ready=False,
        details={"reason": "no usable model provider is available", "models_available": 0},
    )
    return JSONResponse(status_code=503, content=probe.model_dump(mode="json"))
