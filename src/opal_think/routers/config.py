"""``/api/v1/config``: kernel ``config save|load|validate`` (v0.1.0). Portable
``OpalConfigSnapshot`` envelopes carry flat setting-key overrides plus the declared
resource sections (profiles/personas/models/engines); history, sessions, runtime state
and secrets are never included. Loads are atomic full replacements under a mandatory
``If-Match`` revision; a failed validation or write leaves the live config intact.
``models``/``engines`` are declared sections think does not own: they must be arrays
and non-empty content is warned-and-ignored on load.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response

from opal_think.deps import get_config, get_settings
from opal_think.errors import OpalHTTPException
from opal_think.preconditions import if_match_revision
from opal_think.schemas import (
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MODULE,
    SNAPSHOT_RESOURCE_SECTIONS,
    ConfigLoadRequest,
    ConfigLoadResult,
    ConfigSaveRequest,
    ConfigSaveResult,
    ConfigSnapshot,
    ConfigValidation,
    OpalValidationIssue,
)
from opal_think.settings_registry import RevisionConflict, SettingsRegistry, SettingsStore

router = APIRouter(prefix="/api/v1/config", tags=["config"])

MANAGED_SNAPSHOT_NAME = "opal-think.snapshot.json"
_PERSONA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


def _managed_path(config) -> Path:
    """The module's single default managed snapshot, beside the SQLite database."""
    return Path(config.db_path).parent / MANAGED_SNAPSHOT_NAME


def _issue(code: str, path: str, message: str) -> OpalValidationIssue:
    return OpalValidationIssue(code=code, path=path, message=message)


def _build_snapshot(registry: SettingsRegistry) -> ConfigSnapshot:
    """Snapshot of the live persistent desired configuration (for save and for
    validating the live configuration on a bare ``config validate`` call)."""
    overrides = {key: value for key, (value, _rev) in registry.store.all_overrides().items()}
    return ConfigSnapshot(
        created_at=datetime.now(UTC),
        settings=overrides,
        resources={
            "profiles": registry.store.list_profiles(),
            "personas": registry.store.list_personas(),
            "models": [],
            "engines": [],
        },
    )


