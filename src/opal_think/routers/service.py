"""``/api/v1/service``: managed server-process lifecycle (v1.7.1 kernel ``service`` tag).

All three routes delegate to :class:`opal_think.lifecycle.ServiceProcessManager`. When
the pidfile-tracked process *is* this serving process, start/stop report the status and
then self-terminate after the response is sent — ``start`` on self is therefore a
restart.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query

from opal_think.deps import get_config
from opal_think.errors import OpalHTTPException
from opal_think.lifecycle import ServiceProcessManager
from opal_think.schemas import ServiceLogTail, ServiceStartOptions, ServiceStatus

router = APIRouter(prefix="/api/v1/service", tags=["service"])


def _manager(config) -> ServiceProcessManager:
    return ServiceProcessManager(config)


@router.post("/start", operation_id="opal_service_start", summary="Start service")
def service_start(
    background_tasks: BackgroundTasks,
    config=Depends(get_config),
    body: Annotated[ServiceStartOptions | None, Body()] = None,
) -> ServiceStatus:
    manager = _manager(config)
    status, terminate_self = manager.start(append=body.append if body else False)
    if terminate_self:
        background_tasks.add_task(manager.terminate_self)
    return ServiceStatus(**status)


@router.post("/stop", operation_id="opal_service_stop", summary="Stop service")
def service_stop(background_tasks: BackgroundTasks, config=Depends(get_config)) -> ServiceStatus:
    manager = _manager(config)
    status, terminate_self = manager.stop()
    if terminate_self:
        background_tasks.add_task(manager.terminate_self)
    return ServiceStatus(**status)


@router.get("/log", operation_id="opal_service_log", summary="Tail service log")
def service_log(
    config=Depends(get_config),
    lines: Annotated[int, Query(ge=1, le=1000)] = 10,
) -> ServiceLogTail:
    manager = _manager(config)
    tail = manager.tail_log(lines)
    if tail is None:
        raise OpalHTTPException(404, "not_found", f"no service log at {manager.log_file}")
    return ServiceLogTail(**tail)
