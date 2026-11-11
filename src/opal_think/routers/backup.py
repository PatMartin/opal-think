"""``/api/v1/backup``: portable config export/validate/import plus service-managed
``.tar.gz`` archives (list/create/download/restore/delete). The bundle is the kernel
``OpalConfigExport`` shape — ``format_version``, ``exported_at``, this service's
portable sections (``settings`` overrides, ``personas``, ``profiles``) — and never
carries secrets or history. Import/restore replace the whole portable configuration
atomically under a mandatory strong If-Match (428 missing, 412 stale, 409 invalid).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import FileResponse

from opal_think.backups import backup_dir, backup_path, create_backup, list_backups, read_bundle
from opal_think.deps import get_config, get_settings
from opal_think.errors import OpalHTTPException
from opal_think.preconditions import if_match_revision
from opal_think.schemas import (
    BackupCreateResponse,
    BackupInfo,
    ConfigExport,
    ImportResult,
    OpalValidationReport,
)
from opal_think.settings_registry import RevisionConflict, SettingsRegistry, SettingsStore

router = APIRouter(prefix="/api/v1/backup", tags=["backup"])

_BUNDLE_VERSION = "1.0.0"
_PORTABLE_SECTIONS = {"format_version", "exported_at", "settings", "personas", "profiles"}


def _bundle(registry: SettingsRegistry) -> dict[str, Any]:
    """The portable configuration as a wire bundle: settings *overrides* (not
    effective values, so is_default flags survive a round-trip) plus the full
    persona and profile records."""
    overrides = {key: value for key, (value, _rev) in registry.store.all_overrides().items()}
    return {
        "format_version": _BUNDLE_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "settings": overrides,
        "personas": registry.store.list_personas(),
        "profiles": registry.store.list_profiles(),
    }


def _validate_bundle(bundle: ConfigExport, registry: SettingsRegistry) -> OpalValidationReport:
    """Non-mutating bundle validation: settings keys/values, persona/profile identity
    and preset integrity, the single-active-profile invariant. Foreign sections are
    warned-and-ignored (this service owns settings/personas/profiles only)."""
    errors: list[str] = []
    warnings: list[str] = []
    for key, value in bundle.settings.items():
        if key == "pending_restart_fields":
            errors.append("setting 'pending_restart_fields' is a reserved, read-only key")
        elif (reason := registry.validate_value(key, value)) is not None:
            errors.append(f"setting '{key}': {reason}")
    seen: set[str] = set()
    for persona in bundle.personas:
        pid = persona.get("persona_id")
        if not pid:
            errors.append("persona is missing required 'persona_id'")
        elif pid in seen:
            errors.append(f"duplicate persona id: {pid}")
        else:
            seen.add(pid)
        if not persona.get("name"):
            errors.append(f"persona '{pid or '?'}' is missing required 'name'")
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    active = 0
    for profile in bundle.profiles:
        pid, name = profile.get("id"), profile.get("name")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
            errors.append(f"profile has missing/invalid 'id': {pid!r}")
        elif pid in seen_ids:
            errors.append(f"duplicate profile id: {pid}")
        else:
            seen_ids.add(pid)
        if not name:
            errors.append(f"profile {pid or '?'} is missing required 'name'")
        elif name in seen_names:
            errors.append(f"duplicate profile name: {name}")
        else:
            seen_names.add(name)
        if profile.get("is_active"):
            active += 1
        errors.extend(
            f"profile '{name or pid}': {e['key']}: {e['reason']}"
            for e in SettingsStore._preset_errors(profile)
        )
    if active > 1:
        errors.append("more than one active profile: exactly one may be active")
    for key in sorted(bundle.model_extra or {}):
        warnings.append(f"section '{key}' is not used by this service; ignored on import")
    return OpalValidationReport(valid=not errors, errors=errors, warnings=warnings)


def _archive_dir(config) -> Any:
    return backup_dir(config.db_path)


def _apply_bundle(registry: SettingsRegistry, bundle: ConfigExport, expected: int) -> int:
    """Atomic replace of the portable sections; maps a stale precondition to 412."""
    try:
        return registry.store.replace_portable_config_checked(
            settings=bundle.settings,
            personas=bundle.personas,
            profiles=bundle.profiles,
            expect_global=expected,
        )
    except RevisionConflict as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current configuration revision; re-read and retry",
        ) from exc


def _import_result(registry: SettingsRegistry, bundle: ConfigExport, revision: int, warnings: list[str]) -> ImportResult:
    _, _, pending_restart = registry.snapshot()
    return ImportResult(
        revision=f'"s{revision}"',
        counts={
            "settings": len(bundle.settings),
            "personas": len(bundle.personas),
            "profiles": len(bundle.profiles),
        },
        warnings=warnings,
        pending_restart=bool(pending_restart),
        message="portable configuration imported",
    )


# -- logical config bundle -----------------------------------------------------


@router.get("/export", operation_id="opal_backup_export", summary="Export configuration")
def export_backup(registry: SettingsRegistry = Depends(get_settings)) -> dict[str, Any]:
    return _bundle(registry)


@router.post("/validate", operation_id="opal_backup_validate_post", summary="Validate configuration")
def validate_backup(
    bundle: ConfigExport,
    registry: SettingsRegistry = Depends(get_settings),
) -> OpalValidationReport:
    return _validate_bundle(bundle, registry)


@router.post("/import", operation_id="opal_backup_import", summary="Import configuration")
def import_backup(
    request: Request,
    bundle: ConfigExport | None = Body(default=None),
    registry: SettingsRegistry = Depends(get_settings),
) -> ImportResult:
    """Atomically import a config bundle. An empty body re-validates and re-applies
    the persisted configuration itself — think's portable state lives in SQLite, so
    the implementation's "config file set" is the store: a self round-trip."""
    expected = if_match_revision(request, "s")
    if bundle is None:
        bundle = ConfigExport.model_validate(_bundle(registry))
    report = _validate_bundle(bundle, registry)
    if not report.valid:
        raise OpalHTTPException(
            409, "conflict", f"backup failed validation: {'; '.join(report.errors)}", details=report.errors
        )
    revision = _apply_bundle(registry, bundle, expected)
    return _import_result(registry, bundle, revision, report.warnings)


