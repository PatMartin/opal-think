"""Pydantic models mirroring ``openapi/opal_think_v0.5.0.yaml`` component schemas.

The OpenAPI contract is authoritative; these models implement it exactly:
``additionalProperties: false`` maps to ``extra="forbid"``, required fields are non-defaulted,
and enumerated strings use ``Literal``/``StrEnum``.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

_METADATA_MAX_PROPERTIES = 32
_METADATA_MAX_BYTES = 8192
_METADATA_MAX_DEPTH = 8
_SENSITIVE_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "authorization",
)


def _check_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Bounds caller-supplied tracking metadata: it is data only — never fed to the model,
    never an authorization source. Enforces contract maxProperties plus implementation
    size/depth/sensitive-content bounds required by the contract description."""

    def walk(node: Any, depth: int) -> None:
        if depth > _METADATA_MAX_DEPTH:
            raise ValueError(f"metadata exceeds maximum depth {_METADATA_MAX_DEPTH}")
        if isinstance(node, dict):
            for key, child in node.items():
                lowered = str(key).lower()
                if any(hint in lowered for hint in _SENSITIVE_KEY_HINTS):
                    raise ValueError(f"metadata key {key!r} looks like sensitive content; credentials are forbidden")
                walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)

    if len(value) > _METADATA_MAX_PROPERTIES:
        raise ValueError(f"metadata exceeds maximum {_METADATA_MAX_PROPERTIES} top-level properties")
    walk(value, 1)
    if len(json.dumps(value, default=str).encode("utf-8")) > _METADATA_MAX_BYTES:
        raise ValueError(f"metadata exceeds maximum serialized size {_METADATA_MAX_BYTES} bytes")
    return value


class TaskStatus(StrEnum):
    """Terminal states are completed, failed, cancelled and timed_out."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_TASK_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT}

# Kernel ``OpalPersonaRef``: globally unique OPAL persona identifier.
_PERSONA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class TaskCreate(BaseModel):
    """Universal entry point for ask, chat, autonomous tasks and EVENT-triggered work."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=32768)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    persona_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=_PERSONA_ID_PATTERN)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    context: dict[str, Any] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=100)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    metadata: dict[str, Any] | None = None

    _metadata_bounded = field_validator("metadata")(_check_metadata)


class TaskStep(BaseModel):
    """Bounded, redacted task activity summary. EVENT is authoritative for action details."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    kind: Literal["reasoning", "action", "result"]
    summary: str
    action_id: str | None = None
    execution_id: str | None = None
    action_input: dict[str, Any] | None = None
    status: Literal["running", "completed", "failed", "timed_out", "dropped", "cancelled"]
    at: datetime


class TaskFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class TaskPersona(BaseModel):
    """Resolved persona selection persisted at admission for inspection and
    reproducibility: which persona ran, under which portable-config revision
    (the ``"s<n>"`` ETag basis), and the binding snapshot that was applied.
    Later persona or binding edits do not alter this record."""

    model_config = ConfigDict(extra="forbid")

    persona_id: str
    revision: int = Field(ge=0)
    snapshot: dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    status: TaskStatus
    prompt: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None
    persona: TaskPersona | None = None
    model_id: str
    max_steps: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)
    steps_completed: int = Field(ge=0)
    cancel_requested: bool
    answer: str | None
    error: TaskFailure | None
    steps: list[TaskStep]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SessionCreate(BaseModel):
    """Create an optional conversation context; does not start a task."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    persona_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=_PERSONA_ID_PATTERN)


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None
    persona_id: str | None = None
    created_at: datetime
    updated_at: datetime
    task_count: int = Field(ge=0)


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streaming: bool
    tool_calling: bool
    structured_output: bool
    vision: bool


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    name: str
    available: bool
    capabilities: ModelCapabilities
    context_window: int | None = Field(ge=1)
    description: str | None


class Setting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any = None
    default: Any = None
    is_default: bool
    pending_restart: bool


class SettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any  # required; may still be JSON null


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "unavailable"]
    ready: bool
    models_available: int = Field(ge=0)
    event: Literal["available", "degraded", "unavailable", "not_configured"]
    active_tasks: int = Field(ge=0)
    pending_restart_fields: list[str]


class HealthProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "unavailable"]
    ready: bool
    details: dict[str, Any] | None = None


class OpalValidationReport(BaseModel):
    """Kernel ``OpalValidationReport``: ``valid`` plus service-defined diagnostics."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    details: dict[str, Any] | None = None


# -- service lifecycle (kernel ``service_*`` path items) -------------------------------------------------


class ServiceStartOptions(BaseModel):
    """Kernel ``OpalServiceStartOptions``."""

    model_config = ConfigDict(extra="forbid")

    append: bool = False


class ServiceStatus(BaseModel):
    """Kernel ``OpalServiceStatus``."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["running", "stopped"]
    pid: int | None = None
    previous_pid: int | None = None
    log_file: str | None = None


class ServiceLogTail(BaseModel):
    """Kernel ``OpalServiceLogTail``."""

    model_config = ConfigDict(extra="forbid")

    log_file: str | None = None
    lines: list[str] = Field(default_factory=list)


# -- configuration snapshots (kernel ``config_*`` path items, v0.1.0) ------------------------------------

SNAPSHOT_FORMAT_VERSION = "1.0.0"
SNAPSHOT_MODULE = "opal-think"
SNAPSHOT_RESOURCE_SECTIONS = ("profiles", "personas", "models", "engines")


class OpalValidationIssue(BaseModel):
    """Kernel ``OpalValidationIssue``."""

    model_config = ConfigDict(extra="allow")

    code: str
    path: str
    message: str


class ConfigSnapshot(BaseModel):
    """Kernel ``OpalConfigSnapshot``: closed envelope carrying flat setting-key
    overrides plus declared declarative-resource sections."""

    model_config = ConfigDict(extra="forbid")

    format_version: str = SNAPSHOT_FORMAT_VERSION
    module: str = SNAPSHOT_MODULE
    created_at: datetime
    settings: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)


class ConfigSaveRequest(BaseModel):
    """Kernel ``OpalConfigSaveRequest``."""

    model_config = ConfigDict(extra="forbid")

    destination: Literal["managed", "response"] = "managed"


class ConfigSaveResult(BaseModel):
    """Kernel ``OpalConfigSaveResult``."""

    model_config = ConfigDict(extra="forbid")

    snapshot: ConfigSnapshot
    managed: bool


class ConfigLoadRequest(BaseModel):
    """Kernel ``OpalConfigLoadRequest``. ``snapshot`` is typed loosely so an
    explicit ``null`` can be reported as a structured 422 rather than a schema
    rejection; omit (or send ``{}``) to load the managed snapshot."""

    model_config = ConfigDict(extra="forbid")

    snapshot: dict[str, Any] | None = None


class ConfigLoadResult(BaseModel):
    """Kernel ``OpalConfigLoadResult``."""

    model_config = ConfigDict(extra="forbid")

    applied: Literal[True] = True
    pending_restart_fields: list[str] = Field(default_factory=list)
    warnings: list[OpalValidationIssue] = Field(default_factory=list)


