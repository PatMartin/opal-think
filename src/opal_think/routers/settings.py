"""Settings endpoints (``/api/v1/setting/`` and ``/api/v1/setting/{key}``).

Portable settings with strong ETags. Collection GET/PATCH use the global revision ETag
(``"s<n>"``); single-key GET/PUT/DELETE use the per-key revision ETag (``"k<n>"``).
Mutations require If-Match: missing -> 428, malformed -> 422, stale -> 412. Unknown keys
-> 404 (single) or 422 (inside a PATCH payload). Secret settings carry secret references,
never raw values. The collection wire shape is the kernel ``OpalSettingsMap``: a flat
dotted-key map with ``pending_restart_fields`` as a reserved, server-managed key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Request, Response

from opal_think.deps import get_settings
from opal_think.errors import OpalHTTPException
from opal_think.preconditions import if_match_revision
from opal_think.schemas import Setting, SettingUpdate
from opal_think.settings_registry import RevisionConflict, SettingsRegistry

router = APIRouter(prefix="/api/v1/setting", tags=["settings"])

_KEY_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]*$"
SettingKey = Path(min_length=1, max_length=200, pattern=_KEY_PATTERN)


def _setting_payload(item: dict[str, Any]) -> Setting:
    return Setting(
        key=item["key"],
        value=item["value"],
        default=item["default"],
        is_default=item["is_default"],
        pending_restart=item["pending_restart"],
    )


def _map_payload(registry: SettingsRegistry) -> tuple[dict[str, Any], str]:
    """Kernel ``OpalSettingsMap``: flat ``key -> value`` plus the reserved
    ``pending_restart_fields`` key naming settings that need a restart."""
    items, revision, pending_restart = registry.snapshot()
    settings_map = {item["key"]: item["value"] for item in items}
    settings_map["pending_restart_fields"] = pending_restart
    return settings_map, f'"s{revision}"'


def _get_or_404(registry: SettingsRegistry, key: str) -> dict[str, Any]:
    item = registry.get(key)
    if item is None:
        raise OpalHTTPException(404, "not_found", f"unknown setting key: {key}")
    return item


@router.get("/", operation_id="opal_setting_list", summary="List settings")
def list_settings(response: Response, registry: SettingsRegistry = Depends(get_settings)) -> dict[str, Any]:
    settings_map, etag = _map_payload(registry)
    response.headers["ETag"] = etag
    return settings_map


@router.patch("/", operation_id="opal_setting_patch", summary="Atomically patch settings")
def patch_settings(
    request: Request,
    response: Response,
    patch: dict[str, Any],
    registry: SettingsRegistry = Depends(get_settings),
) -> dict[str, Any]:
    expected = if_match_revision(request, "s")
    if "pending_restart_fields" in patch:
        raise OpalHTTPException(422, "invalid_settings", "pending_restart_fields is a reserved, read-only key")
    errors = [
        {"key": key, "reason": reason}
        for key, value in patch.items()
        if (reason := registry.validate_value(key, value)) is not None
    ]
    if errors:
        raise OpalHTTPException(422, "invalid_settings", "one or more settings failed validation", details=errors)
    if patch:
        try:
            registry.store.set_many_checked(dict(patch), expect_global=expected)
        except RevisionConflict as exc:
            raise OpalHTTPException(
                412,
                "precondition_failed",
                "If-Match does not match the current settings revision; re-read settings and retry",
            ) from exc
    settings_map, etag = _map_payload(registry)
    response.headers["ETag"] = etag
    return settings_map


@router.get("/{key}", operation_id="opal_setting_get", summary="Show setting")
def show_setting(
    response: Response,
    key: str = SettingKey,
    registry: SettingsRegistry = Depends(get_settings),
) -> Setting:
    item = _get_or_404(registry, key)
    response.headers["ETag"] = SettingsRegistry.etag_key(item["revision"])
    return _setting_payload(item)


@router.put("/{key}", operation_id="opal_setting_put", summary="Set one setting")
def set_setting(
    request: Request,
    response: Response,
    body: SettingUpdate,
    key: str = SettingKey,
    registry: SettingsRegistry = Depends(get_settings),
) -> Setting:
    _get_or_404(registry, key)
    expected = if_match_revision(request, "k")
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


@router.delete("/{key}", operation_id="opal_setting_delete", summary="Remove setting override", status_code=204)
def remove_setting_override(
    request: Request,
    key: str = SettingKey,
    registry: SettingsRegistry = Depends(get_settings),
) -> Response:
    _get_or_404(registry, key)
    expected = if_match_revision(request, "k")
    try:
        registry.store.reset_checked(key, expect_key=expected)
    except RevisionConflict as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current setting revision; re-read the setting and retry",
        ) from exc
    return Response(status_code=204, headers={"ETag": SettingsRegistry.etag_key(0)})
