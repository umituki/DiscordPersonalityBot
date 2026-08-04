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

import dataclasses
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
from app.memory.models import (
    Episode,
    EpisodicMemory,
    SemanticMemory,
    SemanticSubject,
)
from app.memory.policy import MemoryPolicy
from app.memory.recall_mode import RecallMode, classify
from app.memory.recall_models import RetrievalReport
from app.memory.relevance import SemanticReranker
from app.memory.retrieval import MemoryRetriever
from app.memory.segmentation import EpisodeSegmenter
from app.memory.material import EpisodeMaterial, EpisodeMaterialSource
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
        material: EpisodeMaterialSource,
        reranker: SemanticReranker | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._structured = structured
        self._prompts = prompts
        self._material = material
        self._clock = clock or SystemClock()
        self._segmenter = EpisodeSegmenter(policy.segmentation)
        self._gate = EncodingGate(policy.encoding)
        self._retriever = MemoryRetriever(
            repository,
            policy.retrieval,
            # Stage 2 needs the model. Without it the retriever falls back to
            # the conservative lexical check rather than passing candidates
            # through unjudged (§2E failure policy).
            reranker=reranker
            or SemanticReranker(structured=structured, prompts=prompts),
            clock=self._clock,
        )

    @property
    def retriever(self) -> MemoryRetriever:
        return self._retriever

    # --- segmentation ------------------------------------------------------
    def observe(
        self,
        event: Event,
        *,
        conversation_id: str | None = None,
        now: datetime | None = None,
    ) -> Episode:
        """Place an event into the current episode, opening one if needed.

        ``now`` is the effective time (patch spec 13). During a simulation it
        is the simulated moment, so a boundary is measured in the life's own
        time rather than in the seconds the machine spent on it.
        """
        moment = now or self._clock.now()
        current = self._repository.open_episode_for(conversation_id, origin=event.origin)
        if current is not None:
            decision = self._segmenter.decide(
                current,
                now=moment,
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
    async def encode_pending(
        self, *, limit: int = 10, now: datetime | None = None
    ) -> list[EncodingResult]:
        results = []
        for episode in self._repository.episodes_with_status("closed", limit=limit):
            results.append(await self.encode_episode(episode, now=now))
        return results

    async def encode_episode(
        self,
        episode: Episode,
        *,
        emotional_intensity: float = 0.0,
        prediction_error: float = 0.0,
        run_id: str | None = None,
        event_id: str | None = None,
        now: datetime | None = None,
    ) -> EncodingResult:
        """Run one closed episode through the Encoding Gate."""
        existing = self._repository.memory_for_episode(episode.episode_id)
        if existing is not None:
            return EncodingResult(episode, existing, None, "already_encoded")

        if not self._segmenter.is_encodable(episode):
            self._repository.mark_episode(episode.episode_id, "discarded")
            return EncodingResult(episode, None, None, "too_small")

        transcript = self._material.material_for(episode)
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

        # Patch spec 15.3: a memory formed in a simulated 2003 was created
        # then, and starts decaying from then. Stamping it with wall-clock now
        # would leave nineteen years of memories all equally fresh.
        moment = now or self._clock.now()
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
            created_at=moment,
            updated_at=moment,
            last_decayed_at=moment,
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
        transcript: EpisodeMaterial,
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

    # --- recall (Stages 1-3, then Stage 4) ---------------------------------
    async def recall(
        self,
        query_text: str,
        *,
        mode: RecallMode | None = None,
        now: datetime | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        origins: Sequence[str] | None = None,
        cues: Sequence[str] = (),
    ) -> RetrievalReport:
        """Run the four stages, and record every one of them (spec 17.3, 17.5).

        Practice does **not** happen here. Being selected into context is not
        remembering: for a deliberate mode she is genuinely recollecting, and
        that practises; for ordinary conversation the memory has only been put
        within reach, and whether it was used is decided after the reply exists
        (:meth:`mark_used_in_reply`).
        """
        moment = now or self._clock.now()
        resolved = mode or classify(query_text)
        report = await self._retriever.retrieve(
            query_text,
            mode=resolved,
            now=moment,
            origins=origins,
            cues=cues,
            run_id=run_id,
            event_id=event_id,
        )
        self._record(report, run_id=run_id, event_id=event_id, now=moment)

        if not resolved.is_deliberate or not report.selected:
            return report

        # §2K: she was trying to remember, and this is what came. That is a
        # conscious recall, and conscious recall is one of the two things that
        # practise.
        practised = []
        for item in report.selected:
            self._repository.promote_retrieval(
                group_id=report.group_id,
                memory_id=item.memory_id,
                state="consciously_recalled",
                practice_applied=True,
            )
            self._practise(item.memory, moment)
            practised.append(item.memory_id)
        return dataclasses.replace(report, practised=tuple(practised))

    def associate(
        self,
        cues: Sequence[str],
        *,
        now: datetime | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        origins: Sequence[str] | None = None,
    ):
        """Recall from cues rather than from a question (spec 17.6, §2N).

        The Autonomous Runtime will hand this the current activity, mood,
        recent topics, an NPC, a place, an anniversary. Nothing drives it yet —
        the API exists so that when something does, it goes through the same
        four stages as everything else rather than growing a second, simpler
        retrieval path beside them.
        """
        return self.recall(
            " ".join(cue for cue in cues if cue),
            mode=RecallMode.ASSOCIATIVE,
            now=now,
            run_id=run_id,
            event_id=event_id,
            origins=origins,
            cues=cues,
        )

    def mark_used_in_reply(
        self,
        report: RetrievalReport,
        reply_text: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Stage 4 (§2J, §2K). Practise what the reply actually rests on.

        Called after a reply has been *delivered*. A memory that sat in the
        context and left no mark on the sentence was available, not used, and
        availability is not something to strengthen — that was the old bug.
        """
        if not report.selected or not reply_text.strip():
            return ()
        moment = now or self._clock.now()
        already = set(report.practised)
        used: list[str] = []
        reply_tokens = _content_tokens(reply_text)
        for item in report.selected:
            if not (reply_tokens & _content_tokens(item.memory.summary)):
                continue
            self._repository.promote_retrieval(
                group_id=report.group_id,
                memory_id=item.memory_id,
                state="used_in_reply",
                used_in_reply=True,
                practice_applied=True,
            )
            used.append(item.memory_id)
            if item.memory_id not in already:
                self._practise(item.memory, moment)
        return tuple(used)

    def mark_spontaneously_recalled(
        self,
        report: RetrievalReport,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Commit associative selections as an actual spontaneous recall.

        ``associate`` only makes memories available. The autonomous action,
        after winning the normal decision, calls this method to cross the
        practice boundary. The repository's conditional transition makes the
        operation idempotent across action retries.
        """
        if report.mode is not RecallMode.ASSOCIATIVE or not report.selected:
            return ()
        moment = now or self._clock.now()
        practised: list[str] = []
        for item in report.selected:
            if not self._repository.promote_retrieval_once(
                group_id=report.group_id,
                memory_id=item.memory_id,
                state="spontaneously_recalled",
            ):
                continue
            self._practise(item.memory, moment)
            practised.append(item.memory_id)
        return tuple(practised)

    def _record(
        self,
        report: RetrievalReport,
        *,
        run_id: str | None,
        event_id: str | None,
        now: datetime,
    ) -> None:
        """One row per candidate, whatever became of it (observability)."""
        selected = {item.memory_id: item for item in report.selected}
        rejected = {item.memory_id: item for item in report.rejected}
        for rank, candidate in enumerate(report.candidates):
            judgement = report.judgement_for(candidate.memory_id)
            chosen = selected.get(candidate.memory_id)
            refusal = rejected.get(candidate.memory_id)
            if chosen is not None:
                state = "selected"
            elif judgement is not None and judgement.passed:
                state = "relevance_passed"
            else:
                state = "candidate"
            self._repository.record_retrieval(
                memory_id=candidate.memory_id,
                query=report.query,
                score=chosen.availability if chosen else 0.0,
                rank=rank,
                now=now,
                used=chosen is not None,
                run_id=run_id,
                event_id=event_id,
                group_id=report.group_id,
                mode=report.mode.value,
                state=state,
                relevance=judgement.relevance if judgement else "",
                relevance_source=judgement.source if judgement else report.relevance_source,
                relevance_reason=judgement.reason if judgement else "",
                reject_stage=refusal.stage if refusal else "",
                reject_reason=refusal.reason if refusal else "",
                accessibility_at=candidate.memory.accessibility,
                availability=chosen.availability if chosen else (
                    refusal.availability if refusal else None
                ),
                candidate_reasons=candidate.reasons,
                llm_call_id=report.llm_call_id,
            )

    def _practise(self, memory: EpisodicMemory, now: datetime) -> None:
        window_start = now - timedelta(hours=self._policy.practice.window_hours)
        # §2L: only prior *practice* counts as repetition. Candidate lookups do
        # not, which is why this reads ``practices_since`` and not the old
        # ``retrievals_since``.
        recent = self._repository.practices_since(memory.memory_id, window_start)
        updated = practised_accessibility(
            accessibility=memory.accessibility,
            recent_practice_count=max(0, recent - 1),
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
        subject: SemanticSubject,
        topics: Sequence[str] = (),
        source_memory_ids: Sequence[str] = (),
    ) -> SemanticMemory:
        """Record a general fact, growing confidence with repeated support.

        ``subject`` has no default. Whoever forms the knowledge knows what it is
        about — consolidation is generalising *her* episodes, acquisition is
        taking in a fact about the world — and requiring it here is what keeps
        that answer from being reconstructed later by reading the sentence.
        """
        policy = self._policy.semantic
        existing = self._repository.semantic_by_statement(statement, origin, subject)
        support = 1 if existing is None else existing.support_count + 1
        confidence = min(
            policy.max_confidence,
            policy.initial_confidence + policy.confidence_per_support * (support - 1),
        )
        return self._repository.upsert_semantic(
            statement=statement,
            origin=origin,
            subject=subject,
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


#: Grammatical scaffolding, dropped before comparing what a reply and a memory
#: have in common. Japanese has no spaces, so overlap is measured on bigrams of
#: what is left.
_PARTICLES = frozenset("はがをにでとへもやのねよなかだですますましたたるらしいうくっ、。！？!?　 ")


def _content_tokens(text: str) -> frozenset[str]:
    kept = [char for char in (text or "") if char not in _PARTICLES]
    if len(kept) < 2:
        return frozenset(kept)
    return frozenset("".join(kept[i : i + 2]) for i in range(len(kept) - 1))
