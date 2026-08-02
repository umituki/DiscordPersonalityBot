"""Episode segmentation, encoding, forgetting and retrieval (spec 10)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.memory.accessibility import decayed_accessibility, practised_accessibility
from app.memory.encoding import (
    EncodingGate,
    EncodingSignals,
    substance_of,
    user_directed_ratio,
)
from app.memory.engine import MemoryEngine
from app.memory.policy import MemoryPolicy
from app.memory.retrieval import MemoryRetriever, build_match_query
from app.memory.segmentation import EpisodeSegmenter
from app.memory.material import EpisodeMaterial, StaticMaterialSource
from app.llm.structured import StructuredGenerator
from app.storage.repositories.memory import MemoryRepository
from tests.support import PassingReranker
from tests.unit.test_llm_structured import ScriptedClient

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def memory_policy() -> MemoryPolicy:
    return MemoryPolicy.load(REPO_ROOT / "config" / "policies" / "memory.yaml")


@pytest.fixture
def memories(db) -> MemoryRepository:
    return MemoryRepository(db)


def build_engine(
    memories, memory_policy, prompt_registry, clock, script, transcript=None,
    reranker=None,
):
    """An engine with Stage 2 held constant.

    These tests are about encoding, suppression, origin and practice, so the
    relevance judgement is a double that passes everything (Phase 2 §2E). The
    gate itself is tested in tests/invariants/test_memory_retrieval.py, against
    the real reranker.
    """
    generator = StructuredGenerator(
        ScriptedClient(script), prompts=prompt_registry, clock=clock, max_attempts=1
    )
    return MemoryEngine(
        repository=memories,
        policy=memory_policy,
        structured=generator,
        prompts=prompt_registry,
        material=StaticMaterialSource(
            transcript
            or EpisodeMaterial(text="USER: 海に行った話\nYUI: いいね", turn_count=2, user_turn_count=1)
        ),
        reranker=PassingReranker() if reranker is None else reranker,
        clock=clock,
    )


SUMMARY = (
    '{"summary": "ユーザーが海に行った話をしてくれた。", "topics": ["海", "旅行"], '
    '"novelty": 0.7, "felt_significance": 0.6}'
)


# --- segmentation -----------------------------------------------------------


def test_silence_closes_an_episode(memories, memory_policy, clock, make_event) -> None:
    segmenter = EpisodeSegmenter(memory_policy.segmentation)
    episode = memories.open_episode(
        conversation_id="conv_1", origin="real_discord", started_at=clock.now()
    )

    assert segmenter.decide(episode, now=clock.now()).close is False

    later = clock.now() + timedelta(minutes=memory_policy.segmentation.max_gap_minutes + 1)
    decision = segmenter.decide(episode, now=later)
    assert decision.close
    assert decision.reason == "silence_gap"


def test_a_different_conversation_closes_the_episode(memories, memory_policy, clock) -> None:
    segmenter = EpisodeSegmenter(memory_policy.segmentation)
    episode = memories.open_episode(
        conversation_id="conv_1", origin="real_discord", started_at=clock.now()
    )
    decision = segmenter.decide(episode, now=clock.now(), next_conversation_id="conv_2")
    assert decision.reason == "conversation_changed"


def test_events_accumulate_into_one_episode(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [])
    first, second = make_event(), make_event()
    event_store.append_all([first, second])

    engine.observe(first, conversation_id="conv_1")
    episode = engine.observe(second, conversation_id="conv_1")

    assert episode.event_count == 2
    assert memories.episode_count() == 1


def test_a_gap_starts_a_new_episode(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [])
    first = make_event()
    event_store.append(first)
    engine.observe(first, conversation_id="conv_1")

    clock.advance(seconds=(memory_policy.segmentation.max_gap_minutes + 5) * 60)
    later = make_event()
    event_store.append(later)
    engine.observe(later, conversation_id="conv_1")

    assert memories.episode_count() == 2
    assert len(memories.episodes_with_status("closed")) == 1


# --- the encoding gate ------------------------------------------------------


def test_gate_encodes_a_significant_episode(memory_policy) -> None:
    gate = EncodingGate(memory_policy.encoding)
    decision = gate.evaluate(
        EncodingSignals(novelty=0.8, emotional_intensity=0.6, user_directed=1.0, substance=0.8)
    )
    assert decision.encode
    assert decision.importance > memory_policy.encoding.encode_threshold


def test_gate_rejects_an_unremarkable_episode(memory_policy) -> None:
    gate = EncodingGate(memory_policy.encoding)
    decision = gate.evaluate(EncodingSignals())
    assert decision.encode is False
    assert decision.reason == "below_threshold"


def test_gate_importance_is_bounded(memory_policy) -> None:
    gate = EncodingGate(memory_policy.encoding)
    decision = gate.evaluate(
        EncodingSignals(
            novelty=5.0, emotional_intensity=5.0, prediction_error=5.0,
            user_directed=5.0, substance=5.0,
        )
    )
    assert 0.0 <= decision.importance <= 1.0


def test_substance_and_direction_helpers() -> None:
    assert substance_of("") == 0.0
    assert 0 < substance_of("a" * 100) < 1.0
    assert substance_of("a" * 5000) == 1.0
    assert user_directed_ratio(1, 2) == 0.5
    assert user_directed_ratio(0, 0) == 0.0


async def test_encoding_stores_one_memory_per_episode(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    for _ in range(2):
        event = make_event()
        event_store.append(event)
        engine.observe(event, conversation_id="conv_1")

    clock.advance(seconds=(memory_policy.segmentation.max_gap_minutes + 1) * 60)
    engine.close_due_episodes()
    results = await engine.encode_pending()

    assert len(results) == 1
    memory = results[0].memory
    assert memory is not None
    assert memory.summary.startswith("ユーザーが海に行った")
    assert memory.topics == ("海", "旅行")
    assert memory.accessibility == memory_policy.encoding.initial_accessibility
    assert memories.memory_count() == 1


async def test_encoding_is_idempotent(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    for _ in range(2):
        event = make_event()
        event_store.append(event)
        engine.observe(event, conversation_id="conv_1")
    clock.advance(seconds=3600)
    episodes = engine.close_due_episodes()

    first = await engine.encode_episode(episodes[0])
    second = await engine.encode_episode(memories.get_episode(episodes[0].episode_id))

    assert first.encoded
    assert second.reason == "already_encoded"
    assert memories.memory_count() == 1


async def test_tiny_episode_is_not_remembered(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    """Spec 10.4: message-level storage is exactly what must not happen."""
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    event = make_event()
    event_store.append(event)
    engine.observe(event, conversation_id="conv_1")
    clock.advance(seconds=3600)
    episodes = engine.close_due_episodes()

    result = await engine.encode_episode(episodes[0])

    assert result.reason == "too_small"
    assert memories.memory_count() == 0


async def test_failed_summary_does_not_invent_a_memory(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, ["not json"])
    for _ in range(2):
        event = make_event()
        event_store.append(event)
        engine.observe(event, conversation_id="conv_1")
    clock.advance(seconds=3600)
    episodes = engine.close_due_episodes()

    result = await engine.encode_episode(episodes[0])

    assert result.reason == "summary_unavailable"
    assert memories.memory_count() == 0
    # The episode stays closed so it can be retried later.
    assert memories.get_episode(episodes[0].episode_id).status == "closed"


# --- accessibility ----------------------------------------------------------


def test_decay_lowers_accessibility_towards_a_floor(memory_policy) -> None:
    policy = memory_policy.forgetting
    value = 0.8
    for _ in range(20):
        value = decayed_accessibility(
            accessibility=value, importance=0.2, elapsed_days=7, policy=policy
        )
    assert value == pytest.approx(policy.min_accessibility, abs=1e-6)
    assert value > 0.0


def test_important_memories_decay_more_slowly(memory_policy) -> None:
    policy = memory_policy.forgetting
    trivial = decayed_accessibility(
        accessibility=0.8, importance=0.0, elapsed_days=30, policy=policy
    )
    treasured = decayed_accessibility(
        accessibility=0.8, importance=1.0, elapsed_days=30, policy=policy
    )
    assert treasured > trivial


def test_practice_has_diminishing_returns(memory_policy) -> None:
    policy = memory_policy.practice
    first = practised_accessibility(accessibility=0.3, recent_practice_count=0, policy=policy)
    second = practised_accessibility(accessibility=0.3, recent_practice_count=1, policy=policy)
    third = practised_accessibility(accessibility=0.3, recent_practice_count=2, policy=policy)

    assert first > second > third > 0.3
    assert (first - 0.3) > 2 * (third - 0.3)


def test_practice_cannot_exceed_the_ceiling(memory_policy) -> None:
    value = 0.99
    for index in range(10):
        value = practised_accessibility(
            accessibility=value, recent_practice_count=0, policy=memory_policy.practice
        )
    assert value <= memory_policy.practice.max_accessibility


# --- retrieval --------------------------------------------------------------


def test_match_query_handles_japanese_and_latin() -> None:
    japanese = build_match_query("きのう海に行った話")
    assert japanese is not None and '"きのう"' in japanese
    latin = build_match_query("remember the sea trip")
    assert latin is not None and '"remember"' in latin
    assert build_match_query("あ") is None
    assert build_match_query("   ") is None


async def test_retrieval_finds_a_related_memory(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    for _ in range(2):
        event = make_event()
        event_store.append(event)
        engine.observe(event, conversation_id="conv_1")
    clock.advance(seconds=3600)
    engine.close_due_episodes()
    await engine.encode_pending()

    report = await engine.recall("海に行った話のつづき", now=clock.now())

    assert len(report.selected) == 1
    assert "海" in report.selected[0].memory.summary
    # It was found by content, not merely by being recent (§2B).
    assert "fts" in report.selected[0].reasons
    assert report.selected[0].relevance in ("relevant", "strong")


async def test_recall_strengthens_and_counts(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    for _ in range(2):
        event = make_event()
        event_store.append(event)
        engine.observe(event, conversation_id="conv_1")
    clock.advance(seconds=3600)
    engine.close_due_episodes()
    result = (await engine.encode_pending())[0]
    memory_id = result.memory.memory_id

    engine.apply_forgetting(now=clock.now() + timedelta(days=10))
    faded = memories.get_memory(memory_id).accessibility

    # Phase 2 §2K: being selected into context is not remembering. A
    # deliberate lookup is, so this asks in the mode that means she is
    # actually trying to remember.
    from app.memory.recall_mode import RecallMode

    await engine.recall(
        "海", mode=RecallMode.REFLECTIVE, now=clock.now() + timedelta(days=10)
    )
    refreshed = memories.get_memory(memory_id)

    assert refreshed.accessibility > faded
    assert refreshed.recall_count == 1
    assert refreshed.last_recalled_at is not None


async def test_retriever_reads_only_active_memories(memories, memory_policy, clock) -> None:
    retriever = MemoryRetriever(memories, memory_policy.retrieval, clock=clock)
    report = await retriever.retrieve("なんでもいい", now=clock.now())
    assert report.selected == ()
    assert report.candidates == ()


# --- reconstruction and semantic memory -------------------------------------


async def test_revision_keeps_the_previous_version(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    for _ in range(2):
        event = make_event()
        event_store.append(event)
        engine.observe(event, conversation_id="conv_1")
    clock.advance(seconds=3600)
    engine.close_due_episodes()
    memory = (await engine.encode_pending())[0].memory

    engine.revise(
        memory.memory_id,
        new_summary="海ではなく川の話だったかもしれない。",
        reason_code="later_correction",
        content_confidence=0.5,
    )

    revised = memories.get_memory(memory.memory_id)
    history = memories.revisions(memory.memory_id)
    assert revised.summary.startswith("海ではなく川")
    assert revised.revision_count == 1
    assert revised.content_confidence == 0.5
    assert history[0]["previous_summary"] == memory.summary
    # The source events are untouched by a revision (spec 10.6).
    assert event_store.count() == 2


def test_semantic_memory_gains_confidence_with_support(
    memories, memory_policy, prompt_registry, clock
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [])
    statement = "ユーザーは朝が弱い"

    first = engine.note_semantic(statement, origin="real_discord", topics=["生活"])
    engine.note_semantic(statement, origin="real_discord")
    third = engine.note_semantic(statement, origin="real_discord")

    assert first.support_count == 1
    assert third.support_count == 3
    assert third.confidence > first.confidence
    assert memories.semantic_count() == 1
    assert [fact.statement for fact in engine.promoted_semantic()] == [statement]


def test_semantic_confidence_is_capped(memories, memory_policy, prompt_registry, clock) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [])
    for _ in range(30):
        fact = engine.note_semantic("繰り返された話", origin="real_discord")
    assert fact.confidence <= memory_policy.semantic.max_confidence
