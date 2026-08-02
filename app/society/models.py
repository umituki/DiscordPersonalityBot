"""Virtual society domain models (spec 20).

Two separations are built into the types, because they are the ones that are
easiest to lose and worst to lose:

``NPC`` vs ``NPCModel``
    the objective profile of a person versus what YUI believes about them
    (spec 20.2). They are different classes with different writers, so code
    cannot accidentally read one for the other.

``NPC`` vs the USER
    an NPC is never the USER (spec 1.2, 2.10). Every NPC record carries a
    ``virtual_life`` origin and no Discord identity exists here at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware
from app.events.model import EventOrigin

#: Spec 20.1. Tier 0 is a background presence, tier 1 recurs, tier 2 matters.
#: ``全 NPC を完全 Agent 化しない`` — the tier is what stops that happening.
BACKGROUND = 0
RECURRING = 1
SIGNIFICANT = 2

#: Spec 20.4. ``ended`` is a decision, never a consequence of silence.
RelationshipStage = Literal[
    "unmet",
    "acquaintance",
    "familiar",
    "close",
    "strained",
    "distant",
    "dormant",
    "ended",
    "reconnected",
]

STAGES: tuple[RelationshipStage, ...] = (
    "unmet",
    "acquaintance",
    "familiar",
    "close",
    "strained",
    "distant",
    "dormant",
    "ended",
    "reconnected",
)

#: Stages that mean "we have not spoken in a while", as opposed to "something
#: went wrong". Spec 20.4 insists these are different things.
ABSENCE_STAGES: frozenset[str] = frozenset({"distant", "dormant"})
TROUBLE_STAGES: frozenset[str] = frozenset({"strained", "ended"})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class NPC(_Frozen):
    """The objective profile of a person in YUI's virtual life (spec 20.2)."""

    npc_id: str
    name: str
    tier: int = Field(default=BACKGROUND, ge=0, le=2)
    role: str = ""
    traits: dict[str, Any] = Field(default_factory=dict)
    availability: float = Field(default=0.5, ge=0.0, le=1.0)
    warmth: float = Field(default=0.5, ge=0.0, le=1.0)
    reliability: float = Field(default=0.5, ge=0.0, le=1.0)
    status: Literal["active", "inactive", "gone"] = "active"
    created_at: datetime
    updated_at: datetime
    #: Never ``real_discord``: an NPC is not the USER (spec 2.10).
    origin: EventOrigin = "virtual_life"

    @property
    def is_significant(self) -> bool:
        return self.tier >= SIGNIFICANT

    @property
    def keeps_relationship_state(self) -> bool:
        """Tier 0 people are around; they are not tracked (spec 20.1)."""
        return self.tier >= RECURRING


class NPCModel(_Frozen):
    """What YUI thinks an NPC is like. Allowed to be wrong (spec 20.2)."""

    model_id: str
    npc_id: str
    perceived_warmth: float = Field(default=0.5, ge=0.0, le=1.0)
    perceived_reliability: float = Field(default=0.5, ge=0.0, le=1.0)
    perceived_availability: float = Field(default=0.5, ge=0.0, le=1.0)
    observation_count: int = 0
    confidence: float = Field(default=0.2, ge=0.0, le=1.0)
    updated_at: datetime


class NPCRelationship(_Frozen):
    """YUI's relationship with one NPC, and where it stands (spec 20.4)."""

    relationship_id: str
    npc_id: str
    stage: RelationshipStage = "unmet"
    previous_stage: str = ""
    familiarity: float = Field(default=0.0, ge=0.0, le=1.0)
    closeness: float = Field(default=0.0, ge=0.0, le=1.0)
    conflict: float = Field(default=0.0, ge=0.0, le=1.0)
    interaction_count: int = 0
    first_met_at: datetime | None = None
    last_contact_at: datetime | None = None
    ended_at: datetime | None = None
    ended_reason: str = ""
    updated_at: datetime

    @property
    def is_ended(self) -> bool:
        return self.stage == "ended"

    @property
    def merely_out_of_touch(self) -> bool:
        """Out of contact, not fallen out. The distinction of spec 20.4."""
        return self.stage in ABSENCE_STAGES


class Group(_Frozen):
    """A group YUI can belong to (spec 20.3)."""

    group_id: str
    name: str
    activity_type: str = ""
    norms: tuple[str, ...] = ()
    social_density: float = Field(default=0.5, ge=0.0, le=1.0)
    competitiveness: float = Field(default=0.5, ge=0.0, le=1.0)
    warmth: float = Field(default=0.5, ge=0.0, le=1.0)
    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    status: Literal["active", "dissolved"] = "active"
    created_at: datetime
    updated_at: datetime


class GroupMembership(_Frozen):
    membership_id: str
    group_id: str
    member_type: Literal["yui", "npc"] = "npc"
    npc_id: str | None = None
    role: str = "member"
    joined_at: datetime
    left_at: datetime | None = None
    status: Literal["active", "left"] = "active"


class SocialLink(_Frozen):
    """A tie between two NPCs. The society does not revolve around YUI."""

    link_id: str
    from_npc_id: str
    to_npc_id: str
    kind: str = "acquaintance"
    strength: float = Field(default=0.3, ge=0.0, le=1.0)
    created_at: datetime
    updated_at: datetime


class NPCInteraction(_Frozen):
    """Something that happened with an NPC. Virtual life, never Discord."""

    interaction_id: str
    npc_id: str
    group_id: str | None = None
    kind: str = "conversation"
    valence: float = Field(default=0.0, ge=-1.0, le=1.0)
    summary: str = ""
    occurred_at: datetime
    event_id: str | None = None
    origin: EventOrigin = "virtual_life"
