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


class RevisionConflict(Exception):
    """The expected revision did not match inside the write transaction (stale ETag)."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected revision {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


class PersonaExists(Exception):
    """Persona create collided with an already-registered persona_id (409)."""


class PersonaMissing(Exception):
    """Persona addressed by a mutation is not registered (404)."""


class ProfileExists(Exception):
    """Profile create/patch collided with an existing name or id, or the catalog is full (409)."""


class ProfileMissing(Exception):
    """Profile addressed by a mutation is not registered (404)."""


class ProfileConflict(Exception):
    """Profile mutation is itself valid but conflicts with profile invariants (409):
    deleting the active profile, or changing the immutable id."""


class ProfileSettingsInvalid(Exception):
    """A profile's ``settings`` compartment failed settings-registry validation (422)."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("; ".join(f"{e['key']}: {e['reason']}" for e in errors))
        self.errors = errors


class SettingsStore:
    """SQLite-backed portable-configuration store with a global revision and per-key revisions.

    Only setting *overrides* are persisted; defaults live in the registry. The ``personas``
    table shares the same global revision: personas are part of the logical portable
    configuration, so persona mutations bump it and accept the same ``"s<n>"`` ETags.
    All writes take a short exclusive lock and a single transaction, so multi-key patches
    and preconditioned persona mutations are atomic.
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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS personas ("
                "persona_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, voice_id TEXT,"
                " enabled INTEGER NOT NULL, origin TEXT NOT NULL DEFAULT 'local',"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS profiles ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
                " is_active INTEGER NOT NULL DEFAULT 0, record TEXT NOT NULL)"
            )
            conn.execute("INSERT OR IGNORE INTO meta (name, value) VALUES ('revision', '0')")
            if "origin" not in {r[1] for r in conn.execute("PRAGMA table_info(personas)")}:
                conn.execute("ALTER TABLE personas ADD COLUMN origin TEXT NOT NULL DEFAULT 'local'")

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

    def _checked_write(
        self,
        writer: Callable[[sqlite3.Connection], int],
        *,
        expect_global: int | None = None,
        key: str | None = None,
        expect_key: int | None = None,
    ) -> int:
        """Check precondition revisions and mutate inside one IMMEDIATE transaction.

        ``BEGIN IMMEDIATE`` serializes writers across connections: a concurrent
        writer blocks on the SQLite busy timeout then sees the fresh revision, so
        exactly one writer with a given ETag wins and the loser gets
        :class:`RevisionConflict` — no lost updates."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if expect_global is not None:
                    actual = int(conn.execute("SELECT value FROM meta WHERE name='revision'").fetchone()[0])
                    if actual != expect_global:
                        raise RevisionConflict(expect_global, actual)
                if key is not None and expect_key is not None:
                    row = conn.execute("SELECT revision FROM settings WHERE key=?", (key,)).fetchone()
                    actual_key = int(row[0]) if row else 0
                    if actual_key != expect_key:
                        raise RevisionConflict(expect_key, actual_key)
                revision = writer(conn)
                conn.commit()
                return revision
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def set_many_checked(
        self,
        values: dict[str, Any],
        *,
        expect_global: int | None = None,
        key: str | None = None,
        expect_key: int | None = None,
    ) -> int:
        """Atomic precondition check + write overrides; returns the new global revision."""

        def write(conn: sqlite3.Connection) -> int:
            revision = self._bump(conn)
            for k, value in values.items():
                conn.execute(
                    "INSERT INTO settings (key, value, revision, updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, revision=excluded.revision, "
                    "updated_at=excluded.updated_at",
                    (k, json.dumps(value), revision, _now()),
                )
            return revision

        return self._checked_write(write, expect_global=expect_global, key=key, expect_key=expect_key)

    def reset_checked(self, key: str, *, expect_key: int | None = None) -> int:
        """Atomic precondition check + remove override; returns the new global revision."""

        def write(conn: sqlite3.Connection) -> int:
            revision = self._bump(conn)
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
            return revision

        return self._checked_write(write, key=key, expect_key=expect_key)

    def set_many(self, values: dict[str, Any]) -> int:
        """Atomically write overrides; returns the new global revision."""
        return self.set_many_checked(values)

    def reset(self, key: str) -> int:
        """Remove an override (restore default); returns the new global revision."""
        return self.reset_checked(key)

    # -- personas (v1.6.0; share the global config revision) ---------------------------------------------

    @staticmethod
    def _persona_row(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        return data

    def list_personas(self, enabled: bool | None = None) -> list[dict[str, Any]]:
        """Complete bounded catalog ordered by persona_id; optional enabled filter."""
        sql, params = "SELECT * FROM personas WHERE 1 = 1", []
        if enabled is not None:
            sql, params = "SELECT * FROM personas WHERE enabled = ?", [int(enabled)]
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql + " ORDER BY persona_id", params).fetchall()
        return [self._persona_row(r) for r in rows]

    def get_persona(self, persona_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return self._persona_row(
                conn.execute("SELECT * FROM personas WHERE persona_id = ?", (persona_id,)).fetchone()
            )

    def insert_persona_checked(
        self, persona: dict[str, Any], *, expect_global: int | None = None, max_count: int = 10_000
    ) -> int:
        """Insert a persona row; bumps the global revision. ``PersonaExists`` on
        duplicate id or a full catalog; ``RevisionConflict`` on stale ETag."""

        def write(conn: sqlite3.Connection) -> int:
            if conn.execute("SELECT 1 FROM personas WHERE persona_id = ?", (persona["persona_id"],)).fetchone():
                raise PersonaExists(persona["persona_id"])
            if conn.execute("SELECT COUNT(*) FROM personas").fetchone()[0] >= max_count:
                raise PersonaExists("persona catalog is full")
            revision = self._bump(conn)
            conn.execute(
                "INSERT INTO personas (persona_id, name, description, voice_id, enabled, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    persona["persona_id"],
                    persona["name"],
                    persona.get("description"),
                    persona.get("voice_id"),
                    int(persona["enabled"]),
                    persona["created_at"],
                    persona["updated_at"],
                ),
            )
            return revision

        return self._checked_write(write, expect_global=expect_global)

    def update_persona_checked(
        self, persona_id: str, changes: dict[str, Any], *, expect_global: int | None = None
    ) -> int:
        """Apply a partial update; bumps the global revision. ``PersonaMissing`` on
        unknown id; ``RevisionConflict`` on stale ETag."""

        def write(conn: sqlite3.Connection) -> int:
            if conn.execute("SELECT 1 FROM personas WHERE persona_id = ?", (persona_id,)).fetchone() is None:
                raise PersonaMissing(persona_id)
            revision = self._bump(conn)
            if "enabled" in changes:
                changes["enabled"] = int(changes["enabled"])
            assignments = ", ".join(f"{field} = ?" for field in (*changes, "updated_at"))
            conn.execute(
                f"UPDATE personas SET {assignments} WHERE persona_id = ?",
                [*changes.values(), _now(), persona_id],
            )
            return revision

        return self._checked_write(write, expect_global=expect_global)

    def delete_persona_checked(self, persona_id: str, *, expect_global: int | None = None) -> int:
        """Delete a persona row; bumps the global revision. ``PersonaMissing`` on
        unknown id; ``RevisionConflict`` on stale ETag."""

        def write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute("DELETE FROM personas WHERE persona_id = ?", (persona_id,))
            if cursor.rowcount == 0:
                raise PersonaMissing(persona_id)
            return self._bump(conn)

        return self._checked_write(write, expect_global=expect_global)

    # -- profiles (v1.7.1; share the global config revision) ---------------------------------------------
    #
    # A profile is a named preset of operating defaults. ``id``/``name``/``is_active``
    # are the indexed suite-standard fields; the ``record`` JSON holds everything else:
    # description, timestamps, this service's ``settings`` compartment, and any foreign
    # compartments other OPAL services attach (stored opaquely — never interpreted).
    # Activating a profile applies its ``settings`` compartment as a write-only preset:
    # the values are written into the settings table in the same transaction and are
    # NOT reverted when another profile is activated later.

    @staticmethod
    def _profile_row(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
        """Recompose the wire record: indexed suite fields + opaque record JSON.

        Accepts both ``sqlite3.Row`` (reads via ``conn.row_factory``) and plain tuples
        ``(id, name, is_active, record)`` from write transactions."""
        if row is None:
            return None
        pid, name, is_active, record_json = (
            (row["id"], row["name"], row["is_active"], row["record"]) if isinstance(row, sqlite3.Row) else row
        )
        return {"id": pid, "name": name, "is_active": bool(is_active), **json.loads(record_json)}

    @staticmethod
    def _preset_errors(record: dict[str, Any]) -> list[dict[str, Any]]:
        """Validate a profile's ``settings`` compartment against the registry."""
        errors = []
        for key, value in (record.get("settings") or {}).items():
            if key == "pending_restart_fields":
                errors.append({"key": key, "reason": "reserved, read-only key"})
                continue
            definition = lookup(key)
            if definition is None:
                errors.append({"key": key, "reason": "unknown setting key"})
            elif (reason := definition.check(value)) is not None:
                errors.append({"key": key, "reason": reason})
        return errors

    @staticmethod
    def _write_preset(conn: sqlite3.Connection, revision: int, record: dict[str, Any]) -> None:
        """Apply a profile's ``settings`` compartment as live setting overrides."""
        now = _now()
        for key, value in (record.get("settings") or {}).items():
            conn.execute(
                "INSERT INTO settings (key, value, revision, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, revision=excluded.revision, "
                "updated_at=excluded.updated_at",
                (key, json.dumps(value), revision, now),
            )

    def _activate(self, conn: sqlite3.Connection, revision: int, record: dict[str, Any]) -> None:
        """Mark `record`'s profile the only active one and apply its preset."""
        conn.execute("UPDATE profiles SET is_active = 0")
        conn.execute("UPDATE profiles SET is_active = 1 WHERE id = ?", (record["id"],))
        self._write_preset(conn, revision, record)

    @staticmethod
    def _check_preset(record: dict[str, Any]) -> None:
        errors = SettingsStore._preset_errors(record)
        if errors:
            raise ProfileSettingsInvalid(errors)

    @staticmethod
    def _profile_record_column(record: dict[str, Any]) -> str:
        """Record JSON = full document minus the indexed suite fields."""
        return json.dumps({k: v for k, v in record.items() if k not in ("id", "name", "is_active")})

    def list_profiles(self) -> list[dict[str, Any]]:
        """Complete bounded catalog ordered by id."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id, name, is_active, record FROM profiles ORDER BY id").fetchall()
        return [self._profile_row(r) for r in rows]

    def get_profile(self, profile_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return self._profile_row(
                conn.execute(
                    "SELECT id, name, is_active, record FROM profiles WHERE id = ?", (profile_id,)
                ).fetchone()
            )

    def insert_profile_checked(
        self, record: dict[str, Any], *, expect_global: int | None = None, max_count: int = 10_000
    ) -> tuple[int, int]:
        """Insert a profile; bumps the global revision. ``ProfileExists`` on duplicate
        name/id or a full catalog; ``ProfileSettingsInvalid`` on a bad ``settings``
        compartment; ``RevisionConflict`` on stale ETag. When the record is active it
        becomes the only active profile and its preset applies in the same write.
        Returns (revision, profile_id)."""

        def write(conn: sqlite3.Connection) -> tuple[int, int]:
            row = dict(record)
            if conn.execute("SELECT 1 FROM profiles WHERE name = ?", (row["name"],)).fetchone():
                raise ProfileExists(f"profile name '{row['name']}' already exists")
            if row.get("id") is not None and conn.execute(
                "SELECT 1 FROM profiles WHERE id = ?", (row["id"],)
            ).fetchone():
                raise ProfileExists(f"profile id {row['id']} is already in use")
            if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] >= max_count:
                raise ProfileExists(f"profile catalog is full (max {max_count})")
            if row.get("id") is None:
                row["id"] = (conn.execute("SELECT MAX(id) FROM profiles").fetchone()[0] or 0) + 1
            self._check_preset(row)
            revision = self._bump(conn)
            if row.get("is_active"):
                self._activate(conn, revision, row)
            conn.execute(
                "INSERT INTO profiles (id, name, is_active, record) VALUES (?,?,?,?)",
                (row["id"], row["name"], int(bool(row.get("is_active"))), self._profile_record_column(row)),
            )
            return revision, row["id"]

        return self._checked_write(write, expect_global=expect_global)

    def update_profile_checked(
        self, profile_id: int, changes: dict[str, Any], *, expect_global: int | None = None
    ) -> int:
        """Top-level merge into one profile record; bumps the global revision.
        ``ProfileMissing`` on unknown id, ``ProfileExists`` on a name collision,
        ``ProfileConflict`` on an ``id`` change, ``ProfileSettingsInvalid`` on a bad
        ``settings`` compartment, ``RevisionConflict`` on stale ETag. ``is_active``
        set true activates the profile (deactivating all others and applying its
        preset); every other supplied key — including foreign compartments — is
        merged verbatim."""

        def write(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT id, name, is_active, record FROM profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if row is None:
                raise ProfileMissing(profile_id)
            patch = {k: v for k, v in changes.items() if k != "created_at"}
            if "id" in patch and patch["id"] != profile_id:
                raise ProfileConflict("profile id is immutable")
            patch.pop("id", None)
            if "name" in patch and conn.execute(
                "SELECT 1 FROM profiles WHERE name = ? AND id != ?", (patch["name"], profile_id)
            ).fetchone():
                raise ProfileExists(f"profile name '{patch['name']}' already exists")
            record = self._profile_row(row)
            record.update(patch)
            record["updated_at"] = _now()
            self._check_preset(record)
            revision = self._bump(conn)
            active = bool(record.get("is_active"))
            if patch.get("is_active") is True:
                self._activate(conn, revision, record)
            conn.execute(
                "UPDATE profiles SET name = ?, is_active = ?, record = ? WHERE id = ?",
                (record["name"], int(active), self._profile_record_column(record), profile_id),
            )
            return revision

        return self._checked_write(write, expect_global=expect_global)

    def delete_profile_checked(self, profile_id: int, *, expect_global: int | None = None) -> int:
        """Delete a profile; bumps the global revision. ``ProfileMissing`` on unknown
        id; ``ProfileConflict`` when the profile is active (the active profile cannot
        be deleted); ``RevisionConflict`` on stale ETag."""

        def write(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT is_active FROM profiles WHERE id = ?", (profile_id,)).fetchone()
            if row is None:
                raise ProfileMissing(profile_id)
            if row[0]:
                raise ProfileConflict("the active profile cannot be deleted")
            conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
            return self._bump(conn)

        return self._checked_write(write, expect_global=expect_global)

    def activate_profile_checked(self, profile_id: int, *, expect_global: int | None = None) -> int:
        """Make `profile_id` the only active profile and apply its preset; bumps the
        global revision. ``ProfileMissing`` on unknown id, ``ProfileSettingsInvalid``
        on a bad stored preset, ``RevisionConflict`` on stale ETag."""

        def write(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT id, name, is_active, record FROM profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if row is None:
                raise ProfileMissing(profile_id)
            record = self._profile_row(row)
            record["updated_at"] = _now()
            self._check_preset(record)
            revision = self._bump(conn)
            self._activate(conn, revision, record)
            record["is_active"] = True
            conn.execute(
                "UPDATE profiles SET record = ? WHERE id = ?",
                (self._profile_record_column(record), profile_id),
            )
            return revision

        return self._checked_write(write, expect_global=expect_global)


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