def _write_managed(path: Path, snapshot: ConfigSnapshot) -> None:
    """Atomic create-or-replace of the managed snapshot; a failed write preserves
    the previous snapshot (temp file + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot.model_dump(mode="json"), indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".opal-think-snapshot-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise OpalHTTPException(500, "operation_failed", f"failed to write managed snapshot: {exc}") from exc


def _read_managed(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise OpalHTTPException(
            404, "not_found", "no managed snapshot exists; save one first or supply a candidate snapshot"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OpalHTTPException(500, "operation_failed", f"managed snapshot is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise OpalHTTPException(500, "operation_failed", "managed snapshot is not a JSON object")
    return data


def _validate_snapshot(
    candidate: Any, registry: SettingsRegistry
) -> tuple[list[OpalValidationIssue], list[OpalValidationIssue], dict[str, Any] | None]:
    """Validate a candidate snapshot without touching live state. Returns
    (errors, warnings, parsed) where parsed is None when the envelope is malformed."""
    errors: list[OpalValidationIssue] = []
    warnings: list[OpalValidationIssue] = []
    if not isinstance(candidate, dict):
        errors.append(_issue("invalid_envelope", "", "snapshot must be a JSON object"))
        return errors, warnings, None
    known = {"format_version", "module", "created_at", "settings", "resources"}
    for key in candidate:
        if key not in known:
            errors.append(_issue("unknown_field", key, f"unknown snapshot field '{key}'"))
    if candidate.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        errors.append(
            _issue(
                "unsupported_format",
                "format_version",
                f"format_version {candidate.get('format_version')!r} is not accepted (want '{SNAPSHOT_FORMAT_VERSION}')",
            )
        )
    if not candidate.get("created_at"):
        errors.append(_issue("invalid_envelope", "created_at", "snapshot is missing required 'created_at'"))
    if candidate.get("module") != SNAPSHOT_MODULE:
        errors.append(
            _issue("wrong_module", "module", f"snapshot is for module {candidate.get('module')!r}, not '{SNAPSHOT_MODULE}'")
        )
    for field in ("settings", "resources"):
        if field not in candidate:
            errors.append(_issue("invalid_envelope", field, f"snapshot is missing required '{field}'"))
    settings = candidate.get("settings", {})
    if not isinstance(settings, dict):
        errors.append(_issue("invalid_section", "settings", "settings must be a flat object"))
        settings = {}
    resources = candidate.get("resources", {})
    if not isinstance(resources, dict):
        errors.append(_issue("invalid_section", "resources", "resources must be an object"))
        resources = {}
    for key in resources:
        if key not in SNAPSHOT_RESOURCE_SECTIONS:
            errors.append(_issue("unknown_section", f"resources.{key}", f"unknown resource section '{key}'"))
    for key, value in settings.items():
        if key == "pending_restart_fields":
            errors.append(_issue("reserved_setting", f"settings.{key}", "setting 'pending_restart_fields' is reserved"))
        elif (reason := registry.validate_value(key, value)) is not None:
            code = "unknown_setting" if reason == "unknown setting key" else "invalid_value"
            errors.append(_issue(code, f"settings.{key}", f"{reason}"))
    personas = resources.get("personas", [])
    profiles = resources.get("profiles", [])
    parsed: dict[str, Any] = {"settings": settings, "personas": [], "profiles": []}
    for section in ("personas", "profiles", "models", "engines"):
        items = resources.get(section, [])
        if not isinstance(items, list):
            errors.append(_issue("invalid_section", f"resources.{section}", "section must be an array"))
    personas = personas if isinstance(personas, list) else []
    profiles = profiles if isinstance(profiles, list) else []
    seen: set[str] = set()
    for i, persona in enumerate(personas):
        path = f"resources.personas[{i}]"
        if not isinstance(persona, dict) or not persona.get("persona_id") or not isinstance(persona.get("persona_id"), str):
            errors.append(_issue("invalid_resource", f"{path}.persona_id", "persona is missing required 'persona_id'"))
            continue
        pid = persona["persona_id"]
        if pid in seen:
            errors.append(_issue("duplicate_id", f"{path}.persona_id", f"duplicate persona id: {pid}"))
        seen.add(pid)
        if not re.match(_PERSONA_ID_PATTERN, pid):
            errors.append(_issue("invalid_resource", f"{path}.persona_id", f"persona id {pid!r} violates the id pattern"))
        if not persona.get("name"):
            errors.append(_issue("invalid_resource", f"{path}.name", f"persona '{pid}' is missing required 'name'"))
        parsed["personas"].append(persona)
    seen_ids: set[Any] = set()
    seen_names: set[str] = set()
    active = 0
    for i, profile in enumerate(profiles):
        path = f"resources.profiles[{i}]"
        if not isinstance(profile, dict):
            errors.append(_issue("invalid_resource", path, "profile must be an object"))
            continue
        pid, name = profile.get("id"), profile.get("name")
        if pid is None or isinstance(pid, bool):
            errors.append(_issue("invalid_resource", f"{path}.id", f"profile has missing/invalid 'id': {pid!r}"))
        elif pid in seen_ids:
            errors.append(_issue("duplicate_id", f"{path}.id", f"duplicate profile id: {pid}"))
        else:
            seen_ids.add(pid)
        if not name:
            errors.append(_issue("invalid_resource", f"{path}.name", f"profile {pid or '?'} is missing required 'name'"))
        elif name in seen_names:
            errors.append(_issue("duplicate_id", f"{path}.name", f"duplicate profile name: {name}"))
        else:
            seen_names.add(name)
        if profile.get("is_active"):
            active += 1
        errors.extend(
            _issue("invalid_resource", f"{path}.{e['key']}", e["reason"])
            for e in SettingsStore._preset_errors(profile)
        )
        parsed["profiles"].append(profile)
    if active > 1:
        errors.append(
            _issue("profile_invariant", "resources.profiles", "more than one active profile: exactly one may be active")
        )
    for section in ("models", "engines"):
        items = resources.get(section, [])
        if items:
            warnings.append(
                _issue(
                    "unsupported_section",
                    f"resources.{section}",
                    f"section '{section}' is declared but unused by this service; content ignored on load",
                )
            )
    return errors, warnings, parsed


def _explicit_null(body: ConfigLoadRequest | None) -> bool:
    return body is not None and "snapshot" in body.model_fields_set and body.snapshot is None


@router.post("/save", operation_id="opal_config_save", summary="Save configuration snapshot")
def config_save(
    response: Response,
    body: ConfigSaveRequest | None = Body(default=None),
    registry: SettingsRegistry = Depends(get_settings),
    config=Depends(get_config),
) -> ConfigSaveResult:
    """``destination: managed`` atomically creates or replaces the default managed
    snapshot; ``response`` only returns the snapshot (CLI ``--out``). Save does not
    mutate live config; the response ETag carries the captured live revision."""
    destination = (body or ConfigSaveRequest()).destination
    snapshot = _build_snapshot(registry)
    managed = destination == "managed"
    if managed:
        _write_managed(_managed_path(config), snapshot)
    response.headers["ETag"] = registry.etag_collection()
    return ConfigSaveResult(snapshot=snapshot, managed=managed)


@router.post("/load", operation_id="opal_config_load", summary="Load configuration snapshot")
def config_load(
    request: Request,
    response: Response,
    body: ConfigLoadRequest | None = Body(default=None),
    registry: SettingsRegistry = Depends(get_settings),
    config=Depends(get_config),
) -> ConfigLoadResult:
    """Atomic replace of the complete portable configuration. Omit ``snapshot`` (or
    send ``{}``) to load the managed snapshot — 404 when none exists; an explicit
    ``null`` snapshot is invalid. Strong ``If-Match`` is mandatory (428/412)."""
    expected = if_match_revision(request, "s")
    if _explicit_null(body):
        raise OpalHTTPException(
            422,
            "invalid_snapshot",
            "explicit null snapshot is invalid; omit the field or send {} to load the managed snapshot",
        )
    candidate = body.snapshot if body and body.snapshot is not None else _read_managed(_managed_path(config))
    errors, warnings, parsed = _validate_snapshot(candidate, registry)
    if errors or parsed is None:
        raise OpalHTTPException(
            422,
            "invalid_snapshot",
            "snapshot failed validation",
            details={
                "errors": [e.model_dump(mode="json") for e in errors],
                "warnings": [w.model_dump(mode="json") for w in warnings],
            },
        )
    try:
        revision = registry.store.replace_portable_config_checked(
            settings=parsed["settings"],
            personas=parsed["personas"],
            profiles=parsed["profiles"],
            expect_global=expected,
        )
    except RevisionConflict as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current configuration revision; re-read and retry",
        ) from exc
    _, _, pending_restart = registry.snapshot()
    response.headers["ETag"] = f'"s{revision}"'
    return ConfigLoadResult(applied=True, pending_restart_fields=pending_restart, warnings=warnings)


@router.post("/validate", operation_id="opal_config_validate", summary="Validate configuration")
def config_validate(
    body: ConfigLoadRequest | None = Body(default=None),
    registry: SettingsRegistry = Depends(get_settings),
) -> ConfigValidation:
    """Non-mutating. Omit ``snapshot`` to validate the current live configuration;
    the managed snapshot is never read implicitly."""
    if _explicit_null(body):
        raise OpalHTTPException(
            422, "invalid_snapshot", "explicit null snapshot is invalid; omit the field to validate the live configuration"
        )
    candidate = (
        body.snapshot
        if body and body.snapshot is not None
        else _build_snapshot(registry).model_dump(mode="json")
    )
    errors, warnings, _ = _validate_snapshot(candidate, registry)
    return ConfigValidation(valid=not errors, errors=errors, warnings=warnings)
