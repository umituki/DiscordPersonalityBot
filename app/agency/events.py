"""Agency events (spec 15, 22, 31).

Only things that actually happened. A goal being *adopted* is a change of
intention and gets its own event; a goal being *pursued* is a thing she did, and
so is performing a habit. Neither is a plan — spec 2.15 keeps those apart, and
so does this file: nothing here is emitted for something merely intended.
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

GOAL_ADOPTED = "GOAL_ADOPTED"
GOAL_PURSUED = "GOAL_PURSUED"
HABIT_PERFORMED = "HABIT_PERFORMED"


@register_payload(GOAL_ADOPTED)
class GoalAdoptedPayload(EventPayload):
    goal_id: str
    description: str
    source: str = ""
    importance: float = 0.5
    autonomy: float = 0.5


@register_payload(GOAL_PURSUED)
class GoalPursuedPayload(EventPayload):
    """A step taken towards something she wants (spec 31.3).

    The step is real work — an activity is started for it — so this reports
    what was begun, not what was achieved. Progress is recorded when the
    activity completes, which is the same rule ACT-002 applies everywhere.
    """

    goal_id: str
    description: str
    plan_id: str | None = None
    activity_id: str | None = None
    progress: float = 0.0


@register_payload(HABIT_PERFORMED)
class HabitPerformedPayload(EventPayload):
    habit_id: str
    name: str
    cue: str
    action: str
    automaticity: float = 0.0
    #: True when it ran off its cue rather than from a decision about goals.
    automatic: bool = False


__all__ = [
    "GOAL_ADOPTED",
    "GOAL_PURSUED",
    "HABIT_PERFORMED",
    "GoalAdoptedPayload",
    "GoalPursuedPayload",
    "HabitPerformedPayload",
]
