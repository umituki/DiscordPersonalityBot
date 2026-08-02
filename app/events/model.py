"""Event domain model (spec 8).

Invariants enforced here:

* An event is immutable once constructed (``frozen``). Corrections are new
  events (spec 8.3).
* ``occurred_at`` and ``recorded_at`` are separate, timezone-aware instants. A
  simulated past event legitimately has ``occurred_at`` far before
  ``recorded_at`` (spec 8.3).
* ``origin`` keeps real Discord history, simulated past, virtual life and admin
  operations distinguishable forever (spec 2.10 / 2.16).
* Payloads are typed and versioned. There is no untyped ``dict[str, Any]``
  domain payload (spec 38).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from app import ids
from app.clock import Clock, SystemClock, ensure_aware

EVENT_SCHEMA_VERSION = 1

EventCategory = Literal["world", "social", "internal", "action", "knowledge", "system", "admin"]
ActorType = Literal["user", "yui", "npc", "system", "admin", "world"]
TargetType = Literal["user", "yui", "npc", "system", "world", "none"]
Priority = Literal["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7"]

#: Where an event came from. Never mix these (spec 2.10, 2.16, 34.2-4).
EventOrigin = Literal["real_discord", "virtual_life", "simulated_past", "admin", "system"]

#: Categories that psychology subscribers must not receive (spec 8.1).
NON_PSYCHOLOGICAL_CATEGORIES: frozenset[str] = frozenset({"system", "admin"})


class PayloadError(ValueError):
    """Raised when a payload cannot be typed for its event."""


class EventPayload(BaseModel):
    """Base class for typed, versioned event payloads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Set by :func:`register_payload`.
    event_type: ClassVar[str] = ""
    payload_schema_version: ClassVar[int] = 1


_PAYLOAD_REGISTRY: dict[tuple[str, int], type[EventPayload]] = {}


def register_payload(event_type: str, *, version: int = 1):
    """Class decorator binding a payload model to ``event_type``/``version``."""

    def decorator(cls: type[EventPayload]) -> type[EventPayload]:
        key = (event_type, version)
        existing = _PAYLOAD_REGISTRY.get(key)
        if existing is not None and existing is not cls:
            raise PayloadError(f"payload already registered for {key}: {existing.__name__}")
        cls.event_type = event_type
        cls.payload_schema_version = version
        _PAYLOAD_REGISTRY[key] = cls
        return cls

    return decorator


def payload_model(event_type: str, version: int) -> type[EventPayload] | None:
    return _PAYLOAD_REGISTRY.get((event_type, version))


def build_payload(event_type: str, version: int, data: dict[str, Any]) -> EventPayload:
    """Rebuild a stored payload. Unknown types stay explicitly untyped."""
    model = payload_model(event_type, version)
    if model is None:
        return UnknownPayload(raw=dict(data), declared_type=event_type, declared_version=version)
    return model.model_validate(data)


class UnknownPayload(EventPayload):
    """A payload whose model is not available in this build.

    It is deliberately *not* interchangeable with a typed payload: consumers can
    see the event, but cannot pretend they understood it. This keeps old events
    readable after a schema evolution instead of failing the whole archive.
    """

    raw: dict[str, Any] = Field(default_factory=dict)
    declared_type: str
    declared_version: int


