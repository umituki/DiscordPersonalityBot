"""Habit Engine — single writer of ``habits`` (spec 9.3, 15.3).

``Habit = repetition count ではなく cue-triggered automaticity``. Four rules
follow from that, and each one is a thing this engine refuses to do:

* automaticity grows from **context × repetition**, so a repetition without its
  cue builds nothing;
* a missed day does **not** reset anything — disuse decays slowly instead;
* when the context disappears the habit stops firing but its **trace remains**,
  above a floor, ready to come back if the cue returns;
* the goal-directed and habitual routes **coexist** — a strong habit fires from
  the cue without a goal, and a goal can still drive the same action.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.agency.policy import HabitPolicy
from app.clock import Clock, SystemClock
from app.storage.repositories.agency import HabitRepository
from app.agency.models import Habit

logger = logging.getLogger(__name__)

MODULE = "habit_engine"


@dataclass(frozen=True, slots=True)
class HabitOutcome:
    habit: Habit
    previous_automaticity: float
    fired: bool
    reason: str

    @property
    def strengthened(self) -> bool:
        return self.habit.automaticity > self.previous_automaticity


class HabitEngine:
    name = MODULE

    def __init__(
        self,
        repository: HabitRepository,
        policy: HabitPolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()

    # --- formation ---------------------------------------------------------
    def observe(
        self,
        *,
        name: str,
        cue: str,
        action: str,
        cue_present: bool,
        performed: bool,
    ) -> HabitOutcome:
        """Record one occasion: was the cue there, and was the action taken?"""
        now = self._clock.now()
        habit = self._repository.ensure(name=name, cue=cue, action=action, now=now)
        previous = habit.automaticity

        automaticity = self._decayed(habit, now)
        repetitions = habit.repetitions
        cue_encounters = habit.cue_encounters

        if cue_present:
            cue_encounters += 1
        if performed:
            repetitions += 1

        if cue_present and performed:
            # Only the pairing builds automaticity (spec 15.3).
            headroom = self._policy.automaticity_ceiling - automaticity
            automaticity = min(
                self._policy.automaticity_ceiling,
                automaticity + self._policy.automaticity_gain * max(0.0, headroom),
            )
            reason = "cue_and_action"
        elif performed:
            reason = "action_without_cue"
        elif cue_present:
            reason = "cue_without_action"
        else:
            reason = "neither"

        status = (
            "established" if automaticity >= self._policy.established_at else habit.status
        )
        updated = self._repository.update(
            habit_id=habit.habit_id,
            automaticity=round(automaticity, 6),
            repetitions=repetitions,
            cue_encounters=cue_encounters,
            context_available=habit.context_available,
            last_performed_at=now if performed else habit.last_performed_at,
            last_cue_at=now if cue_present else habit.last_cue_at,
            status=status,
            now=now,
        )
        return HabitOutcome(updated, previous, fired=False, reason=reason)

    # --- expression --------------------------------------------------------
    def cue_encountered(self, cue: str) -> list[HabitOutcome]:
        """Which habits fire on this cue, without any goal being involved."""
        now = self._clock.now()
        outcomes: list[HabitOutcome] = []
        for habit in self._repository.by_cue(cue):
            automaticity = self._decayed(habit, now)
            fires = (
                habit.context_available
                and automaticity >= self._policy.automatic_trigger
            )
            updated = self._repository.update(
                habit_id=habit.habit_id,
                automaticity=round(automaticity, 6),
                repetitions=habit.repetitions,
                cue_encounters=habit.cue_encounters + 1,
                context_available=habit.context_available,
                last_performed_at=habit.last_performed_at,
                last_cue_at=now,
                status=habit.status,
                now=now,
            )
            outcomes.append(
                HabitOutcome(
                    updated,
                    habit.automaticity,
                    fired=fires,
                    reason="automatic" if fires else "below_trigger",
                )
            )
        return outcomes

    def set_context_available(self, habit_id: str, available: bool) -> Habit:
        """The context vanished (or came back). The trace survives either way."""
        now = self._clock.now()
        habit = self._repository.get(habit_id)
        if habit is None:
            raise KeyError(f"unknown habit: {habit_id}")
        return self._repository.update(
            habit_id=habit_id,
            automaticity=habit.automaticity,
            repetitions=habit.repetitions,
            cue_encounters=habit.cue_encounters,
            context_available=available,
            last_performed_at=habit.last_performed_at,
            last_cue_at=habit.last_cue_at,
            status="dormant" if not available else habit.status,
            now=now,
        )

    def apply_disuse(self, *, now: datetime | None = None) -> int:
        """Slow decay for habits that have not been performed lately."""
        moment = now or self._clock.now()
        changed = 0
        for habit in self._repository.all():
            decayed = self._decayed(habit, moment)
            if abs(decayed - habit.automaticity) > 1e-9:
                self._repository.update(
                    habit_id=habit.habit_id,
                    automaticity=round(decayed, 6),
                    repetitions=habit.repetitions,
                    cue_encounters=habit.cue_encounters,
                    context_available=habit.context_available,
                    last_performed_at=habit.last_performed_at,
                    last_cue_at=habit.last_cue_at,
                    status=habit.status,
                    now=moment,
                )
                changed += 1
        return changed

    # --- helpers -----------------------------------------------------------
    def _decayed(self, habit: Habit, now: datetime) -> float:
        """Disuse lowers automaticity gently, and never below the trace floor."""
        reference = habit.last_performed_at or habit.created_at
        idle_days = max(0.0, (now - reference).total_seconds() / 86400.0)
        if idle_days <= 0:
            return habit.automaticity
        decayed = habit.automaticity - self._policy.decay_per_idle_day * idle_days
        floor = min(self._policy.trace_floor, habit.automaticity)
        return max(floor, decayed)

    def habit(self, name: str, cue: str) -> Habit | None:
        return self._repository.find(name, cue)

    def get(self, habit_id: str) -> Habit | None:
        return self._repository.get(habit_id)

    def by_cue(self, cue: str) -> list[Habit]:
        """Habits hanging off this cue. A read — noticing is not encountering.

        ``cue_encountered`` counts an encounter and writes; a source that used
        it would strengthen every habit merely by looking at it.
        """
        return self._repository.by_cue(cue)

    def established(self) -> list[Habit]:
        return self._repository.established(self._policy.established_at)
