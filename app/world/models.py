"""Virtual life domain models (spec 18, 19).

Spec 18.2 insists on four separate things, and the type system says so too::

    Routine          = tendency          (a habit, in app.agency)
    Plan             = intended future   (app.agency.models.Plan)
    Current Activity = ongoing fact      (Activity, status="ongoing")
    Completed Event  = occurred fact     (Activity, status="completed")
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware
from app.events.model import EventOrigin

ActivityStatus = Literal["ongoing", "completed", "abandoned"]
ActivityKind = Literal["rest", "leisure", "work", "social", "maintenance", "sleep"]
#: Spec 19 job classes.
JobClass = Literal["FIXED", "WINDOW", "CONDITION", "BACKGROUND"]
JobStatus = Literal["pending", "fired", "expired", "cancelled", "misfired"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class Activity(_Frozen):
    """What YUI is doing, or did (spec 18.2)."""

    activity_id: str
    name: str
    kind: ActivityKind
    location: str | None = None
    plan_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    status: ActivityStatus = "ongoing"
    outcome: str | None = None
    origin: EventOrigin = "virtual_life"

    @property
    def is_ongoing(self) -> bool:
        return self.status == "ongoing"

    @property
    def has_happened(self) -> bool:
        """Only a finished activity is a fact about the past."""
        return self.status == "completed" and self.ended_at is not None


class SleepEpisode(_Frozen):
    sleep_id: str
    started_at: datetime
    ended_at: datetime | None = None
    planned_wake_at: datetime | None = None
    sleep_pressure_at_onset: float = 0.0
    circadian_at_onset: float = 0.0
    quality: float | None = None
    interrupted: bool = False
    reason: str = ""

    @property
    def is_asleep(self) -> bool:
        return self.ended_at is None

    def slept_hours(self, until: datetime) -> float:
        end = self.ended_at or until
        return max(0.0, (end - self.started_at).total_seconds() / 3600.0)


class ScheduledJob(_Frozen):
    """A scheduled *opportunity source*, never an action (spec 19)."""

    job_id: str
    job_type: str
    job_class: JobClass
    due_at: datetime | None = None
    window_end: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: str = "P4"
    status: JobStatus = "pending"
    misfire_policy: Literal["skip", "run_once", "reschedule"] = "skip"
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_fired_at: datetime | None = None
    attempts: int = 0

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def missed_window(self, now: datetime) -> bool:
        return self.window_end is not None and now > self.window_end


class Opportunity(_Frozen):
    """A moment when something *could* be done. The decision is elsewhere."""

    kind: str
    detail: str = ""
    job_id: str | None = None
    urgency: float = Field(default=0.3, ge=0.0, le=1.0)
    created_at: datetime

    @property
    def is_contact(self) -> bool:
        return self.kind == "proactive_contact"
