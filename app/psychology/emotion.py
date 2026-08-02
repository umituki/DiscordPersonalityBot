"""Emotion Engine — single writer of the ``emotion`` domain (spec 9.3, 11.2).

From appraisal to emotion, in Python:

* several emotions may activate at once (mixed emotion is allowed),
* intensity and duration are separate: intensity comes from the appraisal,
  duration from decay since the value was last written,
* below the activation threshold, nothing happened at all,
* each activation records its trigger, target, cause and whether it is
  unresolved, plus the action tendency it implies — never a behaviour.

The engine proposes. It does not write.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.psychology.events import EMOTION_ACTIVATED, EmotionActivatedPayload
from app.psychology.models import ACTION_TENDENCIES, Appraisal
from app.psychology.policy import EmotionPolicy
from app.state.proposal import StateChangeProposal

logger = logging.getLogger(__name__)

DOMAIN = "emotion"
MODULE = "emotion_engine"


class EmotionEngine:
    name = MODULE

    def __init__(self, policy: EmotionPolicy, *, clock: Clock | None = None) -> None:
        self._policy = policy
        self._clock = clock or SystemClock()

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        appraisal = view.appraisal
        if not isinstance(appraisal, Appraisal):
            # No interpretation, no emotion. Silence is the safe outcome.
            return SubscriberResult()

        # Patch spec 13: the run's time, not the machine's. In a simulation
        # these are decades apart.
        now = view.now(self._clock)
        proposals: list[StateChangeProposal] = []
        events: list[Event] = []

        activations = self._activations(appraisal)
        for name, target_intensity in activations:
            current = self._decayed_current(view, name, now)
            delta = self._bounded_delta(current, target_intensity)
            if abs(delta) < 1e-6:
                continue

            new_value = max(0.0, min(1.0, current + delta))
            proposals.append(self._proposal(event, view, name, current, new_value, appraisal))
            events.append(
                event.child(
                    event_type=EMOTION_ACTIVATED,
                    category="internal",
                    actor_type="yui",
                    source_type=MODULE,
                    clock=self._clock,
                    priority="P2",
                    payload=EmotionActivatedPayload(
                        name=name,
                        intensity=round(new_value, 6),
                        previous_intensity=round(current, 6),
                        action_tendency=ACTION_TENDENCIES.get(name, "none"),
                        trigger_event_id=event.event_id,
                        target_type=event.actor_type,
                        target_id=event.actor_id,
                        cause=appraisal.reason[:200],
                        # An emotion with no resolution path stays unresolved
                        # until something changes it (spec 11.2).
                        unresolved=appraisal.certainty < 0.4 or appraisal.control < 0.4,
                        appraisal_source=appraisal.source,
                        appraisal_confidence=appraisal.confidence,
                    ),
                )
            )

        if proposals:
            logger.debug(
                "emotion proposals event_id=%s emotions=%s",
                event.event_id,
                [proposal.target_key for proposal in proposals],
            )
        return SubscriberResult(proposals=tuple(proposals), events=tuple(events))

    # --- appraisal → intensity --------------------------------------------
    def _activations(self, appraisal: Appraisal) -> list[tuple[str, float]]:
        dimensions = appraisal.as_dimension_map()
        scored: list[tuple[str, float]] = []
        for name, weights in self._policy.dimensions.items():
            raw = sum(weight * dimensions.get(dim, 0.0) for dim, weight in weights.items())
            # Self-relevance gates how much anything is felt at all.
            intensity = max(0.0, min(1.0, raw)) * (0.4 + 0.6 * appraisal.self_relevance)
            if intensity >= self._policy.activation_threshold:
                scored.append((name, intensity))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[: self._policy.max_concurrent]

    def _decayed_current(self, view: RunView, name: str, now: datetime) -> float:
        """Current intensity, decayed for the time since it was last written."""
        entry = view.snapshot.get(DOMAIN, name)
        if entry is None or entry.numeric is None:
            return 0.0
        minutes = max(0.0, (now - entry.updated_at).total_seconds() / 60.0)
        if minutes <= 0:
            return entry.numeric
        return entry.numeric * math.pow(0.5, minutes / self._policy.half_life_minutes)

    def _bounded_delta(self, current: float, target: float) -> float:
        delta = target - current
        limit = self._policy.max_intensity_change
        return max(-limit, min(limit, delta))

    def _proposal(
        self,
        event: Event,
        view: RunView,
        name: str,
        current: float,
        new_value: float,
        appraisal: Appraisal,
    ) -> StateChangeProposal:
        reason_codes = (f"appraisal_{appraisal.source}", f"emotion_{name}")
        if not view.known(DOMAIN, name):
            reason_codes = (*reason_codes, "first_activation")
        return StateChangeProposal.set_value(
            source_event_id=event.event_id,
            source_module=MODULE,
            target_domain=DOMAIN,
            target_key=name,
            value=round(new_value, 6),
            confidence=appraisal.confidence,
            reason_codes=reason_codes,
            clock=self._clock,
        )