# -- service-managed archives ----------------------------------------------------


@router.get("/list", operation_id="opal_backup_list", summary="List backups")
def list_backup_archives(config=Depends(get_config)) -> list[BackupInfo]:
    return list_backups(_archive_dir(config))


@router.post("/create", operation_id="opal_backup_create", summary="Create backup", status_code=201)
def create_backup_archive(
    response: Response,
    config=Depends(get_config),
    registry: SettingsRegistry = Depends(get_settings),
) -> BackupCreateResponse:
    try:
        filename = create_backup(_archive_dir(config), _bundle(registry))
    except OSError as exc:
        raise OpalHTTPException(500, "operation_failed", f"failed to create backup: {exc}") from exc
    response.headers["Location"] = f"/api/v1/backup/download/{filename}"
    return BackupCreateResponse(filename=filename, message="Backup created successfully.")


@router.get("/download/{filename}", operation_id="opal_backup_download", summary="Download backup")
def download_backup(filename: str, config=Depends(get_config)) -> FileResponse:
    path = backup_path(_archive_dir(config), filename)
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


@router.post("/restore/{filename}", operation_id="opal_backup_restore", summary="Restore backup")
def restore_backup(
    request: Request,
    filename: str,
    config=Depends(get_config),
    registry: SettingsRegistry = Depends(get_settings),
) -> ImportResult:
    """Validate an archive's bundle then apply it atomically, like ``/backup/import``."""
    expected = if_match_revision(request, "s")
    raw = read_bundle(_archive_dir(config), filename)
    try:
        bundle = ConfigExport.model_validate(raw)
    except Exception as exc:
        raise OpalHTTPException(409, "conflict", f"backup '{filename}' is not a valid config bundle: {exc}") from exc
    report = _validate_bundle(bundle, registry)
    if not report.valid:
        raise OpalHTTPException(
            409,
            "conflict",
            f"backup '{filename}' failed validation: {'; '.join(report.errors)}",
            details=report.errors,
        )
    revision = _apply_bundle(registry, bundle, expected)
    return _import_result(registry, bundle, revision, report.warnings)


@router.delete("/{filename}", operation_id="opal_backup_delete", summary="Delete backup", status_code=204)
def delete_backup(filename: str, config=Depends(get_config)) -> Response:
    backup_path(_archive_dir(config), filename).unlink()
    return Response(status_code=204)
