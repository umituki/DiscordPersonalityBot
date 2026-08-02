"""Immediate psychology domain models (spec 11, 15.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

#: Spec 11.1 appraisal dimensions.
APPRAISAL_DIMENSIONS: tuple[str, ...] = (
    "self_relevance",
    "goal_congruence",
    "novelty",
    "certainty",
    "control",
    "agency",
    "social_meaning",
    "expectation_violation",
)

#: Spec 11.2: emotions are named states with intensity *and* duration.
EmotionName = Literal[
    "joy", "sadness", "anger", "fear", "surprise", "interest", "affection"
]

#: Spec 11.2: emotion leads to an action tendency, not directly to behaviour.
ACTION_TENDENCIES: dict[str, str] = {
    "joy": "approach",
    "sadness": "withdraw",
    "anger": "confront",
    "fear": "avoid",
    "surprise": "orient",
    "interest": "explore",
    "affection": "affiliate",
}


class Appraisal(BaseModel):
    """How an event was read (spec 11.1).

    Bipolar dimensions run -1..1; unipolar ones 0..1. This is Layer 1: it lives
    for one run, justifies proposals, and is never committed as state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    self_relevance: float = Field(default=0.5, ge=0.0, le=1.0)
    goal_congruence: float = Field(default=0.0, ge=-1.0, le=1.0)
    novelty: float = Field(default=0.2, ge=0.0, le=1.0)
    certainty: float = Field(default=0.5, ge=0.0, le=1.0)
    control: float = Field(default=0.5, ge=0.0, le=1.0)
    agency: float = Field(default=0.5, ge=0.0, le=1.0)
    social_meaning: float = Field(default=0.3, ge=-1.0, le=1.0)
    expectation_violation: float = Field(default=0.0, ge=0.0, le=1.0)
    #: The producer's own confidence. Only ever one signal (spec 24).
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Where this reading came from, for provenance.
    source: Literal["llm", "default", "degraded"] = "default"
    reason: str = ""

    def dimension(self, name: str) -> float:
        if name not in APPRAISAL_DIMENSIONS:
            raise KeyError(f"unknown appraisal dimension: {name!r}")
        return float(getattr(self, name))

    def as_dimension_map(self) -> dict[str, float]:
        return {name: self.dimension(name) for name in APPRAISAL_DIMENSIONS}

    @property
    def is_trusted(self) -> bool:
        return self.source in ("llm", "default")


class AppraisalCandidate(BaseModel):
    """Structured output asked of the model (spec 11.1: a candidate, not state)."""

    model_config = ConfigDict(extra="forbid")

    self_relevance: float = Field(ge=0.0, le=1.0)
    goal_congruence: float = Field(ge=-1.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    certainty: float = Field(ge=0.0, le=1.0)
    control: float = Field(ge=0.0, le=1.0)
    agency: float = Field(ge=0.0, le=1.0)
    social_meaning: float = Field(ge=-1.0, le=1.0)
    expectation_violation: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = ""


class EmotionEpisode(BaseModel):
    """One activated emotion (spec 11.2)."""

    model_config = ConfigDict(frozen=True)

    episode_id: str
    name: EmotionName
    intensity: float = Field(ge=0.0, le=1.0)
    #: Duration is tracked separately from intensity (spec 11.2).
    started_at: datetime
    decayed_at: datetime | None = None
    ended_at: datetime | None = None
    trigger_event_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    cause: str = ""
    unresolved: bool = False
    action_tendency: str = ""
    run_id: str | None = None

    @field_validator("started_at", "decayed_at", "ended_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_aware(value)

    @property
    def is_active(self) -> bool:
        return self.ended_at is None


class MoodPoint(BaseModel):
    """A recorded mood reading (spec 11.3)."""

    model_config = ConfigDict(frozen=True)

    recorded_at: datetime
    valence: float = Field(ge=0.0, le=1.0)
    arousal: float = Field(ge=0.0, le=1.0)
    reason_code: str = ""
    run_id: str | None = None

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)
