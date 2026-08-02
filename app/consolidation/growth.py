"""Growth Engine — single writer of Layer 4 personality state.

Owns ``personality``, ``narrative_identity`` and ``attachment_disposition``
(spec 9.3, 12.1, 12.4, 13.2). The rules it exists to keep:

* **A single event never moves a trait** (spec 2.13, 12.3). The engine reads
  *candidates*, never events, and a candidate only exists after repeated,
  persistent, cross-context, meaningful, mood-independent evidence.
* **Baseline, adaptation and expression stay separate** (spec 23.2). The
  expressed value is committed state; the baseline is this engine's own slow
  ledger; the middle layer belongs to the adaptation engine.
* **The initial baseline is not permanent** (spec 23.2). When a deep update
  promotes, the baseline follows the expressed value by a fraction of the
  distance — so a life lived differently eventually changes where "normal" is.
* **Nothing is applied until it is committed.** ``handle`` only proposes;
  ledger and history are written afterwards, from what the transaction
  actually accepted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from app.clock import Clock, SystemClock
from app.consolidation.deep_gate import DeepUpdateGate, GateDecision
from app.consolidation.dynamics import damped_step
from app.consolidation.models import DeepUpdateCandidate, NarrativeTheme, PersonalityTrait
from app.consolidation.policy import GrowthPolicy
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.state.proposal import StateChangeProposal
from app.storage.repositories.growth import (
    CandidateRepository,
    NarrativeRepository,
    PersonalityRepository,
)

logger = logging.getLogger(__name__)

MODULE = "growth_engine"

PERSONALITY = "personality"
NARRATIVE = "narrative_identity"
DISPOSITION = "attachment_disposition"

OWNED_DOMAINS: frozenset[str] = frozenset({PERSONALITY, NARRATIVE, DISPOSITION})


@dataclass(frozen=True, slots=True)
class DeepUpdate:
    """One promoted candidate, after the commit confirmed it."""

    candidate: DeepUpdateCandidate
    previous_value: float | None
    new_value: float
    baseline_after: float | None
    decision: GateDecision


class GrowthEngine:
    name = MODULE

    def __init__(
        self,
        *,
        candidates: CandidateRepository,
        traits: PersonalityRepository,
        narratives: NarrativeRepository,
        policy: GrowthPolicy,
        clock: Clock | None = None,
    ) -> None:
        self._candidates = candidates
        self._traits = traits
        self._narratives = narratives
        self._policy = policy
        self._gate = DeepUpdateGate(policy.deep_gate)
        self._clock = clock or SystemClock()

    @property
    def gate(self) -> DeepUpdateGate:
        return self._gate

    # --- seeding (ledger only, never state) --------------------------------
    def ensure_seeded(
        self, temperament: Mapping[str, float] | None = None
    ) -> list[PersonalityTrait]:
        """Give the traits their starting baselines (spec 12.1, 22.1).

        This writes the engine's own ledger, not the ``personality`` state
        domain: what YUI *expresses* still has to be proposed and committed.

        ``temperament`` overrides the policy defaults, which is how a
        temperamental seed enters the pipeline — as a *starting bias*, never as
        a finished personality (spec 22.2). Seeding is idempotent, so a life
        already under way is not reset by a later call.
        """
        starting = {**self._policy.personality.temperament, **(temperament or {})}
        return self._traits.seed(starting, now=self._clock.now())

    # --- as a subscriber ---------------------------------------------------
    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        proposals = [
            proposal
            for candidate in self._candidates.accumulating()
            if candidate.target_domain in OWNED_DOMAINS
            for proposal in self._proposals_for(candidate, view, event.event_id)
        ]
        proposals.extend(self._narrative_proposals(view, event.event_id))
        return SubscriberResult(proposals=tuple(proposals))

    def _proposals_for(
        self, candidate: DeepUpdateCandidate, view: RunView, event_id: str
    ) -> list[StateChangeProposal]:
        decision = self._gate.evaluate(candidate)
        if not decision.passed:
            return []
        current = self._current_value(candidate, view)
        new_value = self._next_value(candidate, current)
        if abs(new_value - current) < 1e-9:
            return []
        return [
            StateChangeProposal.set_value(
                source_event_id=event_id,
                source_module=MODULE,
                target_domain=candidate.target_domain,
                target_key=candidate.target_key,
                value=new_value,
                reason_codes=("deep_update", *decision.satisfied),
                evidence_ids=candidate.evidence_ids,
                confidence=min(1.0, candidate.pattern_count / 10.0),
                clock=self._clock,
            )
        ]

    def _narrative_proposals(self, view: RunView, event_id: str) -> list[StateChangeProposal]:
        """A theme becomes part of the story once it keeps being retold."""
        rules = self._policy.narrative
        proposals: list[StateChangeProposal] = []
        for theme in self._narratives.all(limit=rules.max_themes):
            if theme.supporting_memory_count < rules.min_supporting_memories:
                continue
            current = view.number(NARRATIVE, theme.theme, 0.0) or 0.0
            target = min(rules.max_strength, theme.strength)
            if target - current < 1e-9:
                continue
            proposals.append(
                StateChangeProposal.set_value(
                    source_event_id=event_id,
                    source_module=MODULE,
                    target_domain=NARRATIVE,
                    target_key=theme.theme,
                    value=round(min(current + rules.strength_step, target), 6),
                    reason_codes=("narrative_theme", theme.status),
                    evidence_ids=theme.supporting_memory_ids,
                    clock=self._clock,
                )
            )
        return proposals

    # --- after the commit ---------------------------------------------------
    def apply_committed(
        self,
        *,
        committed: Mapping[str, float],
        run_id: str | None,
        now: datetime | None = None,
    ) -> list[DeepUpdate]:
        """Promote the candidates the transaction actually accepted.

        ``committed`` maps ``domain.key`` to the value the commit really wrote,
        so the ledger can never record a change the transaction rejected.
        """
        moment = now or self._clock.now()
        applied: list[DeepUpdate] = []
        for candidate in self._candidates.accumulating():
            if candidate.target_domain not in OWNED_DOMAINS:
                continue
            if candidate.target not in committed:
                continue
            decision = self._gate.evaluate(candidate)
            if not decision.passed:
                continue
            applied.append(
                self._promote(
                    candidate,
                    decision,
                    committed_value=committed[candidate.target],
                    run_id=run_id,
                    now=moment,
                )
            )
        return applied

    def _promote(
        self,
        candidate: DeepUpdateCandidate,
        decision: GateDecision,
        *,
        committed_value: float,
        run_id: str | None,
        now: datetime,
    ) -> DeepUpdate:
        baseline_after: float | None = None
        previous: float | None = None
        new_value = committed_value

        if candidate.target_domain == PERSONALITY:
            trait = self._traits.by_name(candidate.target_key)
            if trait is not None:
                previous = trait.baseline
                # The expressed value has already been committed; the baseline
                # follows it, slowly and only now (spec 23.2).
                baseline_after = round(
                    trait.baseline
                    + self._policy.personality.baseline_follow_rate
                    * (committed_value - trait.baseline),
                    6,
                )
                self._traits.set_baseline(trait.name, baseline=baseline_after, now=now)
                self._traits.record_change(
                    trait=trait.name,
                    previous_value=previous,
                    new_value=new_value,
                    baseline_after=baseline_after,
                    reason_code="deep_update",
                    candidate_id=candidate.candidate_id,
                    run_id=run_id,
                    now=now,
                )

        self._candidates.resolve(candidate.candidate_id, "promoted", now=now)
        logger.info(
            "deep update promoted target=%s direction=%+d patterns=%d contexts=%d",
            candidate.target,
            candidate.direction,
            candidate.pattern_count,
            len(candidate.contexts),
        )
        return DeepUpdate(candidate, previous, new_value, baseline_after, decision)

    # --- helpers ------------------------------------------------------------
    def _current_value(self, candidate: DeepUpdateCandidate, view: RunView) -> float:
        if candidate.target_domain == PERSONALITY:
            trait = self._traits.by_name(candidate.target_key)
            fallback = trait.baseline if trait is not None else 0.5
        else:
            fallback = 0.5
        return view.number(candidate.target_domain, candidate.target_key, fallback) or fallback

    def _next_value(self, candidate: DeepUpdateCandidate, current: float) -> float:
        if candidate.target_domain == PERSONALITY:
            rules = self._policy.personality
        else:
            rules = self._policy.disposition
        delta = damped_step(
            value=current,
            step=candidate.direction * rules.max_step,
            damping=rules.extremity_damping,
            limit=rules.max_step,
        )
        return round(max(0.0, min(1.0, current + delta)), 6)

    # --- reads --------------------------------------------------------------
    def traits(self) -> list[PersonalityTrait]:
        return self._traits.all()

    def themes(self, *, limit: int = 20) -> list[NarrativeTheme]:
        return self._narratives.all(limit=limit)

    def pending_candidates(self) -> list[tuple[DeepUpdateCandidate, GateDecision]]:
        return [
            (candidate, self._gate.evaluate(candidate))
            for candidate in self._candidates.accumulating()
        ]
