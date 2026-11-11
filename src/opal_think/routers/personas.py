"""Persona endpoints (``/api/v1/persona`` family, v1.7.1).

A persona is a named assistant identity for conversation sessions and tasks; an optional
``voice_id`` binds an OPAL Say voice identifier (resolved by Say, not verified locally).
Personas are a bounded configuration catalog: GET returns the complete collection (no
pagination) with the shared portable-configuration ETag (``"s<n>"``), and every mutation
requires If-Match (428 missing, 422 malformed, 412 stale).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from opal_think.deps import get_settings
from opal_think.errors import OpalHTTPException
from opal_think.preconditions import if_match_revision
from opal_think.schemas import OpalValidationReport, PersonaCreate, PersonaRead, PersonaUpdate
from opal_think.settings_registry import PersonaExists, PersonaMissing, RevisionConflict, SettingsRegistry

router = APIRouter(prefix="/api/v1/persona", tags=["persona"])

_PERSONA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
PersonaId = Path(min_length=1, max_length=200, pattern=_PERSONA_ID_PATTERN)

# Generous internal ceiling for the "bounded" catalog; not a contractual limit.
_PERSONAS_MAX_COUNT = 10_000


def _persona_payload(row: dict[str, Any]) -> PersonaRead:
    return PersonaRead(
        persona_id=row["persona_id"],
        name=row["name"],
        description=row["description"],
        voice_id=row["voice_id"],
        enabled=row["enabled"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _get_or_404(registry: SettingsRegistry, persona_id: str) -> dict[str, Any]:
    row = registry.store.get_persona(persona_id)
    if row is None:
        raise OpalHTTPException(404, "not_found", f"persona not found: {persona_id}")
    return row


def _checked(expected: int, fn) -> Any:
    """Run a checked persona mutation, mapping revision/existence failures to OpalError."""
    try:
        return fn(expect_global=expected)
    except RevisionConflict as exc:
        raise OpalHTTPException(
            412,
            "precondition_failed",
            "If-Match does not match the current configuration revision; re-read and retry",
        ) from exc
    except PersonaMissing as exc:
        raise OpalHTTPException(404, "not_found", f"persona not found: {exc}") from exc
    except PersonaExists as exc:
        raise OpalHTTPException(409, "conflict", f"persona registration conflict: {exc}") from exc


@router.get("/", operation_id="opal_persona_list", summary="List Personas")
def list_personas(
    response: Response,
    enabled: bool | None = Query(default=None),
    registry: SettingsRegistry = Depends(get_settings),
) -> list[PersonaRead]:
    response.headers["ETag"] = registry.etag_collection()
    return [_persona_payload(row) for row in registry.store.list_personas(enabled)]


@router.post("/", operation_id="opal_persona_create", summary="Create Persona", status_code=201)
def create_persona(
    body: PersonaCreate,
    request: Request,
    response: Response,
    registry: SettingsRegistry = Depends(get_settings),
) -> PersonaRead:
    expected = if_match_revision(request, "s")
    now = datetime.now(UTC).isoformat()
    row = {
        **body.model_dump(),
        "created_at": now,
        "updated_at": now,
    }
    _checked(expected, lambda expect_global: registry.store.insert_persona_checked(
        row, expect_global=expect_global, max_count=_PERSONAS_MAX_COUNT
    ))
    response.headers["Location"] = str(request.url_for("get_persona", persona_id=body.persona_id))
    return _persona_payload(registry.store.get_persona(body.persona_id))


@router.get("/{persona_id}", operation_id="opal_persona_get", summary="Get Persona")
def get_persona(
    response: Response,
    persona_id: str = PersonaId,
    registry: SettingsRegistry = Depends(get_settings),
) -> PersonaRead:
    response.headers["ETag"] = registry.etag_collection()
    return _persona_payload(_get_or_404(registry, persona_id))


@router.post("/{persona_id}/validate", operation_id="opal_persona_validate", summary="Validate Persona")
def validate_persona(
    persona_id: str = PersonaId,
    registry: SettingsRegistry = Depends(get_settings),
) -> OpalValidationReport:
    persona = _get_or_404(registry, persona_id)
    errors: list[str] = []
    warnings: list[str] = []
    if not persona["enabled"]:
        errors.append(f"persona '{persona_id}' is disabled")
    if persona["voice_id"]:
        warnings.append(f"voice binding '{persona['voice_id']}' is resolved by OPAL Say, not verified locally")
    return OpalValidationReport(valid=not errors, errors=errors, warnings=warnings)


@router.patch("/{persona_id}", operation_id="opal_persona_patch", summary="Update Persona")
def update_persona(
    body: PersonaUpdate,
    request: Request,
    persona_id: str = PersonaId,
    registry: SettingsRegistry = Depends(get_settings),
) -> PersonaRead:
    expected = if_match_revision(request, "s")
    changes = body.model_dump(mode="json", exclude_unset=True)
    _checked(expected, lambda expect_global: registry.store.update_persona_checked(
        persona_id, changes, expect_global=expect_global
    ))
    return _persona_payload(_get_or_404(registry, persona_id))


@router.delete("/{persona_id}", operation_id="opal_persona_delete", summary="Delete Persona", status_code=204)
def delete_persona(
    request: Request,
    persona_id: str = PersonaId,
    registry: SettingsRegistry = Depends(get_settings),
) -> Response:
    expected = if_match_revision(request, "s")
    _checked(expected, lambda expect_global: registry.store.delete_persona_checked(
        persona_id, expect_global=expect_global
    ))
    return Response(status_code=204)
