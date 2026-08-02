"""Adaptation Engine — single writer of ``characteristic_adaptations``.

Spec 12.2: characteristic adaptations sit between traits and expression and may
change faster than traits. Spec 23.2 adds the rules that shape this engine:

* **Domain-specific adaptation moves before the general trait.** Nothing here
  ever touches ``personality``; it produces the evidence that a trait update is
  later built from.
* **Baseline, adaptation and expression stay separate.** The adaptation keeps
  its own baseline, so "moved a long way from where she started" is a question
  that can still be answered years later.
* **Extreme states resist more of the same.** Additional same-direction
  evidence has progressively less effect as a value approaches its edge;
  evidence in the opposite direction is not damped, so YUI can always come back.

Evidence arrives from consolidation, not from single events: the engine reads
what has already been committed, together with its provenance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.clock import Clock, SystemClock
from app.consolidation.dynamics import damped_step
from app.consolidation.models import CharacteristicAdaptation
from app.consolidation.policy import AdaptationRules
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.state.proposal import StateChangeProposal
from app.storage.repositories.growth import AdaptationRepository

logger = logging.getLogger(__name__)

MODULE = "adaptation_engine"
DOMAIN = "characteristic_adaptations"

#: How much provenance travels with an adaptation. Enough to justify a
#: proposal, not so much that the row grows without bound.
MAX_TRACKED_EVIDENCE = 20


@dataclass(frozen=True, slots=True)
class AdaptationEvidence:
    """One observation that says something about an adaptation."""

    adaptation: str
    direction: int
    weight: float
    context: str
    evidence_id: str
    observed_at: datetime
    #: True when mood was near baseline, so mood alone does not explain it.
    mood_independent: bool = False
    outcome_weight: float = 0.0


@dataclass(frozen=True, slots=True)
class AdaptationUpdate:
    adaptation: CharacteristicAdaptation
    previous_value: float
    moved: bool

    @property
    def delta(self) -> float:
        return round(self.adaptation.value - self.previous_value, 6)


class AdaptationEngine:
    name = MODULE

    def __init__(
        self,
        repository: AdaptationRepository,
        policy: AdaptationRules,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()

    # --- evidence ----------------------------------------------------------
    def evidence_from_change(
        self,
        *,
        domain: str,
        key: str,
        delta: float,
        source_module: str,
        change_id: str,
        committed_at: datetime,
        mood_independent: bool,
    ) -> AdaptationEvidence | None:
        """Turn one committed state change into adaptation evidence, if any.

        A change too small to mean anything is noise, and a domain nobody has
        declared as evidence about an adaptation stays out of growth entirely.
        """
        if abs(delta) < self._policy.min_source_delta:
            return None
        mapping = self._policy.source_for(domain, key)
        if mapping is None:
            return None
        adaptation, weight = mapping
        if weight == 0.0:
            return None
        # A negative mapping inverts the reading: needing more reassurance is
        # evidence of *less* independence, not more.
        direction = 1 if delta * weight > 0 else -1
        return AdaptationEvidence(
            adaptation=adaptation,
            direction=direction,
            weight=abs(weight),
            # The context is *where* the evidence came from. Cross-context
            # evidence means several different parts of life agree (spec 12.3).
            context=f"{domain}:{source_module}",
            evidence_id=change_id,
            observed_at=committed_at,
            mood_independent=mood_independent,
            outcome_weight=min(1.0, abs(delta) * 10.0),
        )

    def observe(self, evidence: AdaptationEvidence) -> AdaptationUpdate:
        """Accumulate one piece of evidence; move only when enough has built up.

        This writes the engine's own ledger. What YUI *expresses* is the
        committed ``characteristic_adaptations`` state, which only ``handle``
        proposes and only the committer writes (spec 9.3).
        """
        now = self._clock.now()
        current = self._repository.ensure(
            evidence.adaptation, initial=self._policy.initial_value, now=now
        )
        previous_value = current.value

        pending = current.pending_evidence + evidence.direction * evidence.weight
        supporting = current.supporting_count + (1 if evidence.direction > 0 else 0)
        contradicting = current.contradicting_count + (1 if evidence.direction < 0 else 0)
        contexts = tuple(dict.fromkeys((*current.contexts, evidence.context)))

        value = current.value
        if abs(pending) >= self._policy.evidence_before_change:
            step_direction = 1.0 if pending > 0 else -1.0
            step = damped_step(
                value=current.value,
                step=self._policy.step * step_direction,
                damping=self._policy.extremity_damping,
                limit=self._policy.max_change_per_run,
            )
            value = max(0.0, min(1.0, current.value + step))
            pending -= self._policy.evidence_before_change * step_direction

        evidence_ids = tuple(
            dict.fromkeys((*current.evidence_ids, evidence.evidence_id))
        )[-MAX_TRACKED_EVIDENCE:]

        updated = self._repository.update(
            name=evidence.adaptation,
            value=round(value, 6),
            pending_evidence=round(pending, 6),
            supporting_count=supporting,
            contradicting_count=contradicting,
            contexts=contexts,
            evidence_ids=evidence_ids,
            now=now,
            first_evidence_at=evidence.observed_at,
        )
        moved = abs(updated.value - previous_value) > 1e-9
        if moved:
            logger.info(
                "adaptation moved name=%s %.3f -> %.3f",
                evidence.adaptation,
                previous_value,
                updated.value,
            )
        return AdaptationUpdate(updated, previous_value, moved)

    # --- as a subscriber ---------------------------------------------------
    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        """Mirror the ledger into committed state during consolidation.

        Proposing the current ledger value (rather than a remembered delta)
        makes this self-healing: if a previous commit was rejected, the next
        consolidation proposes the same value again instead of drifting apart.
        """
        proposals = []
        for adaptation in self._repository.all():
            committed = view.number(DOMAIN, adaptation.name)
            if committed is not None and abs(committed - adaptation.value) < 1e-9:
                continue
            if not adaptation.evidence_ids:
                continue
            proposals.append(
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module=MODULE,
                    target_domain=DOMAIN,
                    target_key=adaptation.name,
                    value=adaptation.value,
                    reason_codes=("consolidated_adaptation", *adaptation.contexts[:3]),
                    evidence_ids=adaptation.evidence_ids,
                    clock=self._clock,
                )
            )
        return SubscriberResult(proposals=tuple(proposals))

    # --- reads -------------------------------------------------------------
    def get(self, name: str) -> CharacteristicAdaptation | None:
        return self._repository.by_name(name)

    def all(self) -> list[CharacteristicAdaptation]:
        return self._repository.all()
