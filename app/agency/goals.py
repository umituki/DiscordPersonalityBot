"""Goal Engine — single writer of ``goals`` (spec 9.3, 15.2).

::

    Need / Emotion / Value / Interest → Goal → Intention → Plan → Action

A goal is not a task list entry: it carries why it exists, how much of it is
wanted rather than owed, and what it has to do with who YUI is. Those fields
are what later phases weigh when deciding whether to act on it.

Plans stay strictly separate from what happened. ``complete_plan`` is the only
way a plan becomes a completed experience, and it requires the action to have
actually been carried out (spec 2.15, 18.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.agency.models import Goal, Plan
from app.agency.policy import GoalPolicy
from app.clock import Clock, SystemClock
from app.events.model import EventOrigin
from app.orchestrator.run_view import RunView
from app.storage.repositories.agency import GoalRepository, PlanRepository

logger = logging.getLogger(__name__)

MODULE = "goal_engine"

#: Which need pushes towards which goal, and what the goal would be.
NEED_GOALS: dict[str, tuple[str, str]] = {
    "loneliness": ("connection", "だれかと話したい"),
    "connection_desire": ("connection", "だれかと話したい"),
    "solitude_desire": ("solitude", "しばらくひとりで過ごしたい"),
}


@dataclass(frozen=True, slots=True)
class GoalActivation:
    goal: Goal
    driver: str
    pressure: float
    newly_formed: bool


class GoalEngine:
    name = MODULE

    def __init__(
        self,
        goals: GoalRepository,
        plans: PlanRepository,
        policy: GoalPolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._goals = goals
        self._plans = plans
        self._policy = policy
        self._clock = clock or SystemClock()

    # --- formation ---------------------------------------------------------
    def derive_from_state(
        self, view: RunView, *, origin: EventOrigin = "real_discord"
    ) -> list[GoalActivation]:
        """Turn pressing needs into goals. Mild needs produce nothing."""
        activations: list[GoalActivation] = []
        for need_key, (name, description) in NEED_GOALS.items():
            pressure = view.number("needs", need_key)
            if pressure is None or pressure < self._policy.activation_threshold:
                continue

            existing = self._goals.find(description, "need")
            importance = min(1.0, self._policy.importance_from_need * pressure + 0.2)
            goal = self._goals.upsert(
                description=description,
                source="need",
                reason=f"{need_key}={pressure:.2f}",
                importance=importance,
                autonomy=0.7,
                obligation=0.0,
                expected_reward=min(1.0, pressure),
                identity_relevance=0.3,
                value_alignment=0.5,
                origin=origin,
                now=self._clock.now(),
            )
            activations.append(
                GoalActivation(goal, need_key, pressure, newly_formed=existing is None)
            )

        return sorted(
            activations, key=lambda item: item.goal.importance, reverse=True
        )[: self._policy.max_active]

    def adopt(
        self,
        *,
        description: str,
        source: str,
        reason: str,
        importance: float = 0.5,
        autonomy: float = 0.5,
        obligation: float = 0.0,
        expected_reward: float = 0.5,
        identity_relevance: float = 0.3,
        value_alignment: float = 0.5,
        origin: EventOrigin = "real_discord",
    ) -> Goal:
        return self._goals.upsert(
            description=description,
            source=source,
            reason=reason,
            importance=importance,
            autonomy=autonomy,
            obligation=obligation,
            expected_reward=expected_reward,
            identity_relevance=identity_relevance,
            value_alignment=value_alignment,
            origin=origin,
            now=self._clock.now(),
        )

    # --- pursuit -----------------------------------------------------------
    def record_progress(self, goal_id: str, *, amount: float | None = None) -> Goal:
        goal = self._goals.get(goal_id)
        if goal is None:
            raise KeyError(f"unknown goal: {goal_id}")
        step = self._policy.progress_step if amount is None else amount
        progress = max(0.0, min(1.0, goal.progress + step))
        status = "achieved" if progress >= 1.0 else goal.status
        return self._goals.update_progress(
            goal_id=goal_id, progress=progress, status=status, now=self._clock.now()
        )

    def retire_stale(self, *, now: datetime | None = None) -> int:
        """Goals nobody has touched in a long time go dormant, not away."""
        moment = now or self._clock.now()
        cutoff = timedelta(days=self._policy.abandon_after_idle_days)
        retired = 0
        for goal in self._goals.active(limit=100):
            reference = goal.last_pursued_at or goal.created_at
            if moment - reference >= cutoff:
                self._goals.set_status(goal.goal_id, "dormant", now=moment)
                retired += 1
        return retired

    def active_goals(self, *, limit: int = 10) -> list[Goal]:
        return self._goals.active(limit=limit)

    # --- plans (spec 18.2) --------------------------------------------------
    def plan(
        self, *, goal_id: str | None, description: str, planned_for: datetime | None = None
    ) -> Plan:
        """Intend something. This is a plan, and only a plan."""
        return self._plans.create(
            goal_id=goal_id,
            description=description,
            planned_for=planned_for,
            now=self._clock.now(),
        )

    def start_plan(self, plan_id: str) -> Plan:
        return self._plans.transition(
            plan_id=plan_id, status="in_progress", now=self._clock.now()
        )

    def complete_plan(self, plan_id: str, *, outcome: str) -> Plan:
        """Record that the planned thing actually happened (spec 2.15)."""
        return self._plans.transition(
            plan_id=plan_id, status="completed", now=self._clock.now(), outcome=outcome
        )

    def miss_plan(self, plan_id: str, *, reason: str = "") -> Plan:
        """The time passed and it did not happen. Never a completion."""
        return self._plans.transition(
            plan_id=plan_id, status="missed", now=self._clock.now(), outcome=reason or None
        )

    def cancel_plan(self, plan_id: str, *, reason: str = "") -> Plan:
        return self._plans.transition(
            plan_id=plan_id, status="cancelled", now=self._clock.now(), outcome=reason or None
        )

    def completed_experiences(self, *, limit: int = 50) -> list[Plan]:
        return self._plans.completed(limit=limit)

    def open_plans(self, *, limit: int = 50) -> list[Plan]:
        return self._plans.with_status("planned", limit=limit)
