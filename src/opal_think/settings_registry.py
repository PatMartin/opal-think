"""Registered settings, defaults, validation, and persistence.

Only *registered* keys are accepted by the REST surface. Two key families exist:

- fixed keys with built-in defaults (``models.*``, ``event.*``, ``task.*``, ``session.*``,
  and the seeded ``providers.ollama.*`` / ``providers.openai.*`` rows), and
- dynamic ``providers.<id>.<field>`` keys, materialized on first set so adapters can add
  providers without changing the REST schema.

Overrides persist in SQLite (``settings`` table); a global monotonic revision backs the
collection ETag, and per-key revisions back ``/settings/{key}`` ETags. Credential settings
store secret *references* (``env:NAME``) — never raw credential material.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PROVIDER_KEY = re.compile(r"^providers\.([a-z][a-z0-9_-]{0,63})\.(type|base_url|enabled|credential_ref)$")
_SECRET_REF = re.compile(r"^(env:[A-Za-z_][A-Za-z0-9_]{0,127}|[A-Z][A-Z0-9_]{2,127})$")
_URL = re.compile(r"^https?://[!-~]+$")


class SettingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SettingDef:
    key: str
    default: Any
    check: Callable[[Any], str | None]  # returns error message or None
    secret_ref: bool = False
    restart_required: bool = False


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_int(lo: int, hi: int) -> Callable[[Any], str | None]:
    def check(v: Any) -> str | None:
        if not _is_int(v):
            return "must be an integer"
        return None if lo <= v <= hi else f"must be between {lo} and {hi}"

    return check


def _check_str_or_null(max_len: int = 200) -> Callable[[Any], str | None]:
    def check(v: Any) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            return "must be a string or null"
        return None if 1 <= len(v) <= max_len else f"must be 1..{max_len} characters"

    return check


def _check_bool(v: Any) -> str | None:
    return None if isinstance(v, bool) else "must be a boolean"


def _check_url_or_null(v: Any) -> str | None:
    if v is None:
        return None
    return None if isinstance(v, str) and _URL.match(v) else "must be an http(s) URL or null"


def _check_provider_type(v: Any) -> str | None:
    if v is None:
        return None
    return None if v in {"ollama", "openai", "fake"} else "must be one of: ollama, openai, fake"


def _check_secret_ref(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _SECRET_REF.match(v):
        return "must be a secret reference (env:NAME or ENV_VAR_NAME); raw credentials are forbidden"
    return None


FIXED_SETTINGS: dict[str, SettingDef] = {
    "models.default_id": SettingDef("models.default_id", None, _check_str_or_null()),
    "event.base_url": SettingDef("event.base_url", None, _check_url_or_null),
    "event.credential_ref": SettingDef("event.credential_ref", None, _check_secret_ref, secret_ref=True),
    "task.default_max_steps": SettingDef("task.default_max_steps", 8, _check_int(1, 100)),
    "task.max_steps_limit": SettingDef("task.max_steps_limit", 25, _check_int(1, 100)),
    "task.default_timeout_seconds": SettingDef("task.default_timeout_seconds", 120, _check_int(1, 3600)),
    "task.timeout_limit_seconds": SettingDef("task.timeout_limit_seconds", 600, _check_int(1, 3600)),
    "task.max_concurrent": SettingDef("task.max_concurrent", 4, _check_int(1, 64)),
    "task.max_causation_depth": SettingDef("task.max_causation_depth", 8, _check_int(0, 64)),
    "task.retention_days": SettingDef("task.retention_days", 30, _check_int(0, 3650)),
    "session.retention_days": SettingDef("session.retention_days", 30, _check_int(0, 3650)),
    "providers.ollama.type": SettingDef("providers.ollama.type", "ollama", _check_provider_type),
    "providers.ollama.base_url": SettingDef("providers.ollama.base_url", "http://127.0.0.1:11434", _check_url_or_null),
    "providers.ollama.enabled": SettingDef("providers.ollama.enabled", True, _check_bool),
    "providers.ollama.credential_ref": SettingDef("providers.ollama.credential_ref", None, _check_secret_ref, secret_ref=True),
    "providers.openai.type": SettingDef("providers.openai.type", "openai", _check_provider_type),
    "providers.openai.base_url": SettingDef("providers.openai.base_url", "https://api.openai.com", _check_url_or_null),
    "providers.openai.enabled": SettingDef("providers.openai.enabled", False, _check_bool),
    "providers.openai.credential_ref": SettingDef("providers.openai.credential_ref", None, _check_secret_ref, secret_ref=True),
}

_PROVIDER_FIELD_DEFS: dict[str, SettingDef] = {
    "type": SettingDef("providers.*.type", None, _check_provider_type),
    "base_url": SettingDef("providers.*.base_url", None, _check_url_or_null),
    "enabled": SettingDef("providers.*.enabled", False, _check_bool),
    "credential_ref": SettingDef("providers.*.credential_ref", None, _check_secret_ref, secret_ref=True),
}


def lookup(key: str) -> SettingDef | None:
    """Resolve a setting definition: fixed key, then the dynamic providers.<id>.<field> pattern."""
    if key in FIXED_SETTINGS:
        return FIXED_SETTINGS[key]
    match = _PROVIDER_KEY.match(key)
    if match:
        return _PROVIDER_FIELD_DEFS[match.group(2)]
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SettingsStore:
    """SQLite-backed override store with a global revision and per-key revisions.

    Only overrides are persisted; defaults live in the registry. All writes take a short
    exclusive lock and a single transaction, so multi-key patches are atomic.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute("INSERT OR IGNORE INTO meta (name, value) VALUES ('revision', '0')")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=10)

    def global_revision(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE name='revision'").fetchone()
            return int(row[0])

    def get_override(self, key: str) -> tuple[Any, int] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value, revision FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), int(row[1])

    def all_overrides(self) -> dict[str, tuple[Any, int]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value, revision FROM settings").fetchall()
        return {key: (json.loads(value), int(rev)) for key, value, rev in rows}

    def _bump(self, conn: sqlite3.Connection) -> int:
        revision = int(conn.execute("SELECT value FROM meta WHERE name='revision'").fetchone()[0]) + 1
        conn.execute("UPDATE meta SET value=? WHERE name='revision'", (str(revision),))
        return revision

    def set_many(self, values: dict[str, Any]) -> int:
        """Atomically write overrides; returns the new global revision."""
        with self._lock, self._connect() as conn:
            revision = self._bump(conn)
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings (key, value, revision, updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, revision=excluded.revision, "
                    "updated_at=excluded.updated_at",
                    (key, json.dumps(value), revision, _now()),
                )
            return revision

    def reset(self, key: str) -> int:
        """Remove an override (restore default); returns the new global revision."""
        with self._lock, self._connect() as conn:
            revision = self._bump(conn)
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
            return revision


class SettingsRegistry:
    """Read model over the store: merges defaults with persisted overrides."""

    def __init__(self, store: SettingsStore) -> None:
        self.store = store

    def registered_keys(self) -> list[str]:
        """Fixed keys plus materialized dynamic provider keys, sorted."""
        keys = set(FIXED_SETTINGS)
        keys.update(key for key in self.store.all_overrides() if lookup(key) is not None)
        return sorted(keys)

    def get(self, key: str) -> dict[str, Any] | None:
        definition = lookup(key)
        if definition is None:
            return None
        override = self.store.get_override(key)
        if override is None:
            return {
                "key": key,
                "value": definition.default,
                "default": definition.default,
                "is_default": True,
                "pending_restart": False,
                "revision": 0,
            }
        value, revision = override
        return {
            "key": key,
            "value": value,
            "default": definition.default,
            "is_default": False,
            "pending_restart": definition.restart_required,
            "revision": revision,
        }

    def snapshot(self) -> tuple[list[dict[str, Any]], int, list[str]]:
        items, pending_restart = [], []
        for key in self.registered_keys():
            item = self.get(key)
            assert item is not None
            items.append(item)
            if item["pending_restart"]:
                pending_restart.append(key)
        return items, self.store.global_revision(), pending_restart

    def validate_value(self, key: str, value: Any) -> str | None:
        definition = lookup(key)
        if definition is None:
            return "unknown setting key"
        return definition.check(value)

    def etag_collection(self) -> str:
        return f'"s{self.store.global_revision()}"'

    @staticmethod
    def etag_key(revision: int) -> str:
        return f'"k{revision}"'