class ConfigValidation(BaseModel):
    """Kernel ``OpalConfigValidation``."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[OpalValidationIssue] = Field(default_factory=list)
    warnings: list[OpalValidationIssue] = Field(default_factory=list)


# -- personas (v1.7.1; shared OPAL persona identifiers introduced by OPAL Say) ----------------------------

_T = TypeVar("_T")


def _reject_null(value: Any) -> Any:
    if value is None:
        raise ValueError("field may not be null")
    return value


NonNull = Annotated[_T | None, BeforeValidator(_reject_null)]


class PersonaCreate(BaseModel):
    """Register a persona: a named assistant identity; ``voice_id`` binds an OPAL Say voice."""

    model_config = ConfigDict(extra="forbid")

    persona_id: str = Field(min_length=1, max_length=200, pattern=_PERSONA_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    voice_id: str | None = Field(default=None, min_length=1)
    enabled: bool = True


class PersonaUpdate(BaseModel):
    """Partial persona update (contract ``minProperties: 1``). ``description`` and
    ``voice_id`` accept explicit ``null`` to clear the field; ``name``/``enabled`` do not."""

    model_config = ConfigDict(extra="forbid")

    name: NonNull[str] = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    voice_id: str | None = Field(default=None, min_length=1)
    enabled: NonNull[bool] = None

    @model_validator(mode="after")
    def _at_least_one(self) -> PersonaUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one property is required")
        return self


class PersonaRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona_id: str
    name: str
    description: str | None = None
    voice_id: str | None = None
    enabled: bool
    origin: Literal["local", "owner"] = "local"
    created_at: datetime
    updated_at: datetime


# -- profiles (v0.1.0 kernel ``OpalProfile`` family: namespaced ``shared`` + ``modules``) --------------
#
# A profile is a portable namespaced document: opaque string ``id``, required
# ``shared`` and ``modules`` sections (empty objects valid). The opal-think
# compartment is a closed empty schema — activation is a local selection only and
# changes no live settings; foreign module compartments are stored and returned
# verbatim. ``is_active`` is a local projection: at most one profile is active.

PROFILE_COMPARTMENT = "opal-think"

# Suite-standard top-level fields; anything else in a stored v2 record is a foreign
# compartment that migrates under ``modules``.
_PROFILE_ENVELOPE_FIELDS = {
    "id", "name", "description", "is_active", "shared", "modules", "created_at", "updated_at",
}


def _check_profile_envelope(shared: dict | None, modules: dict | None, patch: bool = False) -> None:
    """Closed-envelope checks shared by create/patch payloads (kernel
    ``OpalProfileShared``/``OpalProfileModules(Patch)`` + ``x-opal-profiles``)."""
    if shared:
        raise ValueError("'shared' is a closed empty object; members are rejected")
    for key, compartment in (modules or {}).items():
        if not isinstance(compartment, dict):
            raise ValueError(f"module compartment '{key}' must be an object")
        if patch and key != PROFILE_COMPARTMENT:
            raise ValueError(
                f"foreign compartment '{key}' is read-only; patches may only target '{PROFILE_COMPARTMENT}'"
            )
        if key == PROFILE_COMPARTMENT and compartment:
            raise ValueError(f"'modules.{PROFILE_COMPARTMENT}' is a closed empty schema; members are rejected")


class ProfileCreate(BaseModel):
    """Kernel ``OpalProfileCreate``: ``name`` required; omitted ``shared``/``modules``
    normalize to empty objects. Foreign compartments are stored verbatim;
    ``is_active: true`` activates the new profile and deactivates all others."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool = False
    shared: dict[str, Any] = Field(default_factory=dict)
    modules: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _envelope(self) -> ProfileCreate:
        _check_profile_envelope(self.shared, self.modules)
        return self


class ProfilePatch(BaseModel):
    """Kernel ``OpalProfilePatch``: partial update; compartment values merge
    field-wise over the stored compartment. ``modules`` may contain only the
    receiving service's own compartment key — foreign edits are rejected (422)."""

    model_config = ConfigDict(extra="forbid")

    name: NonNull[str] = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None
    shared: dict[str, Any] | None = None
    modules: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ProfilePatch:
        if not self.model_fields_set:
            raise ValueError("at least one property is required")
        _check_profile_envelope(self.shared, self.modules, patch=True)
        return self


class Profile(BaseModel):
    """Kernel ``OpalProfile`` wire shape: opaque string ``id``, required ``shared``
    and ``modules`` (empty objects valid). v1.7.1 stored records (integer ``id``,
    flat ``settings`` preset compartment, top-level foreign compartments) upgrade
    on read: ids stringify, ``settings`` is discarded (the v3 opal-think
    compartment declares no fields to carry it), and foreign top-level keys fold
    under ``modules``."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str
    description: str | None = None
    is_active: bool
    shared: dict[str, Any] = Field(default_factory=dict)
    modules: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _migrate_v2(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            data.pop("settings", None)  # v2 preset compartment; dropped in v3
            data.setdefault("shared", {})
            data.setdefault("modules", {})
            if not isinstance(data["modules"], dict):
                data["modules"] = {}
            if isinstance(data.get("id"), int):
                data["id"] = str(data["id"])
            # v2 stored foreign compartments as extra top-level keys; they live
            # under ``modules`` in v3.
            for key in [k for k in data if k not in _PROFILE_ENVELOPE_FIELDS]:
                data["modules"].setdefault(key, data.pop(key))
        return data
