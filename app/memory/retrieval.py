"""Memory retrieval — Stages 1 to 3 (rebuild spec 17.3, Phase 2 §2A).

**This is the only path normal recall may take.** It reads subjective memory
tables and never the objective archive in ``events`` (§2H): if an episode was
never encoded, or its memory has faded or been suppressed, YUI simply does not
remember it (spec 2.4, 34.2-1). There is no quiet second lookup in
``events``/``life_months``/``life_years`` when nothing was found. "I don't
remember" is a real answer.

The three stages are separate objects, called in order, with separate
responsibilities::

    CandidateGenerator   might this be about the query?     (no side effects)
    SemanticReranker     is it actually about the query?    (hard gate)
    RecallSelector       can she bring it to mind now?      (accessibility)

Stage 4 — actually using a memory, and practising it — is not here. It belongs
to :class:`app.memory.engine.MemoryEngine`, because it is the only stage that
writes, and separating it is the whole of §2K: retrieving is not remembering.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Sequence

from app import ids
from app.clock import Clock, SystemClock
from app.memory.candidates import CandidateGenerator, build_match_query
from app.memory.policy import RetrievalPolicy
from app.memory.recall_mode import RecallMode
from app.memory.recall_models import RetrievalReport
from app.memory.relevance import SemanticReranker, fallback_judgements
from app.memory.selection import RecallSelector
from app.storage.repositories.memory import MemoryRepository

logger = logging.getLogger(__name__)

__all__ = ["MemoryRetriever", "build_match_query"]


class MemoryRetriever:
    """Stages 1-3, and nothing that writes."""

    def __init__(
        self,
        repository: MemoryRepository,
        policy: RetrievalPolicy,
        *,
        reranker: SemanticReranker | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()
        self._candidates = CandidateGenerator(repository, policy.candidates)
        self._selector = RecallSelector(policy)
        #: Optional so a retriever can be built before the LLM is wired. With
        #: no reranker there is no model to ask, so the conservative fallback
        #: runs — never an unjudged pass-through.
        self._reranker = reranker

    @property
    def selector(self) -> RecallSelector:
        return self._selector

    async def retrieve(
        self,
        query_text: str,
        *,
        mode: RecallMode = RecallMode.CONVERSATIONAL,
        now: datetime | None = None,
        origins: Sequence[str] | None = None,
        cues: Sequence[str] = (),
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> RetrievalReport:
        """Run the three read stages and report everything they did."""
        moment = now or self._clock.now()
        group_id = ids.new_id(ids.RETRIEVAL)

        # Stage 1. No side effects (§2C).
        candidates = self._candidates.generate(
            query_text, mode=mode, now=moment, origins=origins, cues=cues
        )
        if not candidates:
            return RetrievalReport(
                query=query_text,
                mode=mode,
                retrieved_at=moment,
                group_id=group_id,
                notes=("候補なし",),
            )

        # Stage 2. The hard gate (§2E, §2F).
        if self._reranker is None:
            judgements = fallback_judgements(query_text, candidates)
            call_id, source = None, "fallback"
        else:
            judgements, call_id, source = await self._reranker.judge(
                query_text,
                candidates,
                mode=mode,
                batch_size=self._policy.rerank_batch,
                run_id=run_id,
                event_id=event_id,
            )

        # Stage 3. Only now does accessibility matter.
        selected, rejected = self._selector.select(
            candidates, judgements, mode=mode, now=moment
        )

        report = RetrievalReport(
            query=query_text,
            mode=mode,
            candidates=candidates,
            judgements=judgements,
            selected=selected,
            rejected=rejected,
            llm_call_id=call_id,
            relevance_source=source,
            retrieved_at=moment,
            group_id=group_id,
        )
        logger.debug(
            "retrieval mode=%s candidates=%d passed=%d selected=%d source=%s",
            mode.value,
            len(candidates),
            len(report.passed_ids),
            len(selected),
            source,
        )
        return report
