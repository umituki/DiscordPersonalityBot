"""Growth domain models (spec 12, 23, 31.7).

The three-way separation of spec 23.2 is expressed in the types themselves:

``PersonalityTrait``
    holds only the slow ``baseline``. What YUI currently *expresses* is the
    committed ``personality`` state, so the two can never silently merge.

``CharacteristicAdaptation``
    the middle layer that is allowed to move faster than a trait (spec 12.2).

``DeepUpdateCandidate``
    the waiting room in front of Layer 4. A candidate is not a change; it is a
    claim that something might have become true, still missing one or more of
    the five conditions in spec 12.3.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

CandidateStatus = Literal["accumulating", "promoted", "expired", "rejected"]
ThemeStatus = Literal["emerging", "established", "fading"]
#: Spec 23.3. Only INVALID is a rollback/reprocess candidate.
DriftClassification = Literal["EXPECTED", "SUSPICIOUS", "INVALID"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class CharacteristicAdaptation(_Frozen):
    """A domain-specific adaptation (spec 12.2)."""

    adaptation_id: str
    name: str
    value: float = Field(ge=0.0, le=1.0)
    baseline: float = Field(ge=0.0, le=1.0)
    pending_evidence: float = 0.0
    supporting_count: int = 0
    contradicting_count: int = 0
    contexts: tuple[str, ...] = ()
    #: The most recent committed changes that moved this adaptation. Provenance
    #: travels with the value, so a later proposal can cite it (spec 25).
    evidence_ids: tuple[str, ...] = ()
    first_evidence_at: datetime | None = None
    last_evidence_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def offset(self) -> float:
        """How far this adaptation has moved from where it started."""
        return round(self.value - self.baseline, 6)

    @property
    def cross_context(self) -> bool:
        return len(self.contexts) >= 2


class PersonalityTrait(_Frozen):
    """The slow baseline of one trait. Never the expressed value."""

    trait_id: str
    name: str
    baseline: float = Field(ge=0.0, le=1.0)
    initial_baseline: float = Field(ge=0.0, le=1.0)
    created_at: datetime
    updated_at: datetime

    @property
    def drifted(self) -> float:
        return round(self.baseline - self.initial_baseline, 6)


class ValuePriority(_Frozen):
    """One value's *relative* standing among the others (spec 12.5)."""

    value_id: str
    name: str
    priority: float = Field(ge=0.0, le=1.0)
    initial_priority: float = Field(ge=0.0, le=1.0)
    created_at: datetime
    updated_at: datetime


class DeepUpdateCandidate(_Frozen):
    """Accumulated evidence that a Layer 4 domain may have really changed."""

    candidate_id: str
    target_domain: str
    target_key: str
    #: +1 or -1. A candidate is directional; opposite evidence starts its own.
    direction: int
    pattern_count: int = 0
    contexts: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    outcome_weight: float = 0.0
    mood_independent_count: int = 0
    magnitude: float = 0.0
    source_adaptation: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    status: CandidateStatus = "accumulating"
    resolved_at: datetime | None = None
    blocked_reason: str = ""

    @property
    def persistence_days(self) -> float:
        return round((self.last_seen_at - self.first_seen_at).total_seconds() / 86400.0, 6)

    @property
    def target(self) -> str:
        return f"{self.target_domain}.{self.target_key}"


class NarrativeTheme(_Frozen):
    """A recurring story YUI tells about herself (spec 12.4)."""

    theme_id: str
    theme: str
    statement: str = ""
    strength: float = Field(default=0.0, ge=0.0, le=1.0)
    supporting_memory_count: int = 0
    supporting_memory_ids: tuple[str, ...] = ()
    first_seen_at: datetime
    last_updated_at: datetime
    status: ThemeStatus = "emerging"


class DriftObservation(_Frozen):
    """One measured metric and how it was classified (spec 23.3)."""

    observation_id: str
    metric: str
    window_start: datetime
    window_end: datetime
    value: float
    expected_max: float
    classification: DriftClassification
    detail: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime

    @property
    def rollback_candidate(self) -> bool:
        """Only INVALID is one. SUSPICIOUS is for a human to look at."""
        return self.classification == "INVALID"


class ConsolidationRunRecord(_Frozen):
    consolidation_id: str
    started_at: datetime
    ended_at: datetime | None = None
    kind: str = "routine"
    changes_read: int = 0
    adaptations_moved: int = 0
    candidates_raised: int = 0
    deep_updates: int = 0
    semantic_facts: int = 0
    status: str = "running"
