"""What one wake-up was (rebuild spec 21, Phase 6)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

#: Why the loop woke. ``timer`` is the ordinary case; the others are worth
#: telling apart, because "she acted because the USER appeared" and "she acted
#: because it was time" are different facts about her.
WakeReason = Literal["timer", "user_activity", "manual", "startup", "shutdown"]

#: What the wake-up came to. ``idle`` is a legitimate outcome and by far the
#: most common one: most moments are not moments to do something.
TickOutcome = Literal[
    "idle",  # nothing was collected, or nothing was worth doing
    "acted",  # a decision was made and its action ran
    "deferred",  # something was worth doing and this was not the moment
    "unexecutable",  # a decision was made and nothing was wired to carry it out
    "failed",  # the action raised
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class RuntimeTick(_Frozen):
    """One pass of the loop, recorded whether or not anything happened.

    Recording the empty passes is the point. A runtime that wakes on schedule
    and never does anything looks identical to a runtime that is not running,
    unless the quiet wake-ups leave a trace (spec 4.5).
    """

    tick_id: str
    woke_at: datetime
    wake_reason: WakeReason = "timer"
    opportunities: int = Field(default=0, ge=0)
    #: Every kind collected, in order, so the audit can ask what fires.
    opportunity_kinds: tuple[str, ...] = ()
    #: Kinds nobody could turn into a candidate. A scheduler firing
    #: ``activity_due`` into a runtime with no activity phase yet must be
    #: visible rather than silently dropped.
    unclaimed_kinds: tuple[str, ...] = ()
    candidates: int = Field(default=0, ge=0)
    decision_id: str | None = None
    chosen_action: str | None = None
    executed: bool = False
    outcome: TickOutcome = "idle"
    #: Set when ``outcome`` is ``deferred``: which rule held the action back.
    deferred_reason: str = ""
    next_wake_at: datetime | None = None
    duration_ms: int = Field(default=0, ge=0)

    @property
    def did_something(self) -> bool:
        return self.executed

    def describe(self) -> str:
        """One line, for a log or the debug command."""
        kinds = ",".join(self.opportunity_kinds) or "-"
        return (
            f"{self.wake_reason} opportunities={self.opportunities}({kinds}) "
            f"candidates={self.candidates} outcome={self.outcome} "
            f"action={self.chosen_action or '-'}"
        )


__all__ = ["RuntimeTick", "TickOutcome", "WakeReason"]
