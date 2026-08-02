"""Agency domain models (spec 15, 16.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware
from app.events.model import EventOrigin

GoalSource = Literal["need", "emotion", "value", "interest", "user_request", "habit"]
GoalStatus = Literal["active", "achieved", "abandoned", "blocked", "dormant"]
#: Spec 18.2: these are four different things and never interchangeable.
PlanStatus = Literal["planned", "in_progress", "completed", "cancelled", "missed"]
HabitStatus = Literal["forming", "established", "dormant", "extinguished"]
#: Spec 15.3: the two routes coexist.
DecisionRoute = Literal["goal_directed", "habitual", "reactive"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class Goal(_Frozen):
    """Something YUI wants, and why (spec 15.2)."""

    goal_id: str
    description: str
    source: GoalSource
    reason: str = ""
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Wanted for its own sake versus felt as an obligation (spec 15.2).
    autonomy: float = Field(default=0.5, ge=0.0, le=1.0)
    obligation: float = Field(default=0.0, ge=0.0, le=1.0)
    expected_reward: float = Field(default=0.5, ge=0.0, le=1.0)
    identity_relevance: float = Field(default=0.3, ge=0.0, le=1.0)
    value_alignment: float = Field(default=0.5, ge=0.0, le=1.0)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    status: GoalStatus = "active"
    created_at: datetime
    updated_at: datetime
    last_pursued_at: datetime | None = None
    origin: EventOrigin = "real_discord"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_self_endorsed(self) -> bool:
        """Wanted, rather than merely owed."""
        return self.autonomy > self.obligation


class Plan(_Frozen):
    """An intended future. Never an experience (spec 2.15, 18.2)."""

    plan_id: str
    goal_id: str | None
    description: str
    status: PlanStatus = "planned"
    planned_for: datetime | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    outcome: str | None = None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"

    @property
    def has_happened(self) -> bool:
        """Only a completed plan describes something that actually occurred."""
        return self.status == "completed" and self.completed_at is not None


class Habit(_Frozen):
    """A cue-triggered automatism (spec 15.3)."""

    habit_id: str
    name: str
    cue: str
    action: str
    automaticity: float = Field(default=0.0, ge=0.0, le=1.0)
    repetitions: int = Field(default=0, ge=0)
    cue_encounters: int = Field(default=0, ge=0)
    context_available: bool = True
    last_performed_at: datetime | None = None
    last_cue_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    status: HabitStatus = "forming"

    @property
    def is_established(self) -> bool:
        return self.status == "established"


class ActionCandidate(_Frozen):
    """One thing YUI could do now, with what she expects from it (spec 15.4)."""

    action: str
    route: DecisionRoute
    expected_value: float = Field(ge=0.0, le=1.0)
    goal_id: str | None = None
    habit_id: str | None = None
    reason: str = ""

    def with_value(self, value: float) -> ActionCandidate:
        return self.model_copy(update={"expected_value": max(0.0, min(1.0, value))})


class DecisionRecord(_Frozen):
    decision_id: str
    chosen: ActionCandidate
    considered: tuple[ActionCandidate, ...]
    #: True when the choice was made among near-equal options.
    was_close: bool = False
    decided_at: datetime
    run_id: str | None = None
    event_id: str | None = None
