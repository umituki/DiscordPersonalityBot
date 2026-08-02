"""Mood Engine — single writer of the ``mood`` domain (spec 9.3, 11.3).

Mood is not an emotion. It is diffuse, slower, and it drifts back towards a
baseline when nothing is happening. It reads the current emotions from S0 — a
cross-domain *read*, which is allowed — and proposes a small step; it never
writes emotion, and emotion never writes mood.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.psychology.events import MOOD_SHIFTED, MoodShiftedPayload
from app.psychology.policy import MoodPolicy
from app.state.proposal import StateChangeProposal

logger = logging.getLogger(__name__)

DOMAIN = "mood"
MODULE = "mood_engine"

VALENCE = "valence"
AROUSAL = "arousal"

#: How each emotion pulls valence and arousal.
EMOTION_TONE: dict[str, tuple[float, float]] = {
    "joy": (1.0, 0.6),
    "affection": (0.8, 0.3),
    "interest": (0.4, 0.5),
    "surprise": (0.0, 0.8),
    "sadness": (-0.9, -0.3),
    "fear": (-0.7, 0.7),
    "anger": (-0.8, 0.8),
}


class MoodEngine:
    name = MODULE

    def __init__(self, policy: MoodPolicy, *, clock: Clock | None = None) -> None:
        self._policy = policy
        self._clock = clock or SystemClock()

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        now = self._clock.now()
        current_valence = view.number(DOMAIN, VALENCE, self._policy.baseline_valence)
        current_arousal = view.number(DOMAIN, AROUSAL, self._policy.baseline_arousal)
        assert current_valence is not None and current_arousal is not None

        drifted_valence = self._drift(current_valence, self._policy.baseline_valence, view, now)
        drifted_arousal = self._drift(current_arousal, self._policy.baseline_arousal, view, now)

        target_valence, target_arousal = self._emotional_tone(view)
        new_valence = self._step(drifted_valence, target_valence)
        new_arousal = self._step(drifted_arousal, target_arousal)

        proposals = []
        for key, previous, new_value in (
            (VALENCE, current_valence, new_valence),
            (AROUSAL, current_arousal, new_arousal),
        ):
            if abs(new_value - previous) < 1e-6:
                continue
            proposals.append(
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module=MODULE,
                    target_domain=DOMAIN,
                    target_key=key,
                    value=round(new_value, 6),
                    reason_codes=("emotional_tone", "baseline_drift"),
                    clock=self._clock,
                )
            )

        if not proposals:
            return SubscriberResult()

        shifted = event.child(
            event_type=MOOD_SHIFTED,
            category="internal",
            actor_type="yui",
            source_type=MODULE,
            clock=self._clock,
            priority="P4",
            payload=MoodShiftedPayload(
                valence=round(new_valence, 6),
                arousal=round(new_arousal, 6),
                previous_valence=round(current_valence, 6),
                previous_arousal=round(current_arousal, 6),
                reason_code="emotional_tone",
            ),
        )
        return SubscriberResult(proposals=tuple(proposals), events=(shifted,))

    # --- pieces ------------------------------------------------------------
    def _emotional_tone(self, view: RunView) -> tuple[float, float]:
        """Where the current emotions would put mood if they persisted."""
        emotions = view.snapshot.domain("emotion")
        if not emotions:
            return self._policy.baseline_valence, self._policy.baseline_arousal

        weight_total = 0.0
        valence_sum = 0.0
        arousal_sum = 0.0
        for name, value in emotions.items():
            intensity = value.numeric
            if intensity is None or intensity <= 0:
                continue
            tone = EMOTION_TONE.get(name)
            if tone is None:
                continue
            valence_sum += tone[0] * intensity
            arousal_sum += tone[1] * intensity
            weight_total += intensity

        if weight_total <= 0:
            return self._policy.baseline_valence, self._policy.baseline_arousal

        valence = self._policy.baseline_valence + 0.5 * (valence_sum / weight_total)
        arousal = self._policy.baseline_arousal + 0.5 * (arousal_sum / weight_total)
        return max(0.0, min(1.0, valence)), max(0.0, min(1.0, arousal))

    def _drift(self, current: float, baseline: float, view: RunView, now: datetime) -> float:
        entry = view.snapshot.get(DOMAIN, VALENCE)
        if entry is None:
            return current
        hours = max(0.0, (now - entry.updated_at).total_seconds() / 3600.0)
        pull = min(1.0, self._policy.return_rate_per_hour * hours)
        return current + (baseline - current) * pull

    def _step(self, current: float, target: float) -> float:
        blended = self._policy.inertia * current + (1.0 - self._policy.inertia) * target
        limit = self._policy.max_change_per_event
        delta = max(-limit, min(limit, blended - current))
        return max(0.0, min(1.0, current + delta))
