"""Curiosity, connected to the runtime (rebuild spec 17, 32 — Phase 11).

The rule this file exists to keep is 17.1's::

    未知 = 自動 Web Search にしてはならない。

So the builder does not turn a gap into a search. It asks the Epistemic Action
Selector what to do about the gap, records the answer, and produces a search
candidate **only** when the answer was ``web_search``. A gap that the selector
resolves with ``recall``, ``infer``, ``ask_user``, ``defer``, ``ignore`` or
``avoid`` leaves no search candidate behind — and leaves a row saying which of
those it was, so the distribution is auditable rather than asserted.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Sequence

from app.agency.models import ActionCandidate
from app.clock import Clock, SystemClock
from app.epistemics.actions import KnowledgeGap
from app.world.models import Opportunity

logger = logging.getLogger(__name__)

MODULE = "knowledge_runtime"

KNOWLEDGE_GAP = "knowledge_gap"
INVESTIGATE = "investigate"


def gap_from_row(row: Any) -> KnowledgeGap:
    return KnowledgeGap(
        topic=row["topic"],
        known_part=row["known_part"],
        unknown_part=row["unknown_part"],
        uncertainty=float(row["uncertainty"]),
        relevance=float(row["relevance"]),
        curiosity=float(row["curiosity"]),
        time_sensitive=bool(row["time_sensitive"]),
    )


class GapSource:
    """Open questions she has not resolved. Reads only."""

    name = "knowledge_gaps"

    def __init__(self, gaps: Any, world: Any, *, clock: Clock | None = None) -> None:
        self._gaps = gaps
        self._world = world
        self._clock = clock or SystemClock()

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        if self._world.current_sleep() is not None:
            return []
        return [
            Opportunity(
                kind=KNOWLEDGE_GAP,
                detail=row["gap_id"],
                urgency=float(row["relevance"]),
                created_at=now,
            )
            for row in self._gaps.open_gaps(limit=3)
        ]

    def next_due(self, now: datetime) -> datetime | None:
        return None


class KnowledgeCandidates:
    """Turns a gap into a search candidate — or into nothing at all."""

    name = "knowledge_candidates"

    def __init__(self, gaps: Any, investigation: Any, *, clock: Clock | None = None) -> None:
        self._gaps = gaps
        self._investigation = investigation
        self._clock = clock or SystemClock()

    def investigate(
        self, opportunity: Opportunity, now: datetime
    ) -> ActionCandidate | None:
        row = self._gaps.get(opportunity.detail)
        if row is None or row["status"] != "open":
            return None
        gap = gap_from_row(row)

        # 17.1, structurally. The selector runs here, and only one of its seven
        # answers produces something the Decision Engine can choose to do.
        decision = self._investigation.decide(gap, gap_id=opportunity.detail)
        if not decision.searches:
            # Not a failure. Most gaps are answered some other way or not worth
            # answering, and the gap row now says which.
            if decision.action in ("ignore", "avoid"):
                self._gaps.close(
                    opportunity.detail, now=now, status=decision.action
                )
            return None

        return ActionCandidate(
            action=INVESTIGATE,
            route="goal_directed",
            expected_value=round(min(0.75, 0.3 + 0.5 * gap.relevance), 6),
            reason=opportunity.detail,
        )


class KnowledgeActions:
    """Runs the investigation. Everything real happens inside it."""

    name = "knowledge_actions"

    def __init__(
        self,
        investigation: Any,
        gaps: Any,
        *,
        candidates: KnowledgeCandidates | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._investigation = investigation
        self._gaps = gaps
        self._candidates = candidates or KnowledgeCandidates(gaps, investigation)
        self._clock = clock or SystemClock()

    async def investigate(self, candidate: ActionCandidate, now: datetime) -> bool:
        row = self._gaps.get(candidate.reason)
        if row is None:
            return False
        outcome = await self._investigation.investigate(
            gap_from_row(row),
            # In ordinary running she searches from now. A past simulation
            # passes its own moment; the parameter has no default anywhere in
            # this chain, which is what stops today's clock leaking into 2012.
            effective_now=now,
            gap_id=row["gap_id"],
        )
        # The *action* happened if a search ran, whatever it found. Reporting
        # only successful learning as action would make a failed search look
        # like nothing was attempted.
        return outcome.outcome != "refused"

    def register(self, registry: Any, *, sources: Sequence[Any] = ()) -> None:
        for source in sources:
            registry.add_source(source)
        registry.add_builder(KNOWLEDGE_GAP, self._candidates.investigate)
        registry.add_handler(INVESTIGATE, self.investigate)


__all__ = [
    "INVESTIGATE",
    "KNOWLEDGE_GAP",
    "GapSource",
    "KnowledgeActions",
    "KnowledgeCandidates",
    "gap_from_row",
]
