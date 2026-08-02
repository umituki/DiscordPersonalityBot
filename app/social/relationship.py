"""Relationship Engine — single writer of ``relationship`` (spec 9.3, 13.1, 13.3).

Two specification rules shape everything here:

* **Contact volume is not closeness.** Only ``familiarity`` rises simply because
  something was said. Trust, closeness, security and respect each need their own
  kind of evidence (spec 13.1, 34.2-6).
* **An apology is not a repair.** Apology relieves conflict residue and helps
  emotional recovery, but restores no trust by itself. Trust comes back only
  through repeated consistent behaviour afterwards (spec 13.3, 34.2-7).

Everything is proposed, never written, and every dimension has its own
per-event ceiling so no single moment can rewrite a relationship.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.psychology.models import Appraisal
from app.social.events import RELATIONSHIP_UPDATED, RelationshipUpdatedPayload
from app.social.policy import RelationshipPolicy
from app.social.signals import SocialSignals, signals_from
from app.state.proposal import StateChangeProposal

logger = logging.getLogger(__name__)

DOMAIN = "relationship"
MODULE = "relationship_engine"

#: Spec 22.7: ``FIRST BOOT 以前に USER との関係経験を生成しない``. This bond is
#: with the USER, and a simulated past contains no USER — so a simulated event
#: must never touch it. What a simulated life *does* shape is the general
#: attachment disposition, which consolidation reaches at Layer 4 (spec 13.2).
NON_RELATIONAL_ORIGINS: frozenset[str] = frozenset({"simulated_past"})


TRUST = "trust"
FAMILIARITY = "familiarity"
CLOSENESS = "emotional_closeness"
SECURITY = "security"
RESPECT = "respect"
CONFLICT_RESIDUE = "conflict_residue"
EXPECTATION = "expectation"
#: Spec 13.3: what a severe breach leaves behind. Forgiveness, emotional
#: recovery and trust restoration are separate from this, and an apology does
#: not touch it.
UNRESOLVED_HURT = "unresolved_hurt"
#: Fraction of the consistent behaviour needed to start restoring trust.
#: Stored as 0..1 so it lives in the same bounded space as everything else.
REPAIR_PROGRESS = "repair_progress"

DEFAULTS: dict[str, float] = {
    TRUST: 0.5,
    FAMILIARITY: 0.1,
    CLOSENESS: 0.3,
    SECURITY: 0.5,
    RESPECT: 0.5,
    CONFLICT_RESIDUE: 0.0,
    UNRESOLVED_HURT: 0.0,
    EXPECTATION: 0.5,
    REPAIR_PROGRESS: 0.0,
}


class RelationshipEngine:
    name = MODULE

    def __init__(self, policy: RelationshipPolicy, *, clock: Clock | None = None) -> None:
        self._policy = policy
        self._clock = clock or SystemClock()

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        if event.actor_type not in ("user", "yui"):
            return SubscriberResult()
        if event.origin in NON_RELATIONAL_ORIGINS:
            return SubscriberResult()

        appraisal = view.appraisal if isinstance(view.appraisal, Appraisal) else None
        # Patch spec 13: the run's time, not the machine's. In a simulation
        # these are decades apart.
        now = view.now(self._clock)
        separation_days = self._separation_days(view, now)
        signals = signals_from(event, appraisal, separation_days=separation_days)

        current = {key: self._current(view, key) for key in DEFAULTS}
        targets = self._targets(current, signals, separation_days)

        proposals: list[StateChangeProposal] = []
        changes: dict[str, float] = {}
        for key, raw_target in targets.items():
            previous = current[key]
            new_value = _clamp(self._bounded(key, previous, raw_target))
            if abs(new_value - previous) < 1e-6:
                continue
            proposals.append(
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module=MODULE,
                    target_domain=DOMAIN,
                    target_key=key,
                    value=round(new_value, 6),
                    confidence=None if appraisal is None else appraisal.confidence,
                    reason_codes=self._reasons(key, signals),
                    # Relationship changes must be traceable to something
                    # (spec 25); the event itself is the evidence.
                    evidence_ids=(event.event_id,),
                    clock=self._clock,
                )
            )
            changes[key] = round(new_value, 6)

        if not proposals:
            return SubscriberResult()

        updated = event.child(
            event_type=RELATIONSHIP_UPDATED,
            category="internal",
            actor_type="yui",
            source_type=MODULE,
            clock=self._clock,
            priority="P3",
            payload=RelationshipUpdatedPayload(
                changes=changes,
                conflict_kind=signals.conflict_kind,
                apology=signals.apology,
                separation_days=round(separation_days, 3),
            ),
        )
        return SubscriberResult(proposals=tuple(proposals), events=(updated,))

    # --- dimension rules ---------------------------------------------------
    def _targets(
        self, current: dict[str, float], signals: SocialSignals, separation_days: float
    ) -> dict[str, float]:
        rules = self._policy.dimensions
        repair = self._policy.repair
        targets: dict[str, float] = {}

        # --- familiarity: the one dimension contact alone may move ---------
        if signals.is_contact:
            headroom = 1.0 - current[FAMILIARITY]
            targets[FAMILIARITY] = current[FAMILIARITY] + rules.familiarity.contact_gain * (
                headroom ** rules.familiarity.ceiling_pull
            )

        # --- conflict residue ----------------------------------------------
        residue = current[CONFLICT_RESIDUE]
        if signals.has_conflict:
            residue = residue + getattr(rules.conflict_residue, signals.conflict_kind)
            # New damage restarts the repair the relationship had accumulated.
            targets[REPAIR_PROGRESS] = 0.0
            if signals.is_severe:
                targets[UNRESOLVED_HURT] = (
                    current[UNRESOLVED_HURT] + repair.unresolved_hurt_floor * 2.0
                )
        else:
            residue = residue - rules.conflict_residue.natural_decay_per_day * max(
                separation_days, 0.0
            )
        if signals.apology and current[CONFLICT_RESIDUE] > 0:
            residue -= repair.apology.conflict_residue_relief * current[CONFLICT_RESIDUE]
        # What a severe breach left behind is a floor under the residue: an
        # apology cannot take the relationship below it (spec 13.3).
        hurt = targets.get(UNRESOLVED_HURT, current[UNRESOLVED_HURT])
        if hurt > 0:
            residue = max(residue, hurt)
        if abs(residue - current[CONFLICT_RESIDUE]) > 1e-9:
            targets[CONFLICT_RESIDUE] = residue

        # --- trust: evidence only, never contact ---------------------------
        trust_target = current[TRUST]
        if signals.has_conflict:
            trust_target -= {
                "disagreement": 0.0,
                "conflict": 0.02,
                "transgression": 0.15,
                "betrayal": 0.35,
            }[signals.conflict_kind]
        elif signals.kept_commitment:
            headroom = 1.0 - current[TRUST]
            trust_target += rules.trust.evidence_gain * (headroom ** rules.trust.ceiling_pull)
        elif current[CONFLICT_RESIDUE] > 0 and signals.is_contact and not signals.apology:
            # Repair happens through what comes *after* the apology, and only
            # once enough consistent events have accumulated.
            step = 1.0 / repair.consistent_behaviour.required_events
            progress = min(1.0, current[REPAIR_PROGRESS] + step)
            targets[REPAIR_PROGRESS] = progress
            if progress >= 1.0 - 1e-9:
                trust_target += repair.consistent_behaviour.trust_restoration_per_event
        if abs(trust_target - current[TRUST]) > 1e-9:
            targets[TRUST] = trust_target

        # --- closeness ------------------------------------------------------
        closeness = current[CLOSENESS]
        if signals.disclosure:
            closeness += rules.emotional_closeness.disclosure_gain
        if signals.positive_affect > 0:
            closeness += rules.emotional_closeness.positive_affect_gain * signals.positive_affect
        if signals.is_severe:
            closeness -= rules.emotional_closeness.disclosure_gain
        if abs(closeness - current[CLOSENESS]) > 1e-9:
            targets[CLOSENESS] = closeness

        # --- security -------------------------------------------------------
        security = current[SECURITY]
        if signals.consistency > 0 and not signals.has_conflict:
            security += rules.security.consistency_gain * signals.consistency
        if current[CONFLICT_RESIDUE] > 0.2 or signals.is_severe:
            security -= rules.security.unresolved_conflict_penalty
        if abs(security - current[SECURITY]) > 1e-9:
            targets[SECURITY] = security

        # --- respect --------------------------------------------------------
        if signals.competence_shown > 0:
            targets[RESPECT] = (
                current[RESPECT] + rules.respect.competence_gain * signals.competence_shown
            )

        # --- expectation ----------------------------------------------------
        if signals.is_contact:
            direction = 1.0 if signals.positive_affect >= signals.negative_affect else -1.0
            targets[EXPECTATION] = (
                current[EXPECTATION] + rules.expectation.adjust_rate * direction * 0.5
            )

        return targets

    # --- helpers -----------------------------------------------------------
    def _bounded(self, key: str, previous: float, target: float) -> float:
        limit = self._limit_for(key)
        delta = max(-limit, min(limit, target - previous))
        return previous + delta

    def _limit_for(self, key: str) -> float:
        rules = self._policy.dimensions
        mapping = {
            TRUST: rules.trust.max_per_event,
            FAMILIARITY: rules.familiarity.max_per_event,
            CLOSENESS: rules.emotional_closeness.max_per_event,
            SECURITY: rules.security.max_per_event,
            RESPECT: rules.respect.max_per_event,
            CONFLICT_RESIDUE: rules.conflict_residue.max_per_event,
            UNRESOLVED_HURT: rules.conflict_residue.max_per_event,
            EXPECTATION: rules.expectation.max_per_event,
            REPAIR_PROGRESS: 1.0,
        }
        return mapping.get(key, 0.02)

    def _current(self, view: RunView, key: str) -> float:
        value = view.number(DOMAIN, key)
        return DEFAULTS[key] if value is None else value

    def _separation_days(self, view: RunView, now: datetime) -> float:
        entry = view.snapshot.get(DOMAIN, FAMILIARITY)
        if entry is None:
            return 0.0
        return max(0.0, (now - entry.updated_at).total_seconds() / 86400.0)

    @staticmethod
    def _reasons(key: str, signals: SocialSignals) -> tuple[str, ...]:
        reasons = [key]
        if signals.has_conflict:
            reasons.append(signals.conflict_kind)
        if signals.apology:
            reasons.append("apology")
        if signals.kept_commitment:
            reasons.append("kept_commitment")
        if signals.disclosure:
            reasons.append("disclosure")
        if signals.is_contact and not signals.has_conflict:
            reasons.append("contact")
        return tuple(reasons)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
