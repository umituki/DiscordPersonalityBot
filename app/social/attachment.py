"""Attachment Engine — single writer of ``attachment`` (spec 9.3, 13.2).

Spec 13.2 separates three things, and so does this engine:

* general **disposition** — Layer 4, deep, changed only by consolidation. This
  engine never touches it.
* **relationship-specific** expectancies — how safe this particular bond feels.
* **current activation** — how switched-on the attachment system is right now.

Two rules matter most: security buys *tolerance* for separation, and growing
closer is not the same as growing more dependent (spec 13.2). More felt
security therefore lowers, not raises, proximity desire and reassurance need.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.psychology.models import Appraisal
from app.social.events import ATTACHMENT_UPDATED, AttachmentUpdatedPayload
from app.social.policy import AttachmentPolicy
from app.social.signals import signals_from
from app.state.proposal import StateChangeProposal

logger = logging.getLogger(__name__)

DOMAIN = "attachment"
MODULE = "attachment_engine"

ACTIVATION = "current_activation"
FELT_SECURITY = "felt_security"
PROXIMITY_DESIRE = "proximity_desire"
REASSURANCE_NEED = "reassurance_need"
WITHDRAWAL_TENDENCY = "withdrawal_tendency"
SEPARATION_SECURITY = "separation_security"

DEFAULTS: dict[str, float] = {
    ACTIVATION: 0.2,
    FELT_SECURITY: 0.5,
    PROXIMITY_DESIRE: 0.3,
    REASSURANCE_NEED: 0.3,
    WITHDRAWAL_TENDENCY: 0.2,
    SEPARATION_SECURITY: 0.5,
}


class AttachmentEngine:
    name = MODULE

    def __init__(self, policy: AttachmentPolicy, *, clock: Clock | None = None) -> None:
        self._policy = policy
        self._clock = clock or SystemClock()

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        if event.actor_type not in ("user", "yui"):
            return SubscriberResult()

        appraisal = view.appraisal if isinstance(view.appraisal, Appraisal) else None
        now = self._clock.now()
        separation_days = self._separation_days(view, now)
        signals = signals_from(event, appraisal, separation_days=separation_days)

        current = {key: self._current(view, key) for key in DEFAULTS}
        # Relationship security is read from S0 — a cross-domain read, never a
        # write (spec 9.3).
        relationship_security = view.number("relationship", "security", 0.5) or 0.5

        policy = self._policy
        targets: dict[str, float] = {}

        # --- activation -----------------------------------------------------
        activation = current[ACTIVATION]
        decay_hours = separation_days * 24.0
        if decay_hours > 0:
            activation = max(0.0, activation - policy.activation_decay_per_hour * decay_hours)
        if signals.has_conflict:
            activation += policy.activation_from_conflict
        if separation_days > 0:
            # Security buys tolerance: the same absence activates a secure bond
            # far less (spec 13.2).
            tolerance = 1.0 - policy.separation_tolerance_from_security * current[FELT_SECURITY]
            activation += policy.activation_from_separation_per_day * separation_days * tolerance
        if abs(activation - current[ACTIVATION]) > 1e-9:
            targets[ACTIVATION] = activation

        # --- felt security --------------------------------------------------
        felt = current[FELT_SECURITY]
        if signals.has_conflict:
            felt -= policy.felt_security_loss_per_conflict
        elif signals.is_contact and signals.positive_affect > 0:
            felt += policy.felt_security_gain_per_positive * (1.0 + signals.positive_affect)
        felt = 0.7 * felt + 0.3 * (
            policy.baseline_felt_security + 0.5 * (relationship_security - 0.5)
        )
        if abs(felt - current[FELT_SECURITY]) > 1e-9:
            targets[FELT_SECURITY] = felt

        # --- what activation and insecurity produce -------------------------
        resolved_activation = targets.get(ACTIVATION, current[ACTIVATION])
        resolved_security = targets.get(FELT_SECURITY, current[FELT_SECURITY])
        insecurity = 1.0 - resolved_security

        targets[PROXIMITY_DESIRE] = (
            policy.proximity_desire_from_activation * resolved_activation * (0.5 + insecurity)
        )
        targets[REASSURANCE_NEED] = policy.reassurance_need_from_insecurity * insecurity * (
            0.5 + resolved_activation
        )
        if signals.has_conflict or view.number("relationship", "conflict_residue", 0.0):
            residue = view.number("relationship", "conflict_residue", 0.0) or 0.0
            targets[WITHDRAWAL_TENDENCY] = policy.withdrawal_from_unresolved * residue
        targets[SEPARATION_SECURITY] = (
            policy.separation_tolerance_from_security * resolved_security
        )

        proposals: list[StateChangeProposal] = []
        changes: dict[str, float] = {}
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
                    reason_codes=(key, signals.conflict_kind)
                    if signals.has_conflict
                    else (key,),
                    evidence_ids=(event.event_id,),
                    clock=self._clock,
                )
            )
            changes[key] = round(new_value, 6)

        if not proposals:
            return SubscriberResult()

        updated = event.child(
            event_type=ATTACHMENT_UPDATED,
            category="internal",
            actor_type="yui",
            source_type=MODULE,
            clock=self._clock,
            priority="P3",
            payload=AttachmentUpdatedPayload(
                changes=changes,
                reason_code=signals.conflict_kind if signals.has_conflict else "contact",
                separation_days=round(separation_days, 3),
            ),
        )
        return SubscriberResult(proposals=tuple(proposals), events=(updated,))

    # --- helpers -----------------------------------------------------------
    def _bounded(self, previous: float, target: float) -> float:
        limit = self._policy.max_per_event
        delta = max(-limit, min(limit, target - previous))
        return previous + delta

    def _current(self, view: RunView, key: str) -> float:
        value = view.number(DOMAIN, key)
        return DEFAULTS[key] if value is None else value

    def _separation_days(self, view: RunView, now: datetime) -> float:
        entry = view.snapshot.get(DOMAIN, ACTIVATION)
        if entry is None:
            return 0.0
        return max(0.0, (now - entry.updated_at).total_seconds() / 86400.0)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
