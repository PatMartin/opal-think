"""Pydantic models mirroring ``openapi/opal_think_v1.6.0.yaml`` component schemas.

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


class TaskCreate(BaseModel):
    """Universal entry point for ask, chat, autonomous tasks and EVENT-triggered work."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=32768)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
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
    status: Literal["started", "succeeded", "failed", "timed_out", "dropped", "cancelled"]
    at: datetime


class TaskFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    status: TaskStatus
    prompt: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None
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


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None
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


# -- service info (kernel ``info`` / ``info_ping`` path items) -------------------------------------------


class ServiceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    api_version: str
    description: str | None = None


class LivenessRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"


class OpalValidationReport(BaseModel):
    """Kernel ``OpalValidationReport``: ``valid`` plus service-defined diagnostics."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    details: dict[str, Any] | None = None


# -- personas (v1.6.0; shared OPAL persona identifiers introduced by OPAL Say) ----------------------------

_PERSONA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

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


# -- profiles (v1.7.1; kernel ``OpalProfile`` family) ----------------------------------------------------
#
# A profile is a named preset of operating defaults owned by this service (the suite's
# authoritative profile registry, mirroring persona ownership). ``id``/``name``/
# ``is_active`` are suite-standard; ``settings`` is this service's compartment — a
# portable-settings preset applied on activation. Models allow extra properties so
# other OPAL services can attach their own compartments; think stores them opaquely.


class ProfileCreate(BaseModel):
    """Kernel ``OpalProfileCreate``: ``name`` required; ``is_active: true`` makes this
    the only active profile and applies its ``settings`` preset in the same write."""

    model_config = ConfigDict(extra="allow")

    id: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool = False
    settings: dict[str, Any] | None = None


class ProfilePatch(BaseModel):
    """Kernel ``OpalProfilePatch``: partial top-level merge — each supplied key
    (including foreign service compartments) replaces that value wholesale.
    ``is_active: true`` activates this profile (deactivating all others)."""

    model_config = ConfigDict(extra="allow")

    name: NonNull[str] = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None
    settings: dict[str, Any] | None = None


class Profile(BaseModel):
    """Kernel ``OpalProfile`` wire shape: suite-standard fields plus every stored
    service-defined compartment (``settings`` and foreign keys are preserved)."""

    model_config = ConfigDict(extra="allow")

    id: int = Field(ge=1)
    name: str
    description: str | None = None
    is_active: bool
    settings: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
