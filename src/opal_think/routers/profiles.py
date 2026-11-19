"""Profile endpoints (``/api/v1/profile`` family, v0.1.0 kernel v3 namespaced profiles).

A profile is a portable namespaced document: opaque string ``id``, required ``shared``
and ``modules`` maps (empty objects valid). This service is the suite's authoritative
profile registry; foreign module compartments are stored and returned verbatim. The
opal-think compartment is a closed empty schema, so activating a profile is only a
local selection change — it no longer applies a settings preset.

Profiles share the portable-configuration revision with settings and personas
(``"s<n>"``): GET returns the complete bounded collection, and every mutation
requires If-Match (428 missing, 422 malformed, 412 stale). At most one profile is
active; zero is legal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Path, Request, Response

from opal_think.deps import get_settings
from opal_think.errors import OpalHTTPException
from opal_think.preconditions import if_match_revision
from opal_think.schemas import Profile, ProfileCreate, ProfilePatch
from opal_think.settings_registry import (
    ProfileConflict,
    ProfileExists,
    ProfileMissing,
    RevisionConflict,
    SettingsRegistry,
)

router = APIRouter(prefix="/api/v1/profile", tags=["profiles"])

ProfileIdPath = Path(min_length=1)

# Generous internal ceiling for the "bounded" catalog; not a contractual limit.
_PROFILES_MAX_COUNT = 10_000


def _profile_payload(row: dict[str, Any]) -> Profile:
    return Profile.model_validate(row)


def _get_or_404(registry: SettingsRegistry, profile_id: str) -> dict[str, Any]:
    row = registry.store.get_profile(profile_id)
    if row is None:
        raise OpalHTTPException(404, "not_found", f"profile not found: {profile_id}")
    return row


def _checked(expected: int, fn) -> Any:
    """Run a checked profile mutation, mapping revision/existence failures to OpalError."""
    try:
        return fn(expect_global=expected)
    except RevisionConflict as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current configuration revision; re-read and retry",
        ) from exc
    except ProfileMissing as exc:
        raise OpalHTTPException(404, "not_found", f"profile not found: {exc}") from exc
    except (ProfileExists, ProfileConflict) as exc:
        raise OpalHTTPException(409, "conflict", str(exc)) from exc


@router.get("/", operation_id="opal_profile_list", summary="List profiles")
def list_profiles(
    response: Response,
    registry: SettingsRegistry = Depends(get_settings),
) -> list[Profile]:
    response.headers["ETag"] = registry.etag_collection()
    return [_profile_payload(row) for row in registry.store.list_profiles()]


@router.post("/", operation_id="opal_profile_create", summary="Create profile", status_code=201)
def create_profile(
    body: ProfileCreate,
    request: Request,
    response: Response,
    registry: SettingsRegistry = Depends(get_settings),
) -> Profile:
    expected = if_match_revision(request, "s")
    now = datetime.now(UTC).isoformat()
    record = {
        **body.model_dump(mode="json"),
        "created_at": now,
        "updated_at": now,
    }
    _, profile_id = _checked(expected, lambda expect_global: registry.store.insert_profile_checked(
        record, expect_global=expect_global, max_count=_PROFILES_MAX_COUNT
    ))
    response.headers["Location"] = str(request.url_for("get_profile", profile_id=profile_id))
    return _profile_payload(registry.store.get_profile(profile_id))


@router.get("/{profile_id}", operation_id="opal_profile_get", summary="Get profile")
def get_profile(
    response: Response,
    profile_id: str = ProfileIdPath,
    registry: SettingsRegistry = Depends(get_settings),
) -> Profile:
    response.headers["ETag"] = registry.etag_collection()
    return _profile_payload(_get_or_404(registry, profile_id))


@router.patch("/{profile_id}", operation_id="opal_profile_patch", summary="Update profile")
def update_profile(
    body: ProfilePatch,
    request: Request,
    profile_id: str = ProfileIdPath,
    registry: SettingsRegistry = Depends(get_settings),
) -> Profile:
    expected = if_match_revision(request, "s")
    changes = body.model_dump(mode="json", exclude_unset=True)
    _checked(expected, lambda expect_global: registry.store.update_profile_checked(
        profile_id, changes, expect_global=expect_global
    ))
    return _profile_payload(_get_or_404(registry, profile_id))


@router.delete("/{profile_id}", operation_id="opal_profile_delete", summary="Delete profile", status_code=204)
def delete_profile(
    request: Request,
    profile_id: str = ProfileIdPath,
    registry: SettingsRegistry = Depends(get_settings),
) -> Response:
    expected = if_match_revision(request, "s")
    _checked(expected, lambda expect_global: registry.store.delete_profile_checked(
        profile_id, expect_global=expect_global
    ))
    return Response(status_code=204)


@router.post("/{profile_id}/activate", operation_id="opal_profile_activate", summary="Activate profile")
def activate_profile(
    request: Request,
    profile_id: str = ProfileIdPath,
    registry: SettingsRegistry = Depends(get_settings),
) -> Profile:
    """Make this profile the only active one (local selection; the empty opal-think
    compartment means no settings change)."""
    expected = if_match_revision(request, "s")
    _checked(expected, lambda expect_global: registry.store.activate_profile_checked(
        profile_id, expect_global=expect_global
    ))
    return _profile_payload(_get_or_404(registry, profile_id))
