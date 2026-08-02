"""Memory retrieval (spec 10.1, 10.7).

**This is the only path normal recall may take.** It reads subjective memory
tables and never the objective archive in ``events``: if an episode was never
encoded, or its memory has faded or been suppressed, YUI simply does not
remember it (spec 2.4, 34.2-1).

Scoring starts with FTS5 plus a weighted blend of accessibility, recency,
emotional salience and importance (spec 10.7). Embeddings come later, if
measurement shows they are needed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Sequence

from app.clock import Clock, SystemClock
from app.memory.models import EpisodicMemory, RetrievalCandidate
from app.memory.policy import RetrievalPolicy
from app.storage.repositories.memory import MemoryRepository

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_NON_WORD = re.compile(r"[^\w\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+")

MAX_MATCH_TERMS = 12

_AGE_QUESTION = re.compile(r"(?:何\s*歳|年齢)")
_MEMORY_ORIGIN_QUESTION = re.compile(
    r"(?:生まれ|誕生|最初.{0,8}記憶|いつ.{0,8}記憶|記憶.{0,8}いつ)"
)
_AGE_MEMORY = re.compile(r"[0-9０-９]{1,3}\s*歳")
_ORIGIN_MEMORY = re.compile(r"(?:生まれ|誕生|出生|幼少|成長|人生|生涯)")
_DATED_MEMORY = re.compile(r"[0-9０-９]{4}\s*年")


def build_match_query(text: str, *, min_chars: int = 3, max_terms: int = MAX_MATCH_TERMS) -> str | None:
    """Turn free text into an FTS5 trigram query.

    Japanese has no word boundaries, so long runs are sliced into overlapping
    fragments; Latin words are used whole.
    """
    cleaned = _NON_WORD.sub(" ", text or "").strip()
    if not cleaned:
        return None

    terms: list[str] = []
    for token in cleaned.split():
        if len(token) < min_chars:
            continue
        if _CJK.search(token) and len(token) > 4:
            for start in range(0, len(token) - 2, 2):
                fragment = token[start : start + 3]
                if len(fragment) >= min_chars:
                    terms.append(fragment)
        else:
            terms.append(token)

    unique = list(dict.fromkeys(terms))[:max_terms]
    if not unique:
        return None
    return " OR ".join(f'"{term}"' for term in unique)


class MemoryRetriever:
    def __init__(
        self,
        repository: MemoryRepository,
        policy: RetrievalPolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()

    def retrieve(
        self,
        query_text: str,
        *,
        now: datetime | None = None,
        origins: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> tuple[RetrievalCandidate, ...]:
        moment = now or self._clock.now()
        wanted = self._policy.limit if limit is None else limit
        if wanted <= 0:
            return ()

        match_query = (
            build_match_query(query_text, min_chars=self._policy.min_query_chars)
            if query_text
            else None
        )
        matched: list[EpisodicMemory] = []
        if match_query:
            try:
                matched = self._repository.search(
                    match_query=match_query,
                    limit=self._policy.candidate_pool,
                    origins=origins,
                )
            except Exception:  # noqa: BLE001 - a bad query must not break a reply
                logger.exception("full-text search failed query=%r", match_query)
                matched = []

        matched_ids = {memory.memory_id for memory in matched}
        fallback = [
            memory
            for memory in self._repository.search(
                match_query=None, limit=self._policy.candidate_pool, origins=origins
            )
            if memory.memory_id not in matched_ids
        ]

        candidates: list[RetrievalCandidate] = []
        for rank, memory in enumerate(matched):
            candidates.append(self._score(memory, moment, relevance=1.0 / (1.0 + rank)))
        for memory in fallback:
            candidates.append(
                self._score(
                    memory,
                    moment,
                    relevance=self._conceptual_relevance(query_text, memory),
                )
            )

        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        selected = [
            candidate
            for candidate in candidates
            if candidate.score >= self._policy.min_score
        ][:wanted]
        return tuple(selected)

    @staticmethod
    def _conceptual_relevance(query_text: str, memory: EpisodicMemory) -> float:
        """Bridge short Japanese questions to autobiographical wording.

        FTS5 trigram search is deliberately literal.  A question such as
        ``何歳だっけ？`` has no three-character fragment in common with a
        memory written as ``2007年7月に誕生し、現在は19歳``.  Treating those
        as unrelated made Genesis memories effectively invisible even though
        they existed.  This narrow fallback only boosts an autobiographical
        memory when the question itself asks for autobiographical facts; it
        does not make the objective event archive recallable.
        """
        if not query_text:
            return 0.0
        material = f"{memory.summary} {' '.join(memory.topics)}"
        if _AGE_QUESTION.search(query_text):
            if _AGE_MEMORY.search(material):
                return 1.0
            if _ORIGIN_MEMORY.search(material):
                return 0.8
            return 0.55 if _DATED_MEMORY.search(material) else 0.0
        if _MEMORY_ORIGIN_QUESTION.search(query_text):
            if _ORIGIN_MEMORY.search(material):
                return 1.0
            if _AGE_MEMORY.search(material):
                return 0.8
            return 0.55 if _DATED_MEMORY.search(material) else 0.0
        return 0.0

    def _score(
        self, memory: EpisodicMemory, now: datetime, *, relevance: float
    ) -> RetrievalCandidate:
        weights = self._policy.weights
        age_days = max(0.0, (now - memory.occurred_at).total_seconds() / 86400.0)
        recency = 0.5 ** (age_days / self._policy.recency_half_life_days)

        components = {
            "relevance": weights.relevance * relevance,
            "accessibility": weights.accessibility * memory.accessibility,
            "recency": weights.recency * recency,
            "emotional_salience": weights.emotional_salience * memory.emotional_intensity,
            "importance": weights.importance * memory.importance,
        }
        return RetrievalCandidate(
            memory=memory,
            score=round(sum(components.values()), 6),
            relevance=relevance,
            components=components,
        )
