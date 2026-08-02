"""Social Cognition Engine — single writer of ``user_model`` (spec 9.3, 14).

Spec 14 separates what the USER objectively did (the archive) from what YUI
*thinks* about them, and then separates that again:

* **state estimates** — "they seem tired right now". Fast, decaying, cheap to be
  wrong about.
* **trait estimates** — "they are a private person". Slow, and only moved by
  repeated observation across occasions. ``一時状態を Trait へ即一般化しない``
  is the rule this engine exists to keep (spec 14, 34.3).
* **provisional impressions** — an estimate built on very few observations is
  marked provisional, not treated as knowledge (spec 14: thin first impressions
  are provisional).

When an estimate turns out wrong, that is recorded as a model error, not as the
USER having changed. Distinguishing ``person_changed`` from ``model_was_wrong``
needs evidence over time, so the engine only ever flags the discrepancy here.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.psychology.models import Appraisal
from app.social.events import USER_MODEL_UPDATED, UserModelUpdatedPayload
from app.social.policy import UserModelPolicy
from app.social.signals import signals_from
from app.state.proposal import StateChangeProposal

logger = logging.getLogger(__name__)

DOMAIN = "user_model"
#: Bookkeeping counters live apart from the bounded model values.
COUNTER_DOMAIN = "user_model_counters"
MODULE = "social_cognition_engine"

#: Fast, decaying reads of how the USER seems right now.
STATE_PREFIX = "state_"
#: Slow reads of what the USER is generally like.
TRAIT_PREFIX = "trait_"

OBSERVATION_COUNT = "observation_count"
MODEL_ERRORS = "model_error_count"
#: 1.0 while the model rests on too few observations to be trusted.
PROVISIONAL = "provisional"

STATE_WARMTH = f"{STATE_PREFIX}warmth"
STATE_ENERGY = f"{STATE_PREFIX}energy"
STATE_DISTRESS = f"{STATE_PREFIX}distress"
TRAIT_WARMTH = f"{TRAIT_PREFIX}warmth"
TRAIT_EXPRESSIVENESS = f"{TRAIT_PREFIX}expressiveness"

#: Evidence tally backing each trait, so a trait cannot move without repetition.
TRAIT_EVIDENCE = {
    TRAIT_WARMTH: "evidence_warmth",
    TRAIT_EXPRESSIVENESS: "evidence_expressiveness",
}

DEFAULTS: dict[str, float] = {
    STATE_WARMTH: 0.5,
    STATE_ENERGY: 0.5,
    STATE_DISTRESS: 0.2,
    TRAIT_WARMTH: 0.5,
    TRAIT_EXPRESSIVENESS: 0.5,
    "evidence_warmth": 0.0,
    "evidence_expressiveness": 0.0,
    OBSERVATION_COUNT: 0.0,
    MODEL_ERRORS: 0.0,
    PROVISIONAL: 1.0,
}


class SocialCognitionEngine:
    name = MODULE

    def __init__(self, policy: UserModelPolicy, *, clock: Clock | None = None) -> None:
        self._policy = policy
        self._clock = clock or SystemClock()

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        if event.actor_type != "user":
            # Only the USER is modelled here. An NPC is a different subject
            # entirely and never merges into this model (spec 2.10).
            return SubscriberResult()

        appraisal = view.appraisal if isinstance(view.appraisal, Appraisal) else None
        signals = signals_from(event, appraisal)
        now = self._clock.now()
        text = getattr(event.payload, "text", "") or ""

        current = {key: self._current(view, key) for key in DEFAULTS}
        observations = current[OBSERVATION_COUNT] + 1.0
        targets: dict[str, float] = {OBSERVATION_COUNT: observations}

        # --- state estimates: fast, and decaying ---------------------------
        idle_hours = self._idle_hours(view, now)
        observed = self._observe(text, signals)
        for key, observed_value in observed.items():
            decayed = self._decayed(current[key], DEFAULTS[key], idle_hours)
            rate = self._policy.state_estimate_rate
            targets[key] = decayed + rate * (observed_value - decayed)

        # --- traits: only repetition moves them ----------------------------
        for trait, evidence_key in TRAIT_EVIDENCE.items():
            state_key = f"{STATE_PREFIX}{trait[len(TRAIT_PREFIX):]}"
            observed_value = observed.get(state_key)
            if observed_value is None:
                continue
            # Evidence only counts when the observation actually leans away
            # from the middle; a neutral message says nothing about a trait.
            if abs(observed_value - 0.5) < 0.15:
                continue

            evidence = current[evidence_key] + 1.0
            targets[evidence_key] = evidence
            if evidence < self._policy.trait_evidence_required:
                continue
            direction = 1.0 if observed_value > 0.5 else -1.0
            targets[trait] = current[trait] + self._policy.trait_gain_per_evidence * direction

        # --- provisional until there is enough to go on --------------------
        targets[PROVISIONAL] = (
            0.0 if observations >= self._policy.provisional_until_observations else 1.0
        )

        # --- was the model wrong? -------------------------------------------
        surprise = 0.0 if appraisal is None else appraisal.expectation_violation
        if surprise > self._policy.model_error_tolerance:
            targets[MODEL_ERRORS] = current[MODEL_ERRORS] + 1.0

        proposals: list[StateChangeProposal] = []
        changes: dict[str, float] = {}
        for key, raw_target in targets.items():
            previous = current[key]
            new_value = self._bounded(key, previous, raw_target)
            if abs(new_value - previous) < 1e-6:
                continue
            proposals.append(
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module=MODULE,
                    target_domain=self._domain_of(key),
                    target_key=key,
                    value=round(new_value, 6),
                    confidence=None if appraisal is None else appraisal.confidence,
                    reason_codes=("state_estimate",)
                    if key.startswith(STATE_PREFIX)
                    else ("trait_estimate",)
                    if key.startswith(TRAIT_PREFIX)
                    else ("bookkeeping",),
                    evidence_ids=(event.event_id,),
                    clock=self._clock,
                )
            )
            changes[key] = round(new_value, 6)

        if not proposals:
            return SubscriberResult()

        updated = event.child(
            event_type=USER_MODEL_UPDATED,
            category="internal",
            actor_type="yui",
            source_type=MODULE,
            clock=self._clock,
            priority="P3",
            payload=UserModelUpdatedPayload(
                changes=changes,
                observation_count=int(observations),
                provisional=targets[PROVISIONAL] >= 1.0,
                model_error=surprise > self._policy.model_error_tolerance,
            ),
        )
        return SubscriberResult(proposals=tuple(proposals), events=(updated,))

    # --- observation -------------------------------------------------------
    def _observe(self, text: str, signals) -> dict[str, float]:
        """What this one message suggests about the USER *right now*."""
        stripped = text.strip()
        if not stripped:
            return {}

        observed: dict[str, float] = {}
        length = len(stripped)
        # A long message suggests energy and expressiveness in this moment —
        # nothing more than this moment.
        observed[STATE_ENERGY] = max(0.0, min(1.0, 0.3 + min(1.0, length / 200.0) * 0.5))
        if signals.positive_affect or signals.negative_affect:
            observed[STATE_WARMTH] = max(
                0.0, min(1.0, 0.5 + 0.5 * (signals.positive_affect - signals.negative_affect))
            )
        if signals.negative_affect > 0.4 or signals.has_conflict:
            observed[STATE_DISTRESS] = max(0.0, min(1.0, 0.3 + signals.negative_affect * 0.6))
        return observed

    # --- helpers -----------------------------------------------------------
    def _decayed(self, current: float, baseline: float, idle_hours: float) -> float:
        if idle_hours <= 0:
            return current
        pull = min(1.0, self._policy.state_estimate_decay_per_hour * idle_hours)
        return current + (baseline - current) * pull

    def _bounded(self, key: str, previous: float, target: float) -> float:
        if _is_counter(key):
            return max(0.0, target)  # counters, not bounded psychological values
        if key == PROVISIONAL:
            return target
        limit = (
            self._policy.trait_max_per_event
            if key.startswith(TRAIT_PREFIX)
            else self._policy.max_per_event
        )
        delta = max(-limit, min(limit, target - previous))
        return max(0.0, min(1.0, previous + delta))

    @staticmethod
    def _domain_of(key: str) -> str:
        return COUNTER_DOMAIN if _is_counter(key) else DOMAIN

    def _current(self, view: RunView, key: str) -> float:
        value = view.number(self._domain_of(key), key)
        return DEFAULTS[key] if value is None else value

    def _idle_hours(self, view: RunView, now: datetime) -> float:
        entry = view.snapshot.get(DOMAIN, STATE_ENERGY)
        if entry is None:
            return 0.0
        return max(0.0, (now - entry.updated_at).total_seconds() / 3600.0)


def _is_counter(key: str) -> bool:
    return key in (OBSERVATION_COUNT, MODEL_ERRORS) or key.startswith("evidence_")
