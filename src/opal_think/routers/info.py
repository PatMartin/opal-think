"""Service identity endpoints (``/api/v1/info/`` and ``/api/v1/info/ping``).

Kernel ``info``/``info_ping`` path items: identity (name, package version, contract
API version) plus a lightweight liveness probe for process supervisors.
"""

from __future__ import annotations

from fastapi import APIRouter

from opal_think import API_VERSION, __version__
from opal_think.schemas import LivenessRead, ServiceInfo

router = APIRouter(prefix="/api/v1/info", tags=["info"])


@router.get("/", operation_id="opal_info_get", summary="Service Info")
def get_info() -> ServiceInfo:
    return ServiceInfo(
        name="opal-think-svc",
        version=__version__,
        api_version=API_VERSION,
        description="OPAL Think: model-neutral, guarded reasoning service",
    )


@router.get("/ping", operation_id="opal_info_ping", summary="Ping")
def get_ping() -> LivenessRead:
    return LivenessRead()
