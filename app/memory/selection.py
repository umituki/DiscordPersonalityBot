"""Stage 3 — Recall Availability / Selection (rebuild spec 17.3, Phase 2).

This is the first and only stage that looks at accessibility, and it looks at
it *after* relevance has already been settled. The ordering is the whole design:

    意味的には関係ある
    ↓
    でも今は思い出しにくい
    ↓
    Recall されない

That state has to be reachable. A person can know a memory exists and fail to
bring it up; a system where every relevant memory always surfaces is not
modelling remembering, it is modelling a database query.

Stage 3 is also allowed to choose nothing at all (§2I). Zero recalled memories
is a normal outcome — most sentences in a conversation do not remind anyone of
anything.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Sequence

from app.memory.policy import RetrievalPolicy
from app.memory.recall_mode import RecallMode
from app.memory.recall_models import (
    Candidate,
    RecalledMemory,
    RejectedCandidate,
    RelevanceJudgement,
)

logger = logging.getLogger(__name__)


class RecallSelector:
    """Stage 3. Reads only; the write happens in Stage 4."""

    name = "recall_selector"

    def __init__(self, policy: RetrievalPolicy) -> None:
        self._policy = policy

    def select(
        self,
        candidates: Sequence[Candidate],
        judgements: Sequence[RelevanceJudgement],
        *,
        mode: RecallMode,
        now: datetime,
    ) -> tuple[tuple[RecalledMemory, ...], tuple[RejectedCandidate, ...]]:
        rules = self._policy.relevance_for(mode.value)
        by_id = {judgement.memory_id: judgement for judgement in judgements}
        rejected: list[RejectedCandidate] = []
        passed: list[tuple[Candidate, RelevanceJudgement, float]] = []

        for candidate in candidates:
            judgement = by_id.get(candidate.memory_id)
            if judgement is None:
                rejected.append(
                    RejectedCandidate(
                        memory_id=candidate.memory_id,
                        stage="relevance",
                        reason="判定なし",
                    )
                )
                continue

            # §2F, the hard gate. Nothing below this line can be rescued by
            # accessibility, and nothing above it is admitted because of it.
            if not self._passes(judgement, rules.accept_weak):
                rejected.append(
                    RejectedCandidate(
                        memory_id=candidate.memory_id,
                        stage="relevance",
                        reason=judgement.reason or f"relevance={judgement.relevance}",
                        relevance=judgement.relevance,
                        availability=None,
                    )
                )
                continue

            availability = self.availability(candidate, judgement, now=now)
            if availability < rules.min_availability:
                # Relevant, and she cannot bring it to mind right now.
                rejected.append(
                    RejectedCandidate(
                        memory_id=candidate.memory_id,
                        stage="availability",
                        reason=(
                            f"関係はあるが想起しにくい "
                            f"({availability:.2f} < {rules.min_availability:.2f})"
                        ),
                        relevance=judgement.relevance,
                        availability=availability,
                    )
                )
                continue
            passed.append((candidate, judgement, availability))

        passed.sort(key=lambda item: item[2], reverse=True)
        chosen = passed[: rules.max_recalled]
        for candidate, judgement, availability in passed[rules.max_recalled :]:
            rejected.append(
                RejectedCandidate(
                    memory_id=candidate.memory_id,
                    stage="availability",
                    reason=f"上位 {rules.max_recalled} 件に入らなかった",
                    relevance=judgement.relevance,
                    availability=availability,
                )
            )

        selected = tuple(
            RecalledMemory(
                memory=candidate.memory,
                relevance=judgement.relevance,
                relevance_reason=judgement.reason,
                availability=availability,
                state="selected",
                reasons=candidate.reasons,
            )
            for candidate, judgement, availability in chosen
        )
        return selected, tuple(rejected)

    @staticmethod
    def _passes(judgement: RelevanceJudgement, accept_weak: bool) -> bool:
        if judgement.passed:
            return True
        return accept_weak and judgement.relevance == "weak"

    def availability(
        self,
        candidate: Candidate,
        judgement: RelevanceJudgement,
        *,
        now: datetime,
    ) -> float:
        """How easily this memory comes to mind, given that it is relevant.

        Everything here is about reachability — how well-worn the memory is,
        how much it mattered, how strongly it was felt, how long ago it was.
        Relevance appears only as a small bonus for a ``strong`` judgement,
        because being unmistakably on topic is itself a retrieval cue. It
        cannot lift an unreachable memory over the bar on its own.
        """
        memory = candidate.memory
        weights = self._policy.weights
        age_days = max(0.0, (now - memory.occurred_at).total_seconds() / 86400.0)
        recency = 0.5 ** (age_days / self._policy.recency_half_life_days)

        score = (
            weights.accessibility * memory.accessibility
            + weights.emotional_salience * memory.emotional_intensity
            + weights.importance * memory.importance
            + weights.recency * recency
        )
        if judgement.relevance == "strong":
            score += self._policy.strong_relevance_bonus
        return round(min(1.0, score), 6)


__all__ = ["RecallSelector"]
