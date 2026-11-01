"""FastAPI dependency accessors and the shared runtime state stored on ``app.state``."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request

from opal_think.config import ThinkConfig
from opal_think.opal_context import OpalContext, get_opal_context
from opal_think.settings_registry import SettingsRegistry
from opal_think.store import ThinkStore


@dataclass
class RuntimeState:
    """Mutable service-wide runtime facts consumed by the health surface and task engine.

    Later steps replace the static counters with real provider registry and task engine reads.
    """

    models_available: int = 0
    event_status: str = "not_configured"  # available | degraded | unavailable | not_configured
    active_tasks: int = 0
    pending_restart_fields: list[str] = field(default_factory=list)


def get_config(request: Request) -> ThinkConfig:
    return request.app.state.config


def get_runtime(request: Request) -> RuntimeState:
    return request.app.state.runtime


def get_context(request: Request) -> OpalContext:
    return get_opal_context(request)


def get_settings(request: Request) -> SettingsRegistry:
    return request.app.state.settings


def get_store(request: Request) -> ThinkStore:
    return request.app.state.store
