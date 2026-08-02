"""Proactive contact (spec 19).

::

    Opportunity → current activity / needs / goals / relationship
    / user availability estimate → Decision Engine → send or do not send

The rule the specification states outright is a prohibition:

    ``USER が反応しないほど送信頻度が増える positive feedback を禁止する``

So silence *widens* the interval. Every unanswered message multiplies the wait
before another may be sent, and past a small number of unanswered contacts
nothing is sent at all until the USER speaks again. This module produces an
action candidate; the Decision Engine still has to choose it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.agency.models import ActionCandidate
from app.clock import Clock, SystemClock
from app.orchestrator.run_view import RunView
from app.storage.repositories.world import ProactiveRepository
from app.world.models import Opportunity
from app.world.policy import ProactivePolicy

logger = logging.getLogger(__name__)

MODULE = "proactive_engine"


@dataclass(frozen=True, slots=True)
class ContactAssessment:
    """Whether reaching out is even permissible, and how much YUI wants to."""

    allowed: bool
    reason: str
    desire: float = 0.0
    required_wait_hours: float = 0.0
    unanswered: int = 0

    @property
    def candidate(self) -> ActionCandidate | None:
        if not self.allowed:
            return None
        return ActionCandidate(
            action="proactive_contact",
            route="goal_directed",
            expected_value=self.desire,
            reason=self.reason,
        )


class ProactiveEngine:
    name = MODULE

    def __init__(
        self,
        repository: ProactiveRepository,
        policy: ProactivePolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()

    def assess(
        self,
        opportunity: Opportunity,
        view: RunView,
        *,
        now: datetime | None = None,
        user_probably_awake: bool = True,
    ) -> ContactAssessment:
        moment = now or self._clock.now()
        unanswered = self._repository.unanswered_count()

        # --- the anti-feedback rule (spec 19) ------------------------------
        if unanswered >= self._policy.max_unanswered:
            return ContactAssessment(
                allowed=False,
                reason="too many unanswered messages already",
                unanswered=unanswered,
            )

        required_wait = self._policy.min_hours_between_contacts * (
            self._policy.unanswered_backoff_multiplier ** unanswered
        )
        last = self._repository.last_contact_at()
        if last is not None and moment - last < timedelta(hours=required_wait):
            return ContactAssessment(
                allowed=False,
                reason="too soon since the last message",
                required_wait_hours=required_wait,
                unanswered=unanswered,
            )

        if not user_probably_awake or self._in_quiet_hours(moment):
            return ContactAssessment(
                allowed=False,
                reason="the USER is probably not available",
                unanswered=unanswered,
            )

        # --- how much YUI actually wants to --------------------------------
        loneliness = view.number("needs", "loneliness", 0.0) or 0.0
        connection = view.number("needs", "connection_desire", 0.0) or 0.0
        closeness = view.number("relationship", "emotional_closeness", 0.3) or 0.3
        solitude = view.number("needs", "solitude_desire", 0.0) or 0.0

        desire = max(
            0.0,
            min(
                1.0,
                0.45 * connection + 0.35 * loneliness + 0.20 * closeness - 0.30 * solitude
                + 0.15 * opportunity.urgency,
            ),
        )
        if desire < self._policy.base_threshold:
            return ContactAssessment(
                allowed=False,
                reason="nothing pressing enough to say",
                desire=round(desire, 6),
                unanswered=unanswered,
            )

        return ContactAssessment(
            allowed=True,
            reason=f"{opportunity.kind}: wanted contact",
            desire=round(desire, 6),
            required_wait_hours=required_wait,
            unanswered=unanswered,
        )

    # --- bookkeeping -------------------------------------------------------
    def record_sent(self, opportunity: Opportunity, *, event_id: str | None = None) -> str:
        return self._repository.record_contact(
            opportunity=opportunity.kind, now=self._clock.now(), event_id=event_id
        )

    def note_user_replied(self) -> int:
        """A reply clears the backlog; the interval returns to normal."""
        return self._repository.mark_answered(now=self._clock.now())

    def unanswered(self) -> int:
        return self._repository.unanswered_count()

    def _in_quiet_hours(self, moment: datetime) -> bool:
        start, end = self._policy.quiet_hours_utc
        hour = moment.hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end
