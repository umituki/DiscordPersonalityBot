"""Activity and sleep, driven by the runtime (rebuild spec 24, 25 — Phase 7).

This is the phase where the loop from Phase 6 stops being an empty frame. It
registers the first real candidate builders and action handlers, so something
autonomous actually happens:

    SleepSource      → sleep_candidate / wake_candidate
    ActivitySource   → activity_finish / activity_due
    LifeActions      → WorldService, then a real event through the processor

SLEEP-003 and SLEEP-004 are the point of the whole arrangement. The previous
build computed a sleep transition only when an event happened to arrive, which
means she could only fall asleep if someone spoke to her. Here the transition is
a due action the loop fires on its own.

Three rules this module is built around:

ACT-001/002
    ``ACTIVITY_STARTED`` は「やった経験」ではない, and she can say 今日は○○した
    only when a completed record exists. So starting and finishing are separate
    opportunities, separate decisions and separate events, and nothing here
    shortcuts from one to the other.

ACT-003
    The model may propose what came of an activity. The *completed fact* is
    the World Service's, which is why the handler asks for details and then
    hands them to ``finish_activity`` rather than writing a row itself.

SLEEP-001
    ``LLM の気分だけで「寝ない」を無限継続させない``. Resistance already decays
    as sleepiness climbs; past ``forced_sleep_threshold`` the candidate is
    raised at a value nothing else can outbid, so staying up stops being an
    option rather than becoming unlikely.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Sequence

from app.agency.models import ActionCandidate
from app.clock import Clock, SystemClock
from app.events.model import Event
from app.world import sleep as sleep_model
from app.world.events import (
    ACTIVITY_FINISHED,
    ACTIVITY_STARTED,
    WENT_TO_SLEEP,
    WOKE_UP as WOKE_UP_EVENT,
    ActivityFinishedPayload,
    ActivityStartedPayload,
    SleepTransitionPayload,
    WokeUpPayload,
)
from app.world.models import Opportunity

logger = logging.getLogger(__name__)

MODULE = "life_runtime"

#: Opportunity kinds this phase claims. Spec 22 names the first two.
SLEEP_CANDIDATE = "sleep_candidate"
WAKE_CANDIDATE = "wake_candidate"
ACTIVITY_DUE = "activity_due"
ACTIVITY_FINISH = "activity_finish"

#: Action names. Kept distinct from the opportunity kinds on purpose: an
#: opportunity is a moment and an action is a thing done, and collapsing the
#: two is how "it was time to sleep" quietly becomes "she slept".
GO_TO_SLEEP = "go_to_sleep"
WAKE_UP = "wake_up"
START_ACTIVITY = "start_activity"
FINISH_ACTIVITY = "finish_activity"

#: How often to look again when nothing has a definite time. Sleepiness moves
#: slowly; checking every few minutes is plenty and keeps 21.1 satisfied.
SLEEP_CHECK_MINUTES = 15.0


class SleepSource:
    """Notices that it is time to sleep, or time to get up (SLEEP-003).

    Reads state; writes nothing. The decision is the Decision Engine's and the
    transition is the World Service's.
    """

    name = "sleep"

    def __init__(
        self,
        world: Any,
        state: Any,
        *,
        policy: Any,
        clock: Clock | None = None,
        check_minutes: float = SLEEP_CHECK_MINUTES,
    ) -> None:
        self._world = world
        self._state = state
        self._policy = policy
        self._clock = clock or SystemClock()
        self._check_minutes = check_minutes

    def signals(self, now: datetime) -> sleep_model.SleepSignals:
        return self._world.consider_sleep(
            sleep_pressure=self._pressure(),
            has_active_goal=False,
            in_conversation=False,
        )

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        episode = self._world.current_sleep()
        signals = self.signals(now)

        if episode is not None:
            slept = episode.slept_hours(now)
            planned = episode.planned_wake_at
            due = planned is not None and now >= planned
            if due or sleep_model.should_wake(
                signals_now=signals, slept_hours=slept, policy=self._policy
            ):
                return [
                    Opportunity(
                        kind=WAKE_CANDIDATE,
                        detail=f"slept {slept:.2f}h",
                        urgency=0.7 if due else 0.5,
                        created_at=now,
                    )
                ]
            return []

        if sleep_model.should_sleep(signals, self._policy):
            forced = sleep_model.resistance_is_exhausted(signals, self._policy)
            return [
                Opportunity(
                    kind=SLEEP_CANDIDATE,
                    detail="exhausted" if forced else "sleepy",
                    # SLEEP-001: past the ceiling this is not a preference.
                    urgency=1.0 if forced else min(0.9, signals.net_sleepiness),
                    created_at=now,
                )
            ]
        return []

    def next_due(self, now: datetime) -> datetime | None:
        """When to look again.

        An episode with a planned wake time knows its own moment. Otherwise
        sleepiness is a slow curve, so a periodic check is both honest and
        cheap — far short of the per-second poll 21.1 rules out.
        """
        episode = self._world.current_sleep()
        if episode is not None and episode.planned_wake_at is not None:
            return episode.planned_wake_at
        return now + timedelta(minutes=self._check_minutes)

    def _pressure(self) -> float:
        entry = self._state.get("world", "sleep_pressure")
        return 0.25 if entry is None else float(entry.numeric or 0.0)


class ActivitySource:
    """Notices that something is due to finish, or that there is nothing to do.

    Spec 24.2's lifecycle in two moments. The *scheduled completion* is read
    from the activity's own intended end rather than from a timer somewhere
    else, so a restart cannot lose it.
    """

    name = "activity"

    def __init__(self, world: Any, *, clock: Clock | None = None) -> None:
        self._world = world
        self._clock = clock or SystemClock()

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        if self._world.current_sleep() is not None:
            return []  # she is asleep; nothing is due

        due = self._world.activity_due_to_finish()
        if due is not None:
            return [
                Opportunity(
                    kind=ACTIVITY_FINISH,
                    detail=due.activity_id,
                    urgency=0.6,
                    created_at=now,
                )
            ]
        if self._world.current_activity() is None:
            return [
                Opportunity(
                    kind=ACTIVITY_DUE, detail="", urgency=0.3, created_at=now
                )
            ]
        return []

    def next_due(self, now: datetime) -> datetime | None:
        current = self._world.current_activity()
        if current is None:
            return None  # nothing scheduled; the idle interval applies
        return current.expected_end_at


class LifeCandidates:
    """What each of those moments would be worth (spec 23).

    The values are the *domain's* opinion, which is why they live here and not
    in the loop. Everything is expressed on the one scale the Decision Engine
    compares on, and nothing here chooses.
    """

    name = "life_candidates"

    def __init__(self, *, policy: Any) -> None:
        self._policy = policy

    def sleep(self, opportunity: Opportunity, now: datetime) -> ActionCandidate | None:
        forced = opportunity.detail == "exhausted"
        return ActionCandidate(
            action=GO_TO_SLEEP,
            route="reactive",
            # SLEEP-001. Nothing else in the system produces 1.0, so at the
            # ceiling this is not competing — it has already won.
            expected_value=1.0 if forced else min(0.95, 0.5 + opportunity.urgency / 2),
            reason="眠気が限界" if forced else "眠くなってきた",
        )

    def wake(self, opportunity: Opportunity, now: datetime) -> ActionCandidate | None:
        return ActionCandidate(
            action=WAKE_UP,
            route="reactive",
            expected_value=min(0.95, 0.4 + opportunity.urgency / 2),
            reason="目が覚めた",
        )

    def start(self, opportunity: Opportunity, now: datetime) -> ActionCandidate | None:
        return ActionCandidate(
            action=START_ACTIVITY,
            route="goal_directed",
            expected_value=0.45,
            reason="手が空いている",
        )

    def finish(self, opportunity: Opportunity, now: datetime) -> ActionCandidate | None:
        return ActionCandidate(
            action=FINISH_ACTIVITY,
            route="goal_directed",
            expected_value=0.7,
            reason="ひと区切りついた",
        )




class LifeActions:
    """Carries out what was chosen, and owns the events that result.

    Spec 22: only a selected *and executed* opportunity becomes an event. So
    every method here ends the same way — the World Service makes the change,
    an event is created describing what happened, and the processor takes it
    from there. Nothing in this class writes psychological state; that is what
    the processor, the domains and the arbitrator are for (RUNTIME-001).
    """

    name = "life_actions"

    def __init__(
        self,
        world: Any,
        *,
        processor: Any,
        state: Any,
        policy: Any,
        source: SleepSource,
        candidates: LifeCandidates | None = None,
        diary: Any = None,
        clock: Clock | None = None,
        activities: Sequence[tuple[str, str, float]] = (),
    ) -> None:
        self._world = world
        self._processor = processor
        self._state = state
        self._policy = policy
        self._source = source
        self._candidates = candidates or LifeCandidates(policy=policy)
        #: Spec 26.3. The diary is written at bedtime, and it may not delay it:
        #: whatever happens in there, she goes to sleep afterwards.
        self._diary = diary
        self._clock = clock or SystemClock()
        #: What she might do, as (name, kind, minutes). Phase 8 replaces this
        #: with the ``activity_candidates`` model call of spec 24.1; until then
        #: a fixed repertoire is the honest placeholder, and it is declared
        #: here rather than invented inside the handler.
        self._repertoire = tuple(activities) or DEFAULT_REPERTOIRE

    # --- sleep ---------------------------------------------------------------
    async def go_to_sleep(self, candidate: ActionCandidate, now: datetime) -> bool:
        """SLEEP-003/004: the transition actually fires, from the loop."""
        if self._world.current_sleep() is not None:
            return False  # already asleep; the opportunity went stale
        signals = self._source.signals(now)

        # 26.3: reflection comes before the transition, and cannot prevent it.
        # A model that hangs leaves the entry owed and she sleeps anyway; the
        # retry writes it later as `late_written`.
        await self._reflect()

        episode, _ = self._world.fall_asleep(
            signals=signals,
            reason="exhausted" if candidate.expected_value >= 1.0 else "sleepy",
        )
        await self._emit(
            WENT_TO_SLEEP,
            SleepTransitionPayload(
                asleep=True,
                sleep_pressure=signals.sleep_pressure,
                sleepiness=signals.sleepiness,
                circadian=signals.circadian_sleepiness,
            ),
        )
        logger.info("fell asleep kind=%s reason=%s", episode.kind, episode.reason)
        return True

    async def wake_up(self, candidate: ActionCandidate, now: datetime) -> bool:
        episode, slept = self._world.wake_up()
        if episode is None:
            return False
        # 26.2: waking starts the day the diary will be about. Only a main
        # sleep does — a nap does not end a day, and treating it as one would
        # give her two diaries for one afternoon.
        if self._diary is not None and not episode.is_nap:
            try:
                self._diary.begin_day(sleep_episode_id=episode.sleep_id)
            except Exception:  # noqa: BLE001 - a missing day is not a reason to stay in bed
                logger.exception("could not open a life day on waking")
        signals = self._source.signals(now)
        await self._emit(
            WOKE_UP_EVENT,
            WokeUpPayload(
                asleep=False,
                sleep_pressure=signals.sleep_pressure,
                sleepiness=signals.sleepiness,
                circadian=signals.circadian_sleepiness,
                sleep_inertia=1.0,
                slept_hours=round(slept, 3),
            ),
        )
        return True

    # --- activity ------------------------------------------------------------
    async def start_activity(self, candidate: ActionCandidate, now: datetime) -> bool:
        """ACT-001. This makes an ongoing fact and nothing more.

        No memory is encoded here, and nothing about this lets her say she did
        anything. That belongs to :meth:`finish_activity`.
        """
        if self._world.current_activity() is not None:
            return False
        name, kind, minutes = self._choose_activity(now)
        activity, _ = self._world.start_activity(
            name=name, kind=kind, expected_minutes=minutes
        )
        await self._emit(
            ACTIVITY_STARTED,
            ActivityStartedPayload(
                activity_id=activity.activity_id,
                name=activity.name,
                kind=activity.kind,
                location=activity.location or "",
            ),
        )
        return True

    async def finish_activity(self, candidate: ActionCandidate, now: datetime) -> bool:
        """ACT-002/003. Only here does something become a thing she did.

        The outcome text may later come from the model (24.1's counterpart at
        completion time); the completed *fact* is the World Service's, which is
        why the details are passed to it rather than written from here.
        """
        activity = self._world.activity_due_to_finish() or self._world.current_activity()
        if activity is None:
            return False
        finished, _ = self._world.finish_activity(
            activity.activity_id, outcome=self._outcome_for(activity)
        )
        await self._emit(
            ACTIVITY_FINISHED,
            ActivityFinishedPayload(
                activity_id=finished.activity_id,
                name=finished.name,
                outcome=finished.outcome or "",
                duration_minutes=round(
                    ((finished.ended_at or now) - finished.started_at).total_seconds()
                    / 60.0,
                    2,
                ),
            ),
        )
        return True

    def attach_diary(self, diary: Any) -> None:
        """Late-bind the diary, which is built after this is (bootstrap order).

        Not optional in the running application: without it she goes to sleep
        without ever looking back at the day, which is a silent hole rather
        than a visible one.
        """
        self._diary = diary

    async def _reflect(self) -> None:
        """Look back at the day, with a hard promise that sleep follows."""
        if self._diary is None:
            return
        try:
            await self._diary.reflect_at_bedtime()
        except Exception:  # noqa: BLE001 - 26.3, in one line
            logger.exception("bedtime reflection failed; going to sleep anyway")

    # --- registration --------------------------------------------------------
    def register(self, registry: Any, *, sources: Sequence[Any] = ()) -> None:
        """Wire this phase into the Phase 6 loop.

        One place, so what she is capable of doing on her own is a list you can
        read rather than something spread across a constructor.
        """
        for source in sources:
            registry.add_source(source)
        registry.add_builder(SLEEP_CANDIDATE, self._candidates.sleep)
        registry.add_builder(WAKE_CANDIDATE, self._candidates.wake)
        registry.add_builder(ACTIVITY_DUE, self._candidates.start)
        registry.add_builder(ACTIVITY_FINISH, self._candidates.finish)
        registry.add_handler(GO_TO_SLEEP, self.go_to_sleep)
        registry.add_handler(WAKE_UP, self.wake_up)
        registry.add_handler(START_ACTIVITY, self.start_activity)
        registry.add_handler(FINISH_ACTIVITY, self.finish_activity)

    # --- internals -----------------------------------------------------------
    async def _emit(self, event_type: str, payload: Any) -> None:
        """One autonomous act, through the ordinary pipeline.

        A root event rather than a child: nothing prompted this. She did it
        because it was time, and pretending it descended from a USER message
        would make the provenance a lie.
        """
        event = Event.create(
            event_type=event_type,
            category="world",
            actor_type="yui",
            source_type=MODULE,
            origin="virtual_life",
            priority="P4",
            payload=payload,
            clock=self._clock,
        )
        await self._processor.process(event)

    def _choose_activity(self, now: datetime) -> tuple[str, str, float]:
        recent = {item.name for item in self._world.completed_activities(limit=3)}
        for name, kind, minutes in self._repertoire:
            if name not in recent:
                return name, kind, minutes
        return self._repertoire[0]

    @staticmethod
    def _outcome_for(activity: Any) -> str:
        return f"{activity.name}をした"


#: Phase 8 replaces this with spec 24.1's ``activity_candidates`` call.
DEFAULT_REPERTOIRE: tuple[tuple[str, str, float], ...] = (
    ("本を読む", "leisure", 45.0),
    ("音楽を聴く", "leisure", 30.0),
    ("部屋を片づける", "maintenance", 25.0),
    ("ぼんやりする", "rest", 20.0),
)


__all__ = [
    "ACTIVITY_DUE",
    "ACTIVITY_FINISH",
    "FINISH_ACTIVITY",
    "GO_TO_SLEEP",
    "SLEEP_CANDIDATE",
    "START_ACTIVITY",
    "WAKE_CANDIDATE",
    "WAKE_UP",
    "ActivitySource",
    "LifeActions",
    "LifeCandidates",
    "SleepSource",
]
