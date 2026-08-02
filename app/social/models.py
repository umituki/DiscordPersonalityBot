"""Belief and self-model domain objects (spec 12.4, 25)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware
from app.events.model import EventOrigin

#: Spec 25 provenance stance. Evidence exists in both directions.
Stance = Literal["supports", "contradicts"]

#: Spec 25 source types.
SourceType = Literal[
    "real_user_message",
    "simulated_past",
    "virtual_npc",
    "web_search",
    "external_source",
    "self_inference",
    "reflection",
    "admin_override",
    "system_migration",
]

BeliefStatus = Literal["held", "contested", "abandoned", "suppressed"]
SchemaStatus = Literal["active", "fading", "retired"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class BeliefEvidence(_Frozen):
    """One piece of support for or against a belief (spec 25)."""

    evidence_id: str
    belief_id: str
    stance: Stance
    weight: float = Field(ge=0.0, le=1.0)
    source_type: SourceType
    #: The *original* source. Two retellings of one source share this id and
    #: therefore count once (spec 25).
    primary_source_id: str
    event_id: str | None
    recorded_at: datetime


class Belief(_Frozen):
    """Something YUI holds to be true, and how well it is supported."""

    belief_id: str
    statement: str
    subject: str
    origin: EventOrigin
    confidence: float = Field(ge=0.0, le=1.0)
    support_weight: float = Field(default=0.0, ge=0.0)
    contradiction_weight: float = Field(default=0.0, ge=0.0)
    status: BeliefStatus = "held"
    first_formed_at: datetime
    updated_at: datetime
    revision_count: int = Field(default=0, ge=0)

    @property
    def is_held(self) -> bool:
        return self.status == "held"

    @property
    def is_contested(self) -> bool:
        return self.status == "contested"


class SelfSchema(_Frozen):
    """What YUI believes she is like (spec 12.4).

    Deliberately allowed to be wrong, and deliberately slow: ``pending_evidence``
    accumulates behaviour that has not yet changed the schema, which is what
    makes the self-concept lag behind actual behaviour (spec 23.2).
    """

    schema_id: str
    name: str
    statement: str
    strength: float = Field(ge=0.0, le=1.0)
    clarity: float = Field(default=0.5, ge=0.0, le=1.0)
    supporting_count: int = Field(default=0, ge=0)
    contradicting_count: int = Field(default=0, ge=0)
    pending_evidence: float = 0.0
    last_behaviour_at: datetime | None = None
    first_formed_at: datetime
    updated_at: datetime
    status: SchemaStatus = "active"

    @property
    def total_evidence(self) -> int:
        return self.supporting_count + self.contradicting_count


class PossibleSelf(_Frozen):
    """Who YUI might become — hoped for or feared (spec 12.4)."""

    possible_self_id: str
    name: str
    statement: str
    valence: Literal["hoped", "feared", "expected"]
    salience: float = Field(default=0.3, ge=0.0, le=1.0)
    created_at: datetime
    updated_at: datetime
    status: Literal["active", "dormant", "achieved", "abandoned"] = "active"
