"""Goals and habits, connected to the runtime (rebuild spec 31 — Phase 8).

    既存 engine を Runtime に接続する。

That is the whole brief, and it is worth reading literally. `GoalEngine` and
`HabitEngine` were written long ago, covered by unit tests, and constructed at
startup — and nothing ever drove them. They are two of the six subsystems §0
names. This module is the connection, not a second engine.

§31.3 is the requirement with teeth:

    Opportunity generation 時に active goals / matching habit cues を
    必ず候補源にする。

So both are sources, always, rather than something consulted when the loop
happens to think of it.

One rule that shapes the code more than it looks: **a source may not write.**
``HabitEngine.cue_encountered`` increments the cue counter, which makes it a
writer and therefore unusable here — noticing that a cue is present is not the
same as encountering it, and if merely looking counted as an encounter then
every habit would strengthen on the strength of being looked at. The source
reads ``by_cue``; the handler is what calls the engine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Sequence

from app.agency.events import (
    GOAL_PURSUED,
    HABIT_PERFORMED,
    GoalPursuedPayload,
    HabitPerformedPayload,
)
from app.agency.models import ActionCandidate
from app.clock import Clock, SystemClock
from app.events.model import Event
from app.world.models import Opportunity

logger = logging.getLogger(__name__)

MODULE = "agency_runtime"

GOAL_STEP = "goal_step"
HABIT_CUE = "habit_cue"

PURSUE_GOAL = "pursue_goal"
DO_HABIT = "do_habit"

#: How long a goal is left alone after she has worked on it. Without this she
#: would pursue the same goal every wake-up, which is not diligence.
GOAL_COOLDOWN_HOURS = 6.0

#: The same for habits, per habit rather than globally.
HABIT_COOLDOWN_HOURS = 8.0

#: How long the work a step produces is expected to take.
GOAL_STEP_MINUTES = 40.0
HABIT_ACTION_MINUTES = 20.0


def present_cues(now: datetime, activity: Any | None) -> frozenset[str]:
    """What is true right now that a habit could hang off.

    Declared rather than inferred, and deliberately small. A cue is a feature
    of the situation she notices — the hour, and what she is doing. Making this
    open-ended would turn habit firing into a fuzzy match against the whole
    world state, which is how a habit system stops being explicable.
    """
    hour = now.hour
    cues = {"朝" if 21 <= hour or hour < 3 else "昼" if hour < 12 else "夜"}
    if activity is not None:
        cues.add(activity.name)
        cues.add(activity.kind)
    else:
        cues.add("手持ち無沙汰")
    return frozenset(cues)


class GoalSource:
    """Active goals, offered every time (§31.3). Reads only."""

    name = "goals"

    def __init__(
        self,
        goals: Any,
        world: Any,
        *,
        clock: Clock | None = None,
        cooldown_hours: float = GOAL_COOLDOWN_HOURS,
    ) -> None:
        self._goals = goals
        self._world = world
        self._clock = clock or SystemClock()
        self._cooldown = timedelta(hours=cooldown_hours)

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        if self._world.current_sleep() is not None:
            return []
        if self._world.current_activity() is not None:
            # She is already doing something. Spec 24.2's lifecycle finishes it
            # before anything else starts.
            return []

        opportunities: list[Opportunity] = []
        for goal in self._goals.active_goals(limit=5):
            last = goal.last_pursued_at
            if last is not None and now - last < self._cooldown:
                continue
            opportunities.append(
                Opportunity(
                    kind=GOAL_STEP,
                    detail=goal.goal_id,
                    urgency=round(min(1.0, goal.importance), 6),
                    created_at=now,
                )
            )
        return opportunities

    def next_due(self, now: datetime) -> datetime | None:
        """When the soonest goal comes off cooldown."""
        soonest: datetime | None = None
        for goal in self._goals.active_goals(limit=5):
            if goal.last_pursued_at is None:
                return now  # something is available already
            ready = goal.last_pursued_at + self._cooldown
            if soonest is None or ready < soonest:
                soonest = ready
        return soonest


class HabitSource:
    """Habits whose cue is present (§31.3). Reads only — see the module note."""

    name = "habits"

    def __init__(
        self,
        habits: Any,
        world: Any,
        *,
        policy: Any,
        clock: Clock | None = None,
        cooldown_hours: float = HABIT_COOLDOWN_HOURS,
    ) -> None:
        self._habits = habits
        self._world = world
        self._policy = policy
        self._clock = clock or SystemClock()
        self._cooldown = timedelta(hours=cooldown_hours)

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        if self._world.current_sleep() is not None:
            return []
        if self._world.current_activity() is not None:
            return []

        cues = present_cues(now, None)
        seen: set[str] = set()
        opportunities: list[Opportunity] = []
        for cue in cues:
            for habit in self._habits.by_cue(cue):
                if habit.habit_id in seen or not habit.context_available:
                    continue
                if (
                    habit.last_performed_at is not None
                    and now - habit.last_performed_at < self._cooldown
                ):
                    continue
                seen.add(habit.habit_id)
                opportunities.append(
                    Opportunity(
                        kind=HABIT_CUE,
                        detail=habit.habit_id,
                        # §31.2: automaticity is what makes a habit pull, and
                        # it is tracked rather than assumed after N days.
                        urgency=round(min(1.0, habit.automaticity), 6),
                        created_at=now,
                    )
                )
        return opportunities

    def next_due(self, now: datetime) -> datetime | None:
        """Cues turn on with the hour, so the next hour boundary is the answer."""
        return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


class AgencyCandidates:
    """What a goal step or a habit cue is worth (§31.1, spec 23).

    §31.1 names what Python evaluates: importance, autonomy, value alignment,
    feasibility, conflicts. The model may *propose* a goal; what a step towards
    one is worth right now is arithmetic, and it lives here rather than in a
    prompt.
    """

    name = "agency_candidates"

    def __init__(self, goals: Any, habits: Any, *, policy: Any) -> None:
        self._goals = goals
        self._habits = habits
        self._policy = policy

    def goal_step(
        self, opportunity: Opportunity, now: datetime
    ) -> ActionCandidate | None:
        goal = self._goals.goal(opportunity.detail)
        if goal is None or not goal.is_active:
            return None
        # §31.1. Wanting it counts for more than owing it, and a goal that
        # cuts against her values is worth less however important it looks.
        value = (
            0.40 * goal.importance
            + 0.25 * goal.autonomy
            + 0.20 * goal.value_alignment
            + 0.15 * goal.expected_reward
        )
        # Feasibility: something already most of the way done is worth less as
        # a *next step* than something with room left in it.
        value *= 1.0 - 0.3 * goal.progress
        return ActionCandidate(
            action=PURSUE_GOAL,
            route="goal_directed",
            expected_value=round(max(0.0, min(1.0, value)), 6),
            goal_id=goal.goal_id,
            reason=goal.description,
        )

    def habit_cue(
        self, opportunity: Opportunity, now: datetime
    ) -> ActionCandidate | None:
        habit = self._habits.get(opportunity.detail)
        if habit is None or not habit.context_available:
            return None
        # Spec 15.3: the habitual route is not the goal-directed one. An
        # established habit competes on automaticity, not on being a good idea.
        return ActionCandidate(
            action=DO_HABIT,
            route="habitual",
            expected_value=round(min(0.95, 0.25 + 0.7 * habit.automaticity), 6),
            habit_id=habit.habit_id,
            reason=habit.name,
        )


class AgencyActions:
    """Carries out a step or a habit, and owns the events.

    Both end in an *activity*, which is the reuse that matters: spec 24.2's
    lifecycle already knows how to finish something and turn it into a fact she
    can talk about (ACT-002). Building a second completion path for goal work
    would give the same rule two implementations and eventually two answers.
    """

    name = "agency_actions"

    def __init__(
        self,
        *,
        goals: Any,
        habits: Any,
        world: Any,
        processor: Any,
        candidates: AgencyCandidates,
        clock: Clock | None = None,
    ) -> None:
        self._goals = goals
        self._habits = habits
        self._world = world
        self._processor = processor
        self._candidates = candidates
        self._clock = clock or SystemClock()

    async def pursue_goal(self, candidate: ActionCandidate, now: datetime) -> bool:
        goal = self._goals.goal(candidate.goal_id or "")
        if goal is None or not goal.is_active:
            return False
        if self._world.current_activity() is not None:
            return False

        plan = self._open_plan_for(goal) or self._goals.plan(
            goal_id=goal.goal_id, description=f"{goal.description}に取り組む"
        )
        started_plan = self._goals.start_plan(plan.plan_id)
        activity, _ = self._world.start_activity(
            name=started_plan.description,
            kind="work",
            plan_id=started_plan.plan_id,
            expected_minutes=GOAL_STEP_MINUTES,
        )
        # Progress is *not* recorded here. Starting is not doing (ACT-001), and
        # a goal that advanced because she began something would be a plan
        # counted as an experience — the thing spec 2.15 forbids.
        updated = self._goals.touch(goal.goal_id, now=now)
        await self._emit(
            GOAL_PURSUED,
            GoalPursuedPayload(
                goal_id=goal.goal_id,
                description=goal.description,
                plan_id=started_plan.plan_id,
                activity_id=activity.activity_id,
                progress=updated.progress if updated is not None else goal.progress,
            ),
        )
        return True

    async def do_habit(self, candidate: ActionCandidate, now: datetime) -> bool:
        habit = self._habits.get(candidate.habit_id or "")
        if habit is None:
            return False
        if self._world.current_activity() is not None:
            return False

        # Now it is a real encounter, and now the engine may count it: the cue
        # was there and she acted on it (spec 15.3's pairing).
        outcome = self._habits.observe(
            name=habit.name,
            cue=habit.cue,
            action=habit.action,
            cue_present=True,
            performed=True,
        )
        activity, _ = self._world.start_activity(
            name=habit.action, kind="maintenance", expected_minutes=HABIT_ACTION_MINUTES
        )
        await self._emit(
            HABIT_PERFORMED,
            HabitPerformedPayload(
                habit_id=outcome.habit.habit_id,
                name=outcome.habit.name,
                cue=outcome.habit.cue,
                action=outcome.habit.action,
                automaticity=outcome.habit.automaticity,
                automatic=candidate.route == "habitual",
            ),
        )
        logger.info(
            "habit performed name=%s automaticity=%.3f",
            outcome.habit.name,
            outcome.habit.automaticity,
        )
        return True

    def register(self, registry: Any, *, sources: Sequence[Any] = ()) -> None:
        for source in sources:
            registry.add_source(source)
        registry.add_builder(GOAL_STEP, self._candidates.goal_step)
        registry.add_builder(HABIT_CUE, self._candidates.habit_cue)
        registry.add_handler(PURSUE_GOAL, self.pursue_goal)
        registry.add_handler(DO_HABIT, self.do_habit)

    # --- internals -----------------------------------------------------------
    def _open_plan_for(self, goal: Any) -> Any | None:
        for plan in self._goals.open_plans(limit=20):
            if plan.goal_id == goal.goal_id:
                return plan
        return None

    async def _emit(self, event_type: str, payload: Any) -> None:
        event = Event.create(
            event_type=event_type,
            category="action",
            actor_type="yui",
            source_type=MODULE,
            origin="virtual_life",
            priority="P4",
            payload=payload,
            clock=self._clock,
        )
        await self._processor.process(event)


__all__ = [
    "DO_HABIT",
    "GOAL_STEP",
    "HABIT_CUE",
    "PURSUE_GOAL",
    "AgencyActions",
    "AgencyCandidates",
    "GoalSource",
    "HabitSource",
    "present_cues",
]
