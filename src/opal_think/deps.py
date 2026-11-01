"""FastAPI dependency accessors and the shared runtime state stored on ``app.state``."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from opal_think.config import ThinkConfig
from opal_think.opal_context import OpalContext, get_opal_context
from opal_think.providers.registry import ProviderRegistry
from opal_think.settings_registry import SettingsRegistry
from opal_think.store import ThinkStore


@dataclass
class RuntimeState:
    """Mutable service-wide runtime facts owned by the task engine.

    Model/EVENT availability is probed live by the provider registry, not stored here.
    """

    active_tasks: int = 0


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


def get_providers(request: Request) -> ProviderRegistry:
    return request.app.state.providers