class Event(BaseModel):
    """An immutable record of one occurrence (spec 8.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: str
    category: EventCategory
    schema_version: int = EVENT_SCHEMA_VERSION
    payload_schema_version: int = 1
    occurred_at: datetime
    recorded_at: datetime
    actor_type: ActorType
    actor_id: str | None = None
    target_type: TargetType = "none"
    target_id: str | None = None
    root_event_id: str
    parent_event_id: str | None = None
    source_type: str
    source_id: str | None = None
    objective: bool = True
    priority: Priority = "P3"
    origin: EventOrigin
    #: ``SerializeAsAny`` keeps the concrete payload's fields when an event is
    #: dumped. Without it the base class schema wins and the payload silently
    #: serialises to ``{}``.
    payload: SerializeAsAny[EventPayload]

    # --- validation --------------------------------------------------------
    @model_validator(mode="before")
    @classmethod
    def _rebuild_payload(cls, data: Any) -> Any:
        """Let a dumped event validate back into a typed payload.

        ``model_dump()`` writes the concrete payload's fields, so validating the
        result must look the type up in the registry again — otherwise the base
        payload class rejects them as extra input.
        """
        if isinstance(data, dict) and isinstance(data.get("payload"), dict):
            event_type = data.get("event_type")
            if isinstance(event_type, str):
                version = int(data.get("payload_schema_version", 1) or 1)
                data = {**data, "payload": build_payload(event_type, version, data["payload"])}
        return data

    @field_validator("occurred_at", "recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @field_validator("event_type")
    @classmethod
    def _event_type_shape(cls, value: str) -> str:
        if not value or value != value.upper():
            raise ValueError("event_type must be a non-empty UPPER_SNAKE_CASE name")
        return value

    @field_validator("event_id", "root_event_id")
    @classmethod
    def _event_id_shape(cls, value: str) -> str:
        if not ids.has_prefix(value, ids.EVENT):
            raise ValueError(f"event ids must use the '{ids.EVENT}_' prefix, got {value!r}")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.parent_event_id == self.event_id:
            raise ValueError("an event cannot be its own parent")
        if self.parent_event_id is not None and not ids.has_prefix(
            self.parent_event_id, ids.EVENT
        ):
            raise ValueError("parent_event_id must be an event id")
        if self.payload.event_type and self.payload.event_type != self.event_type:
            raise ValueError(
                f"payload {type(self.payload).__name__} belongs to "
                f"{self.payload.event_type}, not {self.event_type}"
            )
        if isinstance(self.payload, UnknownPayload):
            if self.payload.declared_type != self.event_type:
                raise ValueError("unknown payload declares a different event type")
        elif self.payload.payload_schema_version != self.payload_schema_version:
            raise ValueError(
                "payload_schema_version does not match the payload model version"
            )
        if self.category in NON_PSYCHOLOGICAL_CATEGORIES and not self.objective:
            raise ValueError("system/admin events are objective records")
        return self

    # --- construction ------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        category: EventCategory,
        actor_type: ActorType,
        source_type: str,
        origin: EventOrigin,
        payload: EventPayload,
        occurred_at: datetime | None = None,
        clock: Clock | None = None,
        event_id: str | None = None,
        root_event_id: str | None = None,
        parent_event_id: str | None = None,
        actor_id: str | None = None,
        target_type: TargetType = "none",
        target_id: str | None = None,
        source_id: str | None = None,
        objective: bool = True,
        priority: Priority = "P3",
    ) -> Event:
        """Create an event, defaulting ``recorded_at`` to now and root to self."""
        now = (clock or SystemClock()).now()
        identifier = event_id or ids.new_id(ids.EVENT)
        return cls(
            event_id=identifier,
            event_type=event_type,
            category=category,
            payload_schema_version=(
                payload.declared_version
                if isinstance(payload, UnknownPayload)
                else payload.payload_schema_version
            ),
            occurred_at=occurred_at if occurred_at is not None else now,
            recorded_at=now,
            actor_type=actor_type,
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            root_event_id=root_event_id or identifier,
            parent_event_id=parent_event_id,
            source_type=source_type,
            source_id=source_id,
            objective=objective,
            priority=priority,
            origin=origin,
            payload=payload,
        )

    def child(
        self,
        *,
        event_type: str,
        category: EventCategory,
        actor_type: ActorType,
        source_type: str,
        payload: EventPayload,
        clock: Clock | None = None,
        **kwargs: Any,
    ) -> Event:
        """Derive a causally linked event in the same root chain (spec 8.3)."""
        origin = kwargs.setdefault("origin", self.origin)
        # A derived part of a simulated life happened in that life, not when
        # the simulation happened to run on the machine. Callers may still
        # provide a more precise simulated moment explicitly.
        if origin == "simulated_past":
            kwargs.setdefault("occurred_at", self.occurred_at)
        return Event.create(
            event_type=event_type,
            category=category,
            actor_type=actor_type,
            source_type=source_type,
            payload=payload,
            clock=clock,
            root_event_id=self.root_event_id,
            parent_event_id=self.event_id,
            **kwargs,
        )

    @property
    def is_root(self) -> bool:
        return self.event_id == self.root_event_id

    @property
    def is_simulated(self) -> bool:
        return self.origin == "simulated_past"

    @property
    def deliverable_to_psychology(self) -> bool:
        """Spec 8.1: admin/system events do not reach psychology subscribers."""
        return self.category not in NON_PSYCHOLOGICAL_CATEGORIES


# --- Phase 1 payloads ------------------------------------------------------


@register_payload("SYSTEM_STARTED")
class SystemStartedPayload(EventPayload):
    manifest_id: str
    schema_version_applied: int
    mode: str


@register_payload("SYSTEM_STOPPED")
class SystemStoppedPayload(EventPayload):
    reason: str


@register_payload("EVENT_INVALIDATED")
class EventInvalidatedPayload(EventPayload):
    """Spec 8.3: the only legitimate way to retract an event."""

    invalidated_event_id: str
    reason_code: str
    detail: str | None = None


@register_payload("REINTERPRETATION_CREATED")
class ReinterpretationCreatedPayload(EventPayload):
    """Spec 8.3: a later reading of an earlier event; the original stands."""

    reinterpreted_event_id: str
    reason_code: str
    detail: str | None = None
