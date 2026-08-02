"""Decision pipeline (spec 15.4).

::

    CURRENT STATE → NEEDS / EMOTION / CONTEXT → GOALS → ACTION CANDIDATES
    → EXPECTED OUTCOMES → ACTION SELECTION → ACTION → OUTCOME
    → PREDICTION ERROR → LEARNING

The selection rule is the part the specification is specific about:
``近い候補では小さい stochasticity を許す。大差候補を純 RNG にしない``. So noise is
applied only inside the tie band; a clearly better option is chosen every time.

Randomness comes from a seeded stream so a run can be reproduced (spec 29).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Sequence

from app import ids
from app.agency.models import ActionCandidate, DecisionRecord
from app.agency.policy import DecisionPolicy
from app.clock import Clock, SystemClock
from app.storage.repositories.agency import DecisionRepository

logger = logging.getLogger(__name__)

MODULE = "decision_engine"
DECISION = "dec"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What actually came of a decision, for learning (spec 15.4)."""

    decision_id: str
    achieved_value: float
    expected_value: float

    @property
    def prediction_error(self) -> float:
        return round(self.achieved_value - self.expected_value, 6)


class DecisionEngine:
    name = MODULE

    def __init__(
        self,
        repository: DecisionRepository,
        policy: DecisionPolicy,
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()
        # Spec 29: randomness is drawn from a named, seedable stream.
        self._rng = rng or random.Random(0)

    def choose(
        self,
        candidates: Sequence[ActionCandidate],
        *,
        run_id: str | None = None,
        event_id: str | None = None,
        record: bool = True,
    ) -> DecisionRecord | None:
        """Pick one action. Ties may waver; clear differences may not."""
        if not candidates:
            return None

        ranked = sorted(candidates, key=lambda item: item.expected_value, reverse=True)
        best = ranked[0]
        contenders = [
            candidate
            for candidate in ranked
            if best.expected_value - candidate.expected_value <= self._policy.tie_threshold
        ]

        was_close = len(contenders) > 1
        if was_close:
            # Only inside the tie band does chance get a say.
            noisy = [
                (
                    candidate,
                    candidate.expected_value
                    + self._rng.uniform(0.0, self._policy.exploration_noise),
                )
                for candidate in contenders
            ]
            chosen = max(noisy, key=lambda item: item[1])[0]
        else:
            chosen = best

        decision = DecisionRecord(
            decision_id=ids.new_id(DECISION),
            chosen=chosen,
            considered=tuple(ranked),
            was_close=was_close,
            decided_at=self._clock.now(),
            run_id=run_id,
            event_id=event_id,
        )
        if record:
            self._repository.record(
                decision_id=decision.decision_id,
                run_id=run_id,
                event_id=event_id,
                chosen=chosen,
                candidates=ranked,
                now=decision.decided_at,
            )
        logger.debug(
            "decision made action=%s route=%s close=%s",
            chosen.action,
            chosen.route,
            was_close,
        )
        return decision

    # --- learning ----------------------------------------------------------
    def resolve(self, decision: DecisionRecord, *, achieved_value: float) -> Outcome:
        """Record what happened and the resulting prediction error."""
        outcome = Outcome(
            decision_id=decision.decision_id,
            achieved_value=max(0.0, min(1.0, achieved_value)),
            expected_value=decision.chosen.expected_value,
        )
        self._repository.resolve(
            decision_id=decision.decision_id,
            outcome_value=outcome.achieved_value,
            prediction_error=outcome.prediction_error,
            now=self._clock.now(),
        )
        return outcome

    def updated_expectation(self, previous: float, outcome: Outcome) -> float:
        """Move the expectation towards what actually happened (spec 15.4)."""
        adjusted = previous + self._policy.learning_rate * outcome.prediction_error
        return round(max(0.0, min(1.0, adjusted)), 6)
