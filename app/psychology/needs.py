"""Need Engine — single writer of the ``needs`` domain (spec 9.3, 15.1).

Needs tracked here:

* autonomy / competence / relatedness satisfaction,
* loneliness, connection desire, solitude desire.

Spec 15.1 is explicit that ``Loneliness と solitude desire は独立して高くなりうる``.
They are separate keys moved by separate causes: loneliness grows with time
alone and is relieved by contact, while solitude desire grows *with* contact.
Neither is computed from the other.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.psychology.events import NEED_CHANGED, NeedChangedPayload
from app.psychology.policy import NeedsPolicy
from app.state.proposal import StateChangeProposal

logger = logging.getLogger(__name__)

DOMAIN = "needs"
MODULE = "need_engine"

RELATEDNESS = "relatedness_satisfaction"
AUTONOMY = "autonomy_satisfaction"
COMPETENCE = "competence_satisfaction"
LONELINESS = "loneliness"
CONNECTION_DESIRE = "connection_desire"
SOLITUDE_DESIRE = "solitude_desire"

DEFAULTS: dict[str, float] = {
    RELATEDNESS: 0.5,
    AUTONOMY: 0.5,
    COMPETENCE: 0.5,
    LONELINESS: 0.2,
    CONNECTION_DESIRE: 0.3,
    SOLITUDE_DESIRE: 0.2,
}


class NeedEngine:
    name = MODULE

    def __init__(self, policy: NeedsPolicy, *, clock: Clock | None = None) -> None:
        self._policy = policy
        self._clock = clock or SystemClock()

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        now = self._clock.now()
        is_contact = event.actor_type == "user"
        idle_hours = self._idle_hours(view, now)

        targets: dict[str, float] = {}
        current = {key: self._current(view, key) for key in DEFAULTS}

        # --- time alone ----------------------------------------------------
        if idle_hours > 0:
            targets[LONELINESS] = (
                current[LONELINESS] + self._policy.loneliness_gain_per_idle_hour * idle_hours
            )
            targets[RELATEDNESS] = (
                current[RELATEDNESS] - self._policy.relatedness_decay_per_hour * idle_hours
            )
            targets[SOLITUDE_DESIRE] = (
                current[SOLITUDE_DESIRE]
                - self._policy.solitude_desire_decay_per_hour * idle_hours
            )

        # --- contact -------------------------------------------------------
        if is_contact:
            base_relatedness = targets.get(RELATEDNESS, current[RELATEDNESS])
            targets[RELATEDNESS] = (
                base_relatedness + self._policy.relatedness_gain_per_interaction
            )
            targets[LONELINESS] = (
                targets.get(LONELINESS, current[LONELINESS])
                - self._policy.loneliness_relief_per_interaction
            )
            # Being with someone also feeds the wish for time alone. Both can
            # be high at once (spec 15.1).
            targets[SOLITUDE_DESIRE] = (
                targets.get(SOLITUDE_DESIRE, current[SOLITUDE_DESIRE])
                + self._policy.solitude_desire_gain_per_interaction
            )

        if event.actor_type == "yui":
            targets[AUTONOMY] = (
                current[AUTONOMY] + self._policy.autonomy_gain_on_self_initiated
            )

        # --- desire follows from loneliness, not the other way round --------
        if LONELINESS in targets:
            loneliness = _clamp(targets[LONELINESS])
            targets[CONNECTION_DESIRE] = (
                0.5 * current[CONNECTION_DESIRE]
                + 0.5 * self._policy.connection_desire_from_loneliness * loneliness
            )

        proposals: list[StateChangeProposal] = []
        events: list[Event] = []
        for key, raw_target in targets.items():
            previous = current[key]
            new_value = _clamp(self._bounded(previous, raw_target))
            if abs(new_value - previous) < 1e-6:
                continue
            proposals.append(
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module=MODULE,
                    target_domain=DOMAIN,
                    target_key=key,
                    value=round(new_value, 6),
                    reason_codes=("contact",) if is_contact else ("time_alone",),
                    clock=self._clock,
                )
            )
            events.append(
                event.child(
                    event_type=NEED_CHANGED,
                    category="internal",
                    actor_type="yui",
                    source_type=MODULE,
                    clock=self._clock,
                    priority="P4",
                    payload=NeedChangedPayload(
                        key=key,
                        value=round(new_value, 6),
                        previous_value=round(previous, 6),
                        reason_code="contact" if is_contact else "time_alone",
                    ),
                )
            )

        return SubscriberResult(proposals=tuple(proposals), events=tuple(events))

    # --- helpers -----------------------------------------------------------
    def _current(self, view: RunView, key: str) -> float:
        value = view.number(DOMAIN, key)
        return DEFAULTS[key] if value is None else value

    def _idle_hours(self, view: RunView, now: datetime) -> float:
        entry = view.snapshot.get(DOMAIN, RELATEDNESS)
        if entry is None:
            return 0.0
        return max(0.0, (now - entry.updated_at).total_seconds() / 3600.0)

    def _bounded(self, previous: float, target: float) -> float:
        limit = self._policy.max_change_per_event
        delta = max(-limit, min(limit, target - previous))
        return previous + delta


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
