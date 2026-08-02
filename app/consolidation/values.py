"""Value Engine — single writer of ``values`` (spec 9.3, 12.5).

    Values は絶対 score の全上昇ではなく相対優先度として扱う。

That sentence is the whole design. Priorities are kept as a distribution that
sums to one, so raising what matters more necessarily lowers what matters less.
There is no state in which everything became more important — which is exactly
the failure mode an "increase the score" model drifts into over years.

Like personality, values are Layer 4: they move only through a candidate that
has passed the deep update gate, never from one event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from app.clock import Clock, SystemClock
from app.consolidation.deep_gate import DeepUpdateGate, GateDecision
from app.consolidation.models import DeepUpdateCandidate, ValuePriority
from app.consolidation.policy import GrowthPolicy
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.state.proposal import StateChangeProposal
from app.storage.repositories.growth import CandidateRepository, ValueRepository

logger = logging.getLogger(__name__)

MODULE = "value_engine"
DOMAIN = "values"


@dataclass(frozen=True, slots=True)
class ValueShift:
    """A promoted candidate and the redistribution it caused."""

    candidate: DeepUpdateCandidate
    raised: str
    before: dict[str, float]
    after: dict[str, float]
    decision: GateDecision

    @property
    def lowered(self) -> tuple[str, ...]:
        return tuple(
            name for name, value in self.after.items() if value < self.before.get(name, 0.0) - 1e-9
        )


def redistribute(
    priorities: Mapping[str, float], *, raise_name: str, step: float
) -> dict[str, float]:
    """Move ``step`` of priority onto ``raise_name``, taken from the others.

    The total is preserved exactly, so this is a reordering of what matters,
    not an inflation of it. A negative ``step`` lowers ``raise_name`` and gives
    the room back to everyone else.
    """
    if raise_name not in priorities:
        return dict(priorities)
    others = [name for name in priorities if name != raise_name]
    if not others:
        return dict(priorities)

    total = sum(priorities.values())
    donor_pool = sum(priorities[name] for name in others)
    if step > 0:
        # Cannot take more than the others actually have.
        step = min(step, donor_pool)
    else:
        # Cannot push a value below zero.
        step = -min(-step, priorities[raise_name])

    updated = dict(priorities)
    updated[raise_name] = priorities[raise_name] + step
    if donor_pool <= 0:
        share = step / len(others)
        for name in others:
            updated[name] = max(0.0, priorities[name] - share)
    else:
        for name in others:
            updated[name] = max(0.0, priorities[name] - step * (priorities[name] / donor_pool))

    # Renormalise against drift from the clamping above (spec 12.5: relative).
    new_total = sum(updated.values())
    if new_total > 0 and abs(new_total - total) > 1e-12:
        scale = total / new_total
        updated = {name: value * scale for name, value in updated.items()}

    rounded = {name: round(value, 6) for name, value in updated.items()}
    # Rounding must not leak priority. Without this, a few thousand
    # consolidations would quietly inflate or deflate the whole distribution,
    # which is precisely the absolute-score drift spec 12.5 rules out.
    residual = total - sum(rounded.values())
    if abs(residual) > 0:
        largest = max(rounded, key=lambda name: rounded[name])
        rounded[largest] += residual
    return rounded


class ValueEngine:
    name = MODULE

    def __init__(
        self,
        *,
        candidates: CandidateRepository,
        values: ValueRepository,
        policy: GrowthPolicy,
        clock: Clock | None = None,
    ) -> None:
        self._candidates = candidates
        self._values = values
        self._policy = policy
        self._gate = DeepUpdateGate(policy.deep_gate)
        self._clock = clock or SystemClock()

    # --- seeding (ledger only) ---------------------------------------------
    def ensure_seeded(self) -> list[ValuePriority]:
        return self._values.seed(self._policy.values.priorities, now=self._clock.now())

    # --- as a subscriber ---------------------------------------------------
    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        proposals: list[StateChangeProposal] = []
        for candidate in self._candidates.accumulating():
            if candidate.target_domain != DOMAIN:
                continue
            decision = self._gate.evaluate(candidate)
            if not decision.passed:
                continue
            proposals.extend(self._proposals_for(candidate, decision, view, event.event_id))
        return SubscriberResult(proposals=tuple(proposals))

    def _proposals_for(
        self,
        candidate: DeepUpdateCandidate,
        decision: GateDecision,
        view: RunView,
        event_id: str,
    ) -> list[StateChangeProposal]:
        current = self.current_priorities(view)
        updated = redistribute(
            current,
            raise_name=candidate.target_key,
            step=candidate.direction * self._policy.values.max_step,
        )
        proposals = []
        for name, value in sorted(updated.items()):
            if abs(value - current.get(name, 0.0)) < 1e-9:
                continue
            proposals.append(
                StateChangeProposal.set_value(
                    source_event_id=event_id,
                    source_module=MODULE,
                    target_domain=DOMAIN,
                    target_key=name,
                    value=value,
                    reason_codes=(
                        "value_reprioritised",
                        f"raised:{candidate.target_key}",
                        *decision.satisfied,
                    ),
                    evidence_ids=candidate.evidence_ids,
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
    ) -> list[ValueShift]:
        moment = now or self._clock.now()
        shifts: list[ValueShift] = []
        for candidate in self._candidates.accumulating():
            if candidate.target_domain != DOMAIN:
                continue
            if candidate.target not in committed:
                continue
            decision = self._gate.evaluate(candidate)
            if not decision.passed:
                continue
            before = {value.name: value.priority for value in self._values.all()}
            after = dict(before)
            for target, new_value in committed.items():
                domain, _, key = target.partition(".")
                if domain != DOMAIN or key not in after:
                    continue
                after[key] = new_value
                self._values.set_priority(key, priority=new_value, now=moment)
                self._values.record_change(
                    value_name=key,
                    previous_priority=before.get(key),
                    new_priority=new_value,
                    reason_code="value_reprioritised",
                    candidate_id=candidate.candidate_id,
                    run_id=run_id,
                    now=moment,
                )
            self._candidates.resolve(candidate.candidate_id, "promoted", now=moment)
            logger.info(
                "value priority shifted raised=%s direction=%+d",
                candidate.target_key,
                candidate.direction,
            )
            shifts.append(ValueShift(candidate, candidate.target_key, before, after, decision))
        return shifts

    # --- reads --------------------------------------------------------------
    def current_priorities(self, view: RunView | None = None) -> dict[str, float]:
        """Committed priorities where they exist, ledger seeds where they do not."""
        priorities: dict[str, float] = {}
        for value in self._values.all():
            if view is None:
                priorities[value.name] = value.priority
            else:
                priorities[value.name] = (
                    view.number(DOMAIN, value.name, value.priority) or value.priority
                )
        return priorities

    def ranking(self) -> list[ValuePriority]:
        return sorted(self._values.all(), key=lambda item: -item.priority)
