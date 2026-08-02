"""World events (spec 8.1, 18.2).

Only completed activities and actual sleep transitions are recorded. A plan or
an intention never produces one of these.
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

ACTIVITY_STARTED = "ACTIVITY_STARTED"
ACTIVITY_FINISHED = "ACTIVITY_FINISHED"
WENT_TO_SLEEP = "WENT_TO_SLEEP"
WOKE_UP = "WOKE_UP"


@register_payload(ACTIVITY_STARTED)
class ActivityStartedPayload(EventPayload):
    activity_id: str
    name: str
    kind: str
    location: str = ""
    plan_id: str | None = None


@register_payload(ACTIVITY_FINISHED)
class ActivityFinishedPayload(EventPayload):
    activity_id: str
    name: str
    outcome: str = ""
    duration_minutes: float = 0.0


@register_payload(WENT_TO_SLEEP)
class SleepTransitionPayload(EventPayload):
    asleep: bool
    sleep_pressure: float = 0.0
    sleepiness: float = 0.0
    circadian: float = 0.0


@register_payload(WOKE_UP)
class WokeUpPayload(SleepTransitionPayload):
    """Waking carries the same readings as falling asleep, plus grogginess."""

    sleep_inertia: float = 0.0
    slept_hours: float = 0.0
