"""Settings endpoints (``/api/v1/settings/`` and ``/api/v1/settings/{key}``).

Portable settings with strong ETags. Collection GET/PATCH use the global revision ETag
(``"s<n>"``); single-key GET/PUT/DELETE use the per-key revision ETag (``"k<n>"``).
Mutations require If-Match: missing -> 428, stale -> 412. Unknown keys -> 404 (single)
or 422 (inside a PATCH payload). Secret settings carry secret references, never raw values.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Request, Response

from opal_think.deps import get_settings
from opal_think.errors import OpalHTTPException
from opal_think.schemas import Setting, SettingsPatch, SettingsSnapshot, SettingUpdate
from opal_think.settings_registry import RevisionConflict, SettingsRegistry

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

_KEY_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]*$"
SettingKey = Path(min_length=1, max_length=200, pattern=_KEY_PATTERN)


def _strip_etag(value: str | None) -> str | None:
    return value.strip('"') if value is not None else None


def _require_if_match(request: Request) -> str:
    raw = request.headers.get("If-Match")
    if raw is None:
        raise OpalHTTPException(428, "precondition_required", "If-Match header is required for settings mutations")
    return _strip_etag(raw) or ""


def _setting_payload(item: dict[str, Any]) -> Setting:
    return Setting(
        key=item["key"],
        value=item["value"],
        default=item["default"],
        is_default=item["is_default"],
        pending_restart=item["pending_restart"],
    )


def _snapshot_payload(registry: SettingsRegistry) -> tuple[SettingsSnapshot, str]:
    items, revision, pending_restart = registry.snapshot()
    return (
        SettingsSnapshot(items=[_setting_payload(item) for item in items], pending_restart_fields=pending_restart),
        f'"s{revision}"',
    )


def _get_or_404(registry: SettingsRegistry, key: str) -> dict[str, Any]:
    item = registry.get(key)
    if item is None:
        raise OpalHTTPException(404, "not_found", f"unknown setting key: {key}")
    return item


def _if_match_revision(request: Request, prefix: str) -> int:
    """Parse the If-Match precondition into the expected revision integer.

    The comparison itself happens inside the store's write transaction — this
    only enforces header presence and shape (428 missing, 412 malformed)."""
    supplied = _require_if_match(request)
    if not supplied.startswith(prefix):
        raise OpalHTTPException(412, "precondition_failed", "If-Match does not match the current settings revision")
    try:
        return int(supplied[len(prefix) :])
    except ValueError as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current settings revision; re-read settings and retry",
        ) from exc


@router.get("/", operation_id="list_settings", summary="List settings")
def list_settings(response: Response, registry: SettingsRegistry = Depends(get_settings)) -> SettingsSnapshot:
    snapshot, etag = _snapshot_payload(registry)
    response.headers["ETag"] = etag
    return snapshot


@router.patch("/", operation_id="patch_settings", summary="Atomically patch settings")
def patch_settings(
    request: Request,
    response: Response,
    patch: SettingsPatch,
    registry: SettingsRegistry = Depends(get_settings),
) -> SettingsSnapshot:
    expected = _if_match_revision(request, "s")
    errors = [
        {"key": key, "reason": reason}
        for key, value in patch.values.items()
        if (reason := registry.validate_value(key, value)) is not None
    ]
    if errors:
        raise OpalHTTPException(422, "invalid_settings", "one or more settings failed validation", details=errors)
    try:
        registry.store.set_many_checked(dict(patch.values), expect_global=expected)
    except RevisionConflict as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current settings revision; re-read settings and retry",
        ) from exc
    snapshot, etag = _snapshot_payload(registry)
    response.headers["ETag"] = etag
    return snapshot


@router.get("/{key}", operation_id="show_setting", summary="Show setting")
def show_setting(
    response: Response,
    key: str = SettingKey,
    registry: SettingsRegistry = Depends(get_settings),
) -> Setting:
    item = _get_or_404(registry, key)
    response.headers["ETag"] = SettingsRegistry.etag_key(item["revision"])
    return _setting_payload(item)


@router.put("/{key}", operation_id="set_setting", summary="Set one setting")
def set_setting(
    request: Request,
    response: Response,
    body: SettingUpdate,
    key: str = SettingKey,
    registry: SettingsRegistry = Depends(get_settings),
) -> Setting:
    _get_or_404(registry, key)
    expected = _if_match_revision(request, "k")
    if (reason := registry.validate_value(key, body.value)) is not None:
        raise OpalHTTPException(422, "invalid_settings", f"invalid value for {key}: {reason}")
    try:
        registry.store.set_many_checked({key: body.value}, key=key, expect_key=expected)
    except RevisionConflict as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current setting revision; re-read the setting and retry",
        ) from exc
    updated = _get_or_404(registry, key)
    response.headers["ETag"] = SettingsRegistry.etag_key(updated["revision"])
    return _setting_payload(updated)


@router.delete("/{key}", operation_id="remove_setting_override", summary="Remove setting override", status_code=204)
def remove_setting_override(
    request: Request,
    key: str = SettingKey,
    registry: SettingsRegistry = Depends(get_settings),
) -> Response:
    _get_or_404(registry, key)
    expected = _if_match_revision(request, "k")
    try:
        registry.store.reset_checked(key, expect_key=expected)
    except RevisionConflict as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current setting revision; re-read the setting and retry",
        ) from exc
    return Response(status_code=204, headers={"ETag": SettingsRegistry.etag_key(0)})
