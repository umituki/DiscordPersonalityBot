"""Memory Engine — the single writer of subjective memory (spec 9.3, 10).

Responsibilities, in the order of spec 35 Phase 4::

    segmentation → encoding → retrieval → accessibility → forgetting
    → semantic memory → consolidation → reconstruction

Boundaries this engine keeps:

* It is the only writer of memory tables. Other subsystems ask it, or emit
  evidence; they never write.
* Recall goes through :class:`MemoryRetriever` and touches only subjective
  memory — never the objective archive (spec 10.1).
* Forgetting lowers accessibility; nothing is deleted (spec 10.5).
* A revision keeps the previous version and never edits the source events
  (spec 10.6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from app import ids
from app.clock import Clock, SystemClock, to_iso
from app.events.model import Event, EventOrigin
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage
from app.memory.accessibility import (
    decayed_accessibility,
    elapsed_days,
    practised_accessibility,
)
from app.memory.encoding import (
    EncodingDecision,
    EncodingGate,
    EncodingSignals,
    EpisodeSummary,
    substance_of,
    user_directed_ratio,
)
from app.memory.models import Episode, EpisodicMemory, RetrievalCandidate, SemanticMemory
from app.memory.policy import MemoryPolicy
from app.memory.retrieval import MemoryRetriever
from app.memory.segmentation import EpisodeSegmenter
from app.memory.transcripts import Transcript, TranscriptSource
from app.storage.repositories.memory import MemoryRepository

logger = logging.getLogger(__name__)

COMPONENT = "memory_engine"
SUMMARY_PROMPT_ID = "episode_summary"
SUMMARY_PURPOSE = "episode_summary"


@dataclass(frozen=True, slots=True)
class EncodingResult:
    episode: Episode
    memory: EpisodicMemory | None
    decision: EncodingDecision | None
    reason: str

    @property
    def encoded(self) -> bool:
        return self.memory is not None


class MemoryEngine:
    def __init__(
        self,
        *,
        repository: MemoryRepository,
        policy: MemoryPolicy,
        structured: StructuredGenerator,
        prompts: PromptRegistry,
        transcripts: TranscriptSource,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._structured = structured
        self._prompts = prompts
        self._transcripts = transcripts
        self._clock = clock or SystemClock()
        self._segmenter = EpisodeSegmenter(policy.segmentation)
        self._gate = EncodingGate(policy.encoding)
        self._retriever = MemoryRetriever(repository, policy.retrieval, clock=self._clock)

    @property
    def retriever(self) -> MemoryRetriever:
        return self._retriever

    # --- segmentation ------------------------------------------------------
    def observe(self, event: Event, *, conversation_id: str | None = None) -> Episode:
        """Place an event into the current episode, opening one if needed."""
        current = self._repository.open_episode_for(conversation_id)
        if current is not None:
            decision = self._segmenter.decide(
                current,
                now=self._clock.now(),
                next_event_at=event.occurred_at,
                next_conversation_id=conversation_id,
            )
            if decision.close:
                self._repository.close_episode(
                    current.episode_id,
                    ended_at=current.ended_at or current.started_at,
                    reason=decision.reason or "boundary",
                )
                current = None

        if current is None:
            current = self._repository.open_episode(
                conversation_id=conversation_id,
                origin=event.origin,
                started_at=event.occurred_at,
                first_event_id=event.event_id,
            )
        else:
            current = self._repository.append_to_episode(current.episode_id, event.event_id)

        return self._touch(current, event.occurred_at)

    def close_due_episodes(
        self, *, now: datetime | None = None, shutting_down: bool = False
    ) -> list[Episode]:
        """Close episodes whose boundary has passed (silence, size, duration)."""
        moment = now or self._clock.now()
        closed: list[Episode] = []
        for episode in self._repository.episodes_with_status("open"):
            decision = self._segmenter.decide(
                episode, now=moment, shutting_down=shutting_down
            )
            if decision.close:
                closed.append(
                    self._repository.close_episode(
                        episode.episode_id,
                        ended_at=episode.ended_at or episode.started_at,
                        reason=decision.reason or "boundary",
                    )
                )
        return closed

    # --- encoding ----------------------------------------------------------
    async def encode_pending(self, *, limit: int = 10) -> list[EncodingResult]:
        results = []
        for episode in self._repository.episodes_with_status("closed", limit=limit):
            results.append(await self.encode_episode(episode))
        return results

    async def encode_episode(
        self,
        episode: Episode,
        *,
        emotional_intensity: float = 0.0,
        prediction_error: float = 0.0,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> EncodingResult:
        """Run one closed episode through the Encoding Gate."""
        existing = self._repository.memory_for_episode(episode.episode_id)
        if existing is not None:
            return EncodingResult(episode, existing, None, "already_encoded")

        if not self._segmenter.is_encodable(episode):
            self._repository.mark_episode(episode.episode_id, "discarded")
            return EncodingResult(episode, None, None, "too_small")

        transcript = self._transcripts.transcript_for(episode)
        if transcript.is_empty:
            self._repository.mark_episode(episode.episode_id, "discarded")
            return EncodingResult(episode, None, None, "no_transcript")

        summary = await self._summarise(transcript, episode, run_id=run_id, event_id=event_id)
        if summary is None:
            # A failed summarisation leaves the episode closed so it can be
            # retried; it never invents a memory (spec 28.2).
            return EncodingResult(episode, None, None, "summary_unavailable")

        signals = EncodingSignals(
            novelty=summary.novelty,
            emotional_intensity=max(emotional_intensity, summary.felt_significance * 0.5),
            prediction_error=prediction_error,
            user_directed=user_directed_ratio(transcript.user_turn_count, transcript.turn_count),
            substance=substance_of(transcript.text),
        )
        decision = self._gate.evaluate(signals)
        if not decision.encode:
            self._repository.mark_episode(episode.episode_id, "discarded")
            logger.info(
                "episode not encoded episode_id=%s importance=%.3f threshold=%.3f",
                episode.episode_id,
                decision.importance,
                self._gate.threshold,
            )
            return EncodingResult(episode, None, decision, "below_threshold")

        now = self._clock.now()
        encoding = self._policy.encoding
        memory = EpisodicMemory(
            memory_id=ids.new_id(ids.MEMORY),
            episode_id=episode.episode_id,
            origin=episode.origin,
            summary=summary.summary[: encoding.summary_max_chars],
            topics=tuple(topic.strip() for topic in summary.topics if topic.strip())[:5],
            importance=decision.importance,
            emotional_intensity=signals.clamped().emotional_intensity,
            accessibility=encoding.initial_accessibility,
            content_confidence=encoding.initial_content_confidence,
            source_confidence=encoding.initial_source_confidence,
            temporal_confidence=encoding.initial_temporal_confidence,
            novelty=signals.clamped().novelty,
            prediction_error=signals.clamped().prediction_error,
            occurred_at=episode.started_at,
            created_at=now,
            updated_at=now,
            last_decayed_at=now,
            source_event_ids=episode.event_ids,
        )
        stored = self._repository.insert_memory(memory)
        self._repository.mark_episode(episode.episode_id, "encoded")
        if not stored:
            return EncodingResult(
                episode,
                self._repository.memory_for_episode(episode.episode_id),
                decision,
                "already_encoded",
            )

        logger.info(
            "episode encoded episode_id=%s memory_id=%s importance=%.3f",
            episode.episode_id,
            memory.memory_id,
            memory.importance,
        )
        return EncodingResult(episode, memory, decision, "encoded")

    async def _summarise(
        self,
        transcript: Transcript,
        episode: Episode,
        *,
        run_id: str | None,
        event_id: str | None,
    ) -> EpisodeSummary | None:
        template = self._prompts.get(SUMMARY_PROMPT_ID)
        content = template.render(
            transcript=transcript.text, occurred_at=to_iso(episode.started_at)
        )
        outcome = await self._structured.generate(
            EpisodeSummary,
            (LLMMessage(role="user", content=content),),
            purpose=SUMMARY_PURPOSE,
            run_id=run_id,
            event_id=event_id,
            priority="P3",  # background work yields to a waiting USER (spec 33)
            prompt_id=SUMMARY_PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        return outcome.value if outcome.accepted else None

    # --- forgetting --------------------------------------------------------
    def apply_forgetting(self, *, now: datetime | None = None, limit: int = 1000) -> int:
        """Decay accessibility for every active memory. Deletes nothing."""
        moment = now or self._clock.now()
        policy = self._policy.forgetting
        changed = 0
        for memory in self._repository.all_memories(limit=limit):
            if memory.status != "active":
                continue
            days = elapsed_days(memory.last_decayed_at or memory.created_at, moment)
            if days <= 0:
                continue
            updated = decayed_accessibility(
                accessibility=memory.accessibility,
                importance=memory.importance,
                elapsed_days=days,
                policy=policy,
            )
            if abs(updated - memory.accessibility) > 1e-9:
                self._repository.update_accessibility(
                    memory.memory_id, accessibility=updated, now=moment, decayed=True
                )
                changed += 1
        return changed

    # --- recall ------------------------------------------------------------
    def recall(
        self,
        query_text: str,
        *,
        now: datetime | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        limit: int | None = None,
        origins: Sequence[str] | None = None,
        practise: bool = True,
    ) -> tuple[RetrievalCandidate, ...]:
        """Retrieve memories and strengthen the ones actually used (spec 10.5)."""
        moment = now or self._clock.now()
        candidates = self._retriever.retrieve(
            query_text, now=moment, origins=origins, limit=limit
        )
        for rank, candidate in enumerate(candidates):
            self._repository.record_retrieval(
                memory_id=candidate.memory_id,
                query=query_text,
                score=candidate.score,
                rank=rank,
                now=moment,
                used=True,
                run_id=run_id,
                event_id=event_id,
            )
            if practise:
                self._practise(candidate.memory, moment)
        return candidates

    def _practise(self, memory: EpisodicMemory, now: datetime) -> None:
        window_start = now - timedelta(hours=self._policy.practice.window_hours)
        recent = max(0, self._repository.retrievals_since(memory.memory_id, window_start) - 1)
        updated = practised_accessibility(
            accessibility=memory.accessibility,
            recent_practice_count=recent,
            policy=self._policy.practice,
        )
        self._repository.update_accessibility(
            memory.memory_id, accessibility=updated, now=now, recalled=True
        )

    # --- reconstruction ----------------------------------------------------
    def revise(
        self,
        memory_id: str,
        *,
        new_summary: str,
        reason_code: str,
        content_confidence: float | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Change what is remembered, keeping the previous version (spec 10.6)."""
        return self._repository.revise(
            memory_id=memory_id,
            new_summary=new_summary,
            reason_code=reason_code,
            now=self._clock.now(),
            content_confidence=content_confidence,
            run_id=run_id,
            event_id=event_id,
        )

    def suppress(self, memory_id: str) -> None:
        """Admin suppression: unreachable by recall, still on disk (spec 30)."""
        self._repository.set_status(memory_id, "suppressed", now=self._clock.now())

    def invalidate(self, memory_id: str) -> None:
        self._repository.set_status(memory_id, "invalidated", now=self._clock.now())

    def restore(self, memory_id: str) -> None:
        self._repository.set_status(memory_id, "active", now=self._clock.now())

    # --- semantic memory ---------------------------------------------------
    def note_semantic(
        self,
        statement: str,
        *,
        origin: EventOrigin,
        topics: Sequence[str] = (),
        source_memory_ids: Sequence[str] = (),
    ) -> SemanticMemory:
        """Record a general fact, growing confidence with repeated support."""
        policy = self._policy.semantic
        existing = self._repository.semantic_by_statement(statement, origin)
        support = 1 if existing is None else existing.support_count + 1
        confidence = min(
            policy.max_confidence,
            policy.initial_confidence + policy.confidence_per_support * (support - 1),
        )
        return self._repository.upsert_semantic(
            statement=statement,
            origin=origin,
            topics=topics,
            confidence=confidence,
            stability="CHANGEABLE",
            now=self._clock.now(),
            source_memory_ids=source_memory_ids,
        )

    def promoted_semantic(self, *, limit: int = 20) -> list[SemanticMemory]:
        """Facts with enough independent support to be treated as known."""
        threshold = self._policy.semantic.promotion_support_count
        return [
            fact
            for fact in self._repository.active_semantic(limit=limit * 3)
            if fact.support_count >= threshold
        ][:limit]

    # --- helpers -----------------------------------------------------------
    def _touch(self, episode: Episode, moment: datetime) -> Episode:
        """Keep ``ended_at`` as the episode's last activity while it is open."""
        return self._repository.touch_episode(episode.episode_id, moment)
