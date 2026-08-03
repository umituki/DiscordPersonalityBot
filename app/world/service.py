"""World Service — single writer of the ``world`` domain (spec 9.3, 18).

Authority fields (spec 18.1): current time, virtual location, current activity,
environment, awake/asleep, active plan, ongoing event, last user interaction.

Two specification rules shape the implementation:

* **Four distinct things** (spec 18.2). A routine is a tendency, a plan is an
  intended future, a current activity is an ongoing fact, and a completed event
  is an occurred fact. Only ``finish_activity`` turns the third into the fourth.
* **Offline catch-up is compressed** (spec 18.3). Coming back after two days
  does not replay 2,880 minutes; it produces a bounded number of meaningful
  transitions.

The service proposes ``world`` state through the normal pipeline when it runs
inside a run, and writes its own objective records (activities, sleep episodes,
world history) which are facts about the world rather than psychology.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.state.proposal import StateChangeProposal
from app.storage.repositories.world import (
    ActivityRepository,
    SleepRepository,
    WorldHistoryRepository,
)
from app.world import sleep as sleep_model
from app.world.events import (
    ACTIVITY_FINISHED,
    ACTIVITY_STARTED,
    WENT_TO_SLEEP,
    WOKE_UP,
    ActivityFinishedPayload,
    ActivityStartedPayload,
    SleepTransitionPayload,
    WokeUpPayload,
)
from app.world.models import Activity, SleepEpisode
from app.world.policy import WorldPolicy

logger = logging.getLogger(__name__)

DOMAIN = "world"
MODULE = "world_service"

AWAKE = "awake"
SLEEP_PRESSURE = "sleep_pressure"
SLEEPINESS = "sleepiness"
CIRCADIAN = "circadian_phase"
SLEEP_INERTIA = "sleep_inertia"
MENTAL_FATIGUE = "mental_fatigue"

DEFAULTS: dict[str, float] = {
    AWAKE: 1.0,
    SLEEP_PRESSURE: 0.25,
    SLEEPINESS: 0.2,
    CIRCADIAN: 0.3,
    SLEEP_INERTIA: 0.0,
    MENTAL_FATIGUE: 0.1,
}


@dataclass(frozen=True, slots=True)
class CatchUpResult:
    """What happened while the process was not running (spec 18.3)."""

    elapsed_hours: float
    transitions: tuple[str, ...] = ()
    slept_hours: float = 0.0
    events: tuple[Event, ...] = field(default_factory=tuple)

    @property
    def compressed(self) -> bool:
        return bool(self.transitions)


class WorldService:
    name = MODULE

    def __init__(
        self,
        *,
        activities: ActivityRepository,
        sleeps: SleepRepository,
        history: WorldHistoryRepository,
        policy: WorldPolicy,
        clock: Clock | None = None,
    ) -> None:
        self._activities = activities
        self._sleeps = sleeps
        self._history = history
        self._policy = policy
        self._clock = clock or SystemClock()

    # --- as a subscriber ---------------------------------------------------
    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        """Keep the world's clock-derived state current for this run."""
        # Patch spec 13: sleep pressure and the day's progression follow the
        # run's time, so a simulated year is a year of world progression.
        now = view.now(self._clock)
        awake = (view.number(DOMAIN, AWAKE, 1.0) or 0.0) >= 0.5
        pressure = view.number(DOMAIN, SLEEP_PRESSURE, DEFAULTS[SLEEP_PRESSURE]) or 0.0
        elapsed = self._hours_since(view, now)

        if awake:
            pressure = sleep_model.accumulated_pressure(
                current=pressure, awake_hours=elapsed, policy=self._policy.sleep
            )
        else:
            pressure = sleep_model.recovered_pressure(
                current=pressure, slept_hours=elapsed, policy=self._policy.sleep
            )

        signals = sleep_model.signals(
            now=now,
            sleep_pressure=pressure,
            policy=self._policy.sleep,
            in_conversation=event.actor_type == "user",
        )

        proposals = [
            self._propose(event, SLEEP_PRESSURE, pressure),
            self._propose(event, SLEEPINESS, signals.sleepiness),
            self._propose(event, CIRCADIAN, signals.circadian_sleepiness),
        ]
        changed = [
            proposal
            for proposal in proposals
            if abs(proposal.value - (view.number(DOMAIN, proposal.target_key) or -1.0)) > 1e-6
        ]
        return SubscriberResult(proposals=tuple(changed))

    # --- activities (spec 18.2) --------------------------------------------
    def start_activity(
        self,
        *,
        name: str,
        kind: str,
        parent_event: Event | None = None,
        location: str | None = None,
        plan_id: str | None = None,
        expected_minutes: float | None = None,
    ) -> tuple[Activity, Event | None]:
        """Begin doing something. This is an ongoing fact, not an experience.

        ACT-001: ``ACTIVITY_STARTED`` は「やった経験」ではない. Starting is a
        change to the world, and nothing more. What she can later say she did
        comes from :meth:`finish_activity`, which is the only place an ongoing
        fact becomes an occurred one.
        """
        now = self._clock.now()
        ongoing = self._activities.ongoing()
        if ongoing is not None:
            self._activities.abandon(ongoing.activity_id, now=now, reason="superseded")

        activity = self._activities.start(
            name=name,
            kind=kind,
            location=location or self._policy.world.default_location,
            plan_id=plan_id,
            now=now,
            expected_end_at=(
                None
                if expected_minutes is None
                else now + timedelta(minutes=max(1.0, float(expected_minutes)))
            ),
        )
        self._history.record(
            transition="activity_started",
            awake=True,
            location=activity.location,
            activity=activity.name,
            detail={"kind": kind, "plan_id": plan_id},
            now=now,
        )
        event = None
        if parent_event is not None:
            event = parent_event.child(
                event_type=ACTIVITY_STARTED,
                category="world",
                actor_type="yui",
                source_type=MODULE,
                clock=self._clock,
                priority="P4",
                payload=ActivityStartedPayload(
                    activity_id=activity.activity_id,
                    name=name,
                    kind=kind,
                    location=activity.location or "",
                    plan_id=plan_id,
                ),
            )
        return activity, event

    def finish_activity(
        self, activity_id: str, *, outcome: str = "", parent_event: Event | None = None
    ) -> tuple[Activity, Event | None]:
        """Only here does an ongoing fact become an occurred fact (spec 18.2)."""
        now = self._clock.now()
        activity = self._activities.finish(activity_id, now=now, outcome=outcome or None)
        self._history.record(
            transition="activity_finished",
            awake=True,
            location=activity.location,
            activity=activity.name,
            detail={"outcome": outcome},
            now=now,
        )
        event = None
        if parent_event is not None:
            event = parent_event.child(
                event_type=ACTIVITY_FINISHED,
                category="world",
                actor_type="yui",
                source_type=MODULE,
                clock=self._clock,
                priority="P4",
                payload=ActivityFinishedPayload(
                    activity_id=activity.activity_id,
                    name=activity.name,
                    outcome=outcome,
                    duration_minutes=round(
                        (now - activity.started_at).total_seconds() / 60.0, 2
                    ),
                ),
            )
        return activity, event

    def current_activity(self) -> Activity | None:
        return self._activities.ongoing()

    def activity_due_to_finish(self) -> Activity | None:
        """Something ongoing whose intended end has passed (spec 24.2)."""
        return self._activities.due_to_finish(self._clock.now())

    def completed_activities(self, *, limit: int = 20) -> list[Activity]:
        return self._activities.completed(limit=limit)

    # --- sleep (spec 18.3) --------------------------------------------------
    def consider_sleep(
        self,
        *,
        sleep_pressure: float,
        has_active_goal: bool = False,
        in_conversation: bool = False,
    ) -> sleep_model.SleepSignals:
        return sleep_model.signals(
            now=self._clock.now(),
            sleep_pressure=sleep_pressure,
            policy=self._policy.sleep,
            has_active_goal=has_active_goal,
            in_conversation=in_conversation,
        )

    def fall_asleep(
        self,
        *,
        signals: sleep_model.SleepSignals,
        reason: str = "sleepy",
        kind: sleep_model.SleepKind | None = None,
    ) -> tuple[SleepEpisode, list[StateChangeProposal]]:
        """Go to sleep. SLEEP-002: a nap and a night are different episodes.

        ``kind`` is decided here rather than read back from the duration
        afterwards. A main sleep that a message cuts short after forty minutes
        is still a main sleep that went wrong, and calling it a nap in hindsight
        loses the only fact worth keeping about it.
        """
        now = self._clock.now()
        ongoing = self._activities.ongoing()
        if ongoing is not None:
            self._activities.finish(ongoing.activity_id, now=now, outcome="眠くなった")

        resolved = kind or sleep_model.classify_sleep(now, signals, self._policy.sleep)
        hours = (
            self._policy.sleep.nap_hours
            if resolved == "nap"
            else self._policy.sleep.typical_sleep_hours
        )
        episode = self._sleeps.begin(
            now=now,
            sleep_pressure=signals.sleep_pressure,
            circadian=signals.circadian_sleepiness,
            planned_wake_at=now + timedelta(hours=hours),
            reason=reason,
            kind=resolved,
        )
        self._history.record(
            transition="fell_asleep",
            awake=False,
            location=self._policy.world.default_location,
            activity="昼寝" if resolved == "nap" else "睡眠",
            detail={"sleep_pressure": signals.sleep_pressure, "kind": resolved},
            now=now,
        )
        return episode, []

    def current_sleep(self) -> SleepEpisode | None:
        """The episode she is in, if she is asleep at all."""
        return self._sleeps.current()

    def wake_up(self, *, interrupted: bool = False) -> tuple[SleepEpisode | None, float]:
        """Wake, discharging sleep pressure for the time actually slept."""
        now = self._clock.now()
        episode = self._sleeps.current()
        if episode is None:
            return None, 0.0

        slept = episode.slept_hours(now)
        quality = min(1.0, slept / self._policy.sleep.typical_sleep_hours)
        finished = self._sleeps.end(
            episode.sleep_id, now=now, quality=round(quality, 4), interrupted=interrupted
        )
        self._history.record(
            transition="woke_up",
            awake=True,
            location=self._policy.world.default_location,
            activity=self._policy.world.default_activity,
            detail={"slept_hours": round(slept, 3), "interrupted": interrupted},
            now=now,
        )
        return finished, slept

    def sleep_transition_event(
        self,
        parent: Event,
        *,
        asleep: bool,
        signals: sleep_model.SleepSignals,
        slept_hours: float = 0.0,
    ) -> Event:
        payload = (
            SleepTransitionPayload(
                asleep=True,
                sleep_pressure=signals.sleep_pressure,
                sleepiness=signals.sleepiness,
                circadian=signals.circadian_sleepiness,
            )
            if asleep
            else WokeUpPayload(
                asleep=False,
                sleep_pressure=signals.sleep_pressure,
                sleepiness=signals.sleepiness,
                circadian=signals.circadian_sleepiness,
                sleep_inertia=1.0,
                slept_hours=round(slept_hours, 3),
            )
        )
        return parent.child(
            event_type=WENT_TO_SLEEP if asleep else WOKE_UP,
            category="world",
            actor_type="yui",
            source_type=MODULE,
            clock=self._clock,
            priority="P4",
            payload=payload,
        )

    # --- offline catch-up (spec 18.3, 32) -----------------------------------
    def catch_up(self, *, since: datetime, sleep_pressure: float) -> CatchUpResult:
        """Advance the world over an offline gap in meaningful steps only."""
        now = self._clock.now()
        elapsed_hours = max(0.0, (now - since).total_seconds() / 3600.0)
        if elapsed_hours <= 0:
            return CatchUpResult(elapsed_hours=0.0)

        rules = self._policy.world
        step_hours = max(
            rules.catch_up_min_step_minutes / 60.0,
            elapsed_hours / rules.catch_up_max_transitions,
        )
        transitions: list[str] = []
        slept_hours = 0.0
        pressure = sleep_pressure
        moment = since
        awake = True

        while moment < now and len(transitions) < rules.catch_up_max_transitions:
            moment = min(now, moment + timedelta(hours=step_hours))
            signals = sleep_model.signals(
                now=moment, sleep_pressure=pressure, policy=self._policy.sleep
            )
            if awake:
                pressure = sleep_model.accumulated_pressure(
                    current=pressure, awake_hours=step_hours, policy=self._policy.sleep
                )
                if sleep_model.should_sleep(signals, self._policy.sleep):
                    awake = False
                    transitions.append("fell_asleep")
            else:
                pressure = sleep_model.recovered_pressure(
                    current=pressure, slept_hours=step_hours, policy=self._policy.sleep
                )
                slept_hours += step_hours
                if sleep_model.should_wake(
                    signals_now=signals, slept_hours=slept_hours, policy=self._policy.sleep
                ):
                    awake = True
                    transitions.append("woke_up")
                    slept_hours = 0.0

        self._history.record(
            transition="offline_catch_up",
            awake=awake,
            location=rules.default_location,
            activity=rules.default_activity,
            detail={
                "elapsed_hours": round(elapsed_hours, 3),
                "transitions": transitions,
                "sleep_pressure": round(pressure, 4),
            },
            now=now,
        )
        logger.info(
            "world caught up elapsed=%.1fh transitions=%d", elapsed_hours, len(transitions)
        )
        return CatchUpResult(
            elapsed_hours=round(elapsed_hours, 3),
            transitions=tuple(transitions),
            slept_hours=round(slept_hours, 3),
        )

    # --- helpers -----------------------------------------------------------
    def _propose(self, event: Event, key: str, value: float) -> StateChangeProposal:
        return StateChangeProposal.set_value(
            source_event_id=event.event_id,
            source_module=MODULE,
            target_domain=DOMAIN,
            target_key=key,
            value=round(max(0.0, min(1.0, value)), 6),
            reason_codes=("world_tick",),
            clock=self._clock,
        )

    def _hours_since(self, view: RunView, now: datetime) -> float:
        entry = view.snapshot.get(DOMAIN, SLEEP_PRESSURE)
        if entry is None:
            return 0.0
        return max(0.0, (now - entry.updated_at).total_seconds() / 3600.0)
