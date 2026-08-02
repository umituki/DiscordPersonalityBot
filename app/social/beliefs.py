"""Belief Engine — single writer of beliefs (spec 9.3, 12, 25).

A belief is never *set*. It is derived from evidence, and evidence is kept in
both directions (spec 25). Three rules do the work:

* **Both stances are stored.** Contradicting evidence does not delete a belief;
  it lowers its confidence and can mark it contested.
* **One primary source counts once.** The same claim repeated in five places is
  one piece of evidence, not five (spec 25).
* **Abandoning is a status, not a delete.** A belief that stops being held stays
  on disk with its history, because later evidence may revive it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.clock import Clock, SystemClock
from app.events.model import EventOrigin
from app.social.belief_policy import BeliefRules
from app.social.models import Belief, Stance
from app.storage.repositories.social import BeliefRepository

logger = logging.getLogger(__name__)

MODULE = "belief_engine"


@dataclass(frozen=True, slots=True)
class BeliefUpdate:
    belief: Belief
    evidence_counted: bool
    previous_confidence: float | None
    reason: str

    @property
    def changed(self) -> bool:
        return self.previous_confidence is None or abs(
            self.belief.confidence - self.previous_confidence
        ) > 1e-9


class BeliefEngine:
    name = MODULE

    def __init__(
        self,
        repository: BeliefRepository,
        policy: BeliefRules,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()

    def record_evidence(
        self,
        *,
        statement: str,
        subject: str,
        stance: Stance,
        source_type: str,
        primary_source_id: str,
        origin: EventOrigin = "real_discord",
        event_id: str | None = None,
        weight: float | None = None,
    ) -> BeliefUpdate:
        """Add one piece of evidence and recompute the belief's confidence."""
        now = self._clock.now()
        existing = self._repository.find(statement, subject, origin)
        previous_confidence = None if existing is None else existing.confidence

        belief = existing or self._repository.upsert(
            statement=statement,
            subject=subject,
            origin=origin,
            confidence=self._policy.initial_confidence,
            support_weight=0.0,
            contradiction_weight=0.0,
            status="held",
            now=now,
        )

        counted = self._repository.add_evidence(
            belief_id=belief.belief_id,
            stance=stance,
            weight=self._policy.weight_for(source_type) if weight is None else weight,
            source_type=source_type,
            primary_source_id=primary_source_id,
            event_id=event_id,
            now=now,
        )
        if not counted:
            logger.debug(
                "evidence already counted belief=%s source=%s", belief.belief_id, primary_source_id
            )
            return BeliefUpdate(belief, False, previous_confidence, "duplicate_primary_source")

        support, contradiction = self._repository.weights(belief.belief_id)
        confidence = self._confidence(support, contradiction)
        status = self._status(support, contradiction, confidence)

        updated = self._repository.upsert(
            statement=statement,
            subject=subject,
            origin=origin,
            confidence=confidence,
            support_weight=support,
            contradiction_weight=contradiction,
            status=status,
            now=now,
        )
        return BeliefUpdate(updated, True, previous_confidence, status)

    # --- derivation --------------------------------------------------------
    def _confidence(self, support: float, contradiction: float) -> float:
        """Move the prior towards the evidence balance, by how much there is.

        Two quantities matter and they are different: which way the evidence
        leans (``balance``), and how much of it there is (``mass``). A single
        supporting message leans all the way but weighs little, so it moves the
        prior only part of the way; independent sources accumulate mass and
        keep moving it (spec 24, 25).
        """
        weighted_contradiction = contradiction * self._policy.contradiction_bias
        mass = support + weighted_contradiction
        if mass <= 0:
            return self._policy.initial_confidence

        balance = support / mass
        pull = mass / (mass + self._policy.evidence_half_weight)
        prior = self._policy.initial_confidence
        confidence = prior + (balance - prior) * pull
        return round(min(self._policy.max_confidence, max(0.0, confidence)), 6)

    def _status(self, support: float, contradiction: float, confidence: float) -> str:
        if confidence < self._policy.abandon_confidence:
            return "abandoned"
        if support > 0 and contradiction > 0:
            weaker, stronger = sorted((support, contradiction))
            if stronger > 0 and weaker / stronger >= self._policy.contested_ratio:
                return "contested"
        return "held"

    # --- reads -------------------------------------------------------------
    def held_beliefs(self, *, subject: str | None = None, limit: int = 20) -> list[Belief]:
        return self._repository.held(subject=subject, limit=limit)

    def belief_about(self, statement: str, subject: str, origin: EventOrigin) -> Belief | None:
        return self._repository.find(statement, subject, origin)

    def evidence_for(self, belief_id: str):
        return self._repository.evidence_for(belief_id)

    def suppress(self, belief_id: str) -> None:
        """Admin suppression (spec 30). Never a delete."""
        self._repository.set_status(belief_id, "suppressed", now=self._clock.now())
