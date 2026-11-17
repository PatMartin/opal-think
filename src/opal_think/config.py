"""Typed service configuration for OPAL-THINK.

Configuration sources:
- optional YAML file (``--config`` / ``OPAL_THINK_CONFIG``)
- environment overrides (``OPAL_THINK_HOST``, ``OPAL_THINK_PORT``)
- secrets always via environment (e.g. ``OPAL_THINK_API_TOKEN``); config stores *references*, never
  raw secret material.

Portable runtime settings (provider URLs, task ceilings, retention) are managed through the
REST ``/api/v1/setting/`` surface implemented by the settings registry, not this bootstrap config.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in {"localhost", "localhost.localdomain"}


@dataclass
class AuthConfig:
    enabled: bool = False
    token_env: str = "OPAL_THINK_API_TOKEN"


@dataclass
class ThinkConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: Path = Path("var/think.db")
    auth: AuthConfig = field(default_factory=AuthConfig)
    config_path: str | None = None  # file the config was loaded from, for managed restarts

    def validate(self) -> None:
        """Suite policy: REST bearer auth is mandatory on non-loopback binds."""
        if not is_loopback(self.host) and not self.auth.enabled:
            raise ValueError(
                f"refusing to start: host {self.host!r} is non-loopback but auth.enabled is false "
                "(REST bearer auth is required on non-loopback interfaces)"
            )


def load_config(path: str | os.PathLike[str] | None = None) -> ThinkConfig:
    config = ThinkConfig()
    config_path = path or os.environ.get("OPAL_THINK_CONFIG")
    if config_path:
        config.config_path = str(config_path)
        data: dict[str, Any] = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        server = data.get("server", {})
        config.host = server.get("host", config.host)
        config.port = int(server.get("port", config.port))
        if "db_path" in data:
            config.db_path = Path(data["db_path"])
        auth = data.get("auth", {})
        config.auth.enabled = bool(auth.get("enabled", config.auth.enabled))
        config.auth.token_env = auth.get("token_env", config.auth.token_env)
    if "OPAL_THINK_HOST" in os.environ:
        config.host = os.environ["OPAL_THINK_HOST"]
    if "OPAL_THINK_PORT" in os.environ:
        config.port = int(os.environ["OPAL_THINK_PORT"])
    if "OPAL_THINK_DB_PATH" in os.environ:
        config.db_path = Path(os.environ["OPAL_THINK_DB_PATH"])
    return config


def dotenv_baseline(path: str | os.PathLike[str] | None = None) -> dict[str, object]:
    config = ThinkConfig()
    config_path = Path(path) if path else None
    if config_path:
        data: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        server = data.get("server", {})
        config.host = server.get("host", config.host)
        config.port = int(server.get("port", config.port))
        config.db_path = Path(data.get("db_path", config.db_path))
    values = {
        "OPAL_THINK_HOST": config.host,
        "OPAL_THINK_PORT": config.port,
        "OPAL_THINK_DB_PATH": config.db_path,
        "OPAL_THINK_API_TOKEN": "<set-a-token>",
    }
    if config_path:
        values["OPAL_THINK_CONFIG"] = str(config_path)
    return values
