"""INVARIANT: the whole retrieval path fires, end to end (Phase 2 DoD).

Phase 2 is not finished because ``MemoryRetriever`` was rewritten. It is
finished when this path runs in one go, driven by a real inbound message::

    Candidate Generation → LLM Relevance → hard relevance gate
    → Accessibility selection → Conversation Context → actual reply
    → used-memory marking → Practice → DB → Debug inspector

and when the fixture database afterwards actually contains

    candidate > 0
    relevance judgements > 0
    selected recalls > 0
    practice rows > 0
    rejected irrelevant memories > 0

Every one of those counts is asserted below. A path that never rejects anything
is not a gate, and a path that never practises anything has not reached Stage 4.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.conversation.engine import ConversationEngine
from app.conversation.service import ConversationService
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMResponse
from app.memory.engine import MemoryEngine
from app.memory.inspector import MemoryInspector
from app.memory.material import ConversationEpisodeSource
from app.memory.models import EpisodicMemory
from app.memory.relevance import SemanticReranker
from app.orchestrator.processor import EventProcessor
from tests.unit.test_conversation import (  # noqa: F401 - shared fixtures
    CHANNEL,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    inbound,
)
from tests.unit.test_memory import memories, memory_policy  # noqa: F401

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

ACTS = (
    '{"acknowledge": true, "self_disclose": true, "goal": "maintain_connection", '
    '"mode": "smalltalk", "question_need": "none", "reciprocity": "high"}'
)

#: The reply repeats what she remembered, so Stage 4 can see that it rests on
#: it. A reply that ignored the memory would (correctly) practise nothing.
REPLY = '{"text": "海に行った話、よかったね。"}'

RELEVANT_SUMMARY = "海に行った話をした"
IRRELEVANT_SUMMARY = "プログラミングの本を読んだ"


class PipelineClient:
    """One scripted model for every purpose the turn needs.

    The relevance answer is built from the ids the prompt actually lists, so
    the real Stage 2 path runs: prompt rendering, schema validation, id
    matching. The irrelevant memory is labelled irrelevant by name, which is
    what makes the rejection count meaningful.
    """

    model = "test-model"

    def __init__(self, *, memory_labels: dict[str, str]) -> None:
        self.memory_labels = memory_labels
        self.purposes: list[str] = []

    async def generate(self, request):
        schema = (request.format_schema or {}).get("title", "")
        self.purposes.append(request.purpose)
        if schema == "DialogueAct":
            text = ACTS
        elif schema == "MemoryRelevanceBatch":
            text = self._judge(request.messages[0].content)
        elif schema == "ReplyDraft":
            text = REPLY
        elif schema == "EpisodeSummary":
            text = (
                '{"summary": "海の話をした", "topics": ["海"], '
                '"novelty": 0.6, "felt_significance": 0.5}'
            )
        else:
            raise AssertionError(f"unscripted schema: {schema!r}")
        return LLMResponse(text=text, model=self.model, created_at=NOW, latency_ms=5)

    def _judge(self, prompt: str) -> str:
        ids = re.findall(r"memory_id: (\S+)", prompt)
        return json.dumps(
            {
                "judgements": [
                    {
                        "memory_id": memory_id,
                        "relevance": self.memory_labels.get(memory_id, "irrelevant"),
                        "reason": "テスト",
                    }
                    for memory_id in ids
                ]
            },
            ensure_ascii=False,
        )

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _store(repository, *, memory_id: str, summary: str, topics: tuple[str, ...],
           accessibility: float, occurred_at: datetime) -> EpisodicMemory:
    episode = repository.open_episode(
        conversation_id=None, origin="real_discord", started_at=occurred_at
    )
    repository.close_episode(episode.episode_id, ended_at=occurred_at, reason="test")
    memory = EpisodicMemory(
        memory_id=memory_id,
        episode_id=episode.episode_id,
        origin="real_discord",
        summary=summary,
        topics=topics,
        importance=0.6,
        emotional_intensity=0.4,
        accessibility=accessibility,
        content_confidence=0.85,
        source_confidence=0.9,
        temporal_confidence=0.85,
        novelty=0.5,
        prediction_error=0.1,
        occurred_at=occurred_at,
        created_at=occurred_at,
        updated_at=occurred_at,
        last_decayed_at=occurred_at,
    )
    repository.insert_memory(memory)
    return memory


@pytest.fixture
def pipeline(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy,  # noqa: F811
    memories, memory_policy, clock,  # noqa: F811
):
    """The real service, with two memories planted: one on topic, one not."""
    relevant = _store(
        memories,
        memory_id="mem_relevant",
        summary=RELEVANT_SUMMARY,
        topics=("海",),
        accessibility=0.75,
        occurred_at=clock.now() - timedelta(days=2),
    )
    # High accessibility and nothing to do with the question — the exact shape
    # of the memory that used to surface in every conversation.
    irrelevant = _store(
        memories,
        memory_id="mem_irrelevant",
        summary=IRRELEVANT_SUMMARY,
        topics=("本", "海"),
        accessibility=1.0,
        occurred_at=clock.now() - timedelta(days=1),
    )

    client = PipelineClient(memory_labels={relevant.memory_id: "strong"})
    generator = StructuredGenerator(
        client, prompts=prompt_registry, clock=clock, max_attempts=1
    )
    memory_engine = MemoryEngine(
        repository=memories,
        policy=memory_policy,
        structured=generator,
        prompts=prompt_registry,
        material=ConversationEpisodeSource(conversations, yui_label=identity.name),
        reranker=SemanticReranker(structured=generator, prompts=prompt_registry),
        clock=clock,
    )
    engine = ConversationEngine(
        identity=identity,
        prompts=prompt_registry,
        structured=generator,
        guard=guard,
        policy=conversation_policy,
        clock=clock,
    )
    processor = EventProcessor(
        db=db, event_store=event_store, dispatcher=dispatcher, snapshots=snapshots,
        arbitrator=arbitrator, committer=committer, runs=runs, failures=failures,
        mode="test", clock=clock,
    )
    service = ConversationService(
        processor=processor,
        engine=engine,
        event_store=event_store,
        conversations=conversations,
        adapter=adapter,
        failures=failures,
        policy=conversation_policy,
        memory=memory_engine,
        clock=clock,
    )
    return service, memory_engine, client, relevant, irrelevant


async def test_the_whole_retrieval_path_fires_on_a_real_turn(
    pipeline, memories, clock  # noqa: F811
) -> None:
    """Phase 2 Definition of Done, in one run."""
    service, memory_engine, client, relevant, irrelevant = pipeline

    before_relevant = memories.get_memory(relevant.memory_id).accessibility
    before_irrelevant = memories.get_memory(irrelevant.memory_id).accessibility

    result = await service.handle_inbound(inbound(clock, text="海の話、覚えてる？"))
    assert result.should_send

    report = result.retrieval
    assert report is not None

    # candidate > 0
    assert len(report.candidates) >= 2
    # relevance judgements > 0
    assert len(report.judgements) == len(report.candidates)
    assert report.relevance_source == "llm"
    assert "memory_relevance" in client.purposes
    # selected recalls > 0
    assert report.selected_ids == (relevant.memory_id,)
    # rejected irrelevant memories > 0
    rejected = [item for item in report.rejected if item.stage == "relevance"]
    assert irrelevant.memory_id in {item.memory_id for item in rejected}

    # It reached the reply prompt.
    assert result.generation.context.includes("relevant_memories")
    assert RELEVANT_SUMMARY in result.generation.context.get("relevant_memories").content

    # Nothing has practised yet: the reply has not been delivered.
    assert memories.get_memory(relevant.memory_id).accessibility == before_relevant

    await service.confirm_sent(result, message_id="msg_1")
    await service.drain_background()

    # practice rows > 0, and only for the memory the reply rests on.
    rows = {row["memory_id"]: row for row in memories.retrievals_in_group(report.group_id)}
    assert rows[relevant.memory_id]["state"] == "used_in_reply"
    assert rows[relevant.memory_id]["used_in_reply"] == 1
    assert rows[relevant.memory_id]["practice_applied"] == 1
    assert rows[irrelevant.memory_id]["practice_applied"] == 0
    assert rows[irrelevant.memory_id]["reject_stage"] == "relevance"

    # DB: the practice actually landed, and only on the used memory. The
    # irrelevant one is *lower* than it started — background maintenance decays
    # it like anything else — which is the opposite of the old behaviour, where
    # merely being searched held it at the ceiling.
    assert memories.get_memory(relevant.memory_id).accessibility > before_relevant
    assert memories.get_memory(relevant.memory_id).recall_count == 1
    assert memories.get_memory(irrelevant.memory_id).accessibility < before_irrelevant
    assert memories.get_memory(irrelevant.memory_id).recall_count == 0
    assert memories.get_memory(irrelevant.memory_id).last_recalled_at is None


async def test_the_inspector_sees_the_same_decision_without_changing_it(
    pipeline, memories, clock  # noqa: F811
) -> None:
    """The last link in the chain: the debug path, and its price of zero."""
    service, memory_engine, _client, relevant, irrelevant = pipeline
    inspector = MemoryInspector(memory_engine.retriever, clock=clock)

    result = await service.handle_inbound(inbound(clock, text="海の話、覚えてる？"))
    await service.confirm_sent(result, message_id="msg_1")
    await service.drain_background()

    settled = {
        memory_id: memories.get_memory(memory_id)
        for memory_id in (relevant.memory_id, irrelevant.memory_id)
    }

    text = await inspector.describe("海の話、覚えてる？", now=clock.now())

    assert relevant.memory_id in text
    assert irrelevant.memory_id in text
    assert "semantic relevance: strong" in text
    assert "semantic relevance: irrelevant" in text
    for memory_id, snapshot in settled.items():
        assert memories.get_memory(memory_id) == snapshot


async def test_a_turn_that_reminds_her_of_nothing_still_replies(
    pipeline, memories, clock  # noqa: F811
) -> None:
    """§2I end to end: zero recalls flows through the conversation pipeline."""
    service, _engine, _client, relevant, irrelevant = pipeline

    result = await service.handle_inbound(inbound(clock, text="そっか"))

    assert result.should_send
    assert result.retrieval is not None
    assert result.retrieval.selected == ()
    assert result.generation.context.includes("relevant_memories") is False
    # And nothing was strengthened by a turn that recalled nothing.
    assert memories.get_memory(irrelevant.memory_id).accessibility == 1.0
