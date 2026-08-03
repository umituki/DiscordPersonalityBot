"""E2E contract for spontaneous associative memory (rebuild spec 17.6)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import ids
from app.agency.models import ActionCandidate
from app.bootstrap import Application
from app.memory.events import MEMORY_SPONTANEOUSLY_RECALLED
from app.memory.models import EpisodicMemory
from app.runtime.spontaneous_memory import RECALL_FROM_CUE, SPONTANEOUS_RECALL
from app.storage.repositories.runtime import SpontaneousMemoryCueRepository
from app.world.models import Opportunity
from tests.support import PassingReranker, use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


@pytest.fixture
def owned_config(temp_config):
    return temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )


@pytest.fixture
def application(owned_config, clock):
    built = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


def _store_memory(
    application: Application,
    *,
    summary: str = "海辺を歩いて潮風を感じた",
    accessibility: float = 0.82,
    days_ago: float = 1.0,
) -> EpisodicMemory:
    occurred = application.clock.now() - timedelta(days=days_ago)
    episode = application.memories.open_episode(
        conversation_id=None, origin="virtual_life", started_at=occurred
    )
    application.memories.close_episode(episode.episode_id, ended_at=occurred, reason="test")
    memory = EpisodicMemory(
        memory_id=ids.new_id(ids.MEMORY),
        episode_id=episode.episode_id,
        origin="virtual_life",
        summary=summary,
        topics=("海辺", "散歩", "潮風"),
        importance=0.75,
        emotional_intensity=0.55,
        accessibility=accessibility,
        content_confidence=0.9,
        source_confidence=0.9,
        temporal_confidence=0.9,
        novelty=0.4,
        prediction_error=0.1,
        occurred_at=occurred,
        created_at=occurred,
        updated_at=occurred,
        last_decayed_at=occurred,
    )
    assert application.memories.insert_memory(memory)
    return memory


def _finish_activity(application: Application, clock, *, name: str = "海辺の散歩"):
    activity, _ = application.world.start_activity(
        name=name, kind="leisure", expected_minutes=5
    )
    clock.advance(minutes=6)
    completed, _ = application.world.finish_activity(
        activity.activity_id, outcome="潮風が心地よかった"
    )
    return completed


def _spontaneous_source(application: Application):
    return next(
        source
        for source in application.runtime._registry.sources
        if source.name == "spontaneous_memory"
    )


async def test_spontaneous_recall_slice(application, clock) -> None:
    """A real built application reaches cue -> choice -> recall -> psychology."""
    memory = _store_memory(application)
    application.memory.retriever._reranker = PassingReranker("strong")
    before_memory = application.memories.get_memory(memory.memory_id)
    before_memories = application.memories.memory_count()
    before_episodes = application.memories.episode_count()
    _finish_activity(application, clock)

    tick = await application.runtime.tick(now=clock.now())

    assert SPONTANEOUS_RECALL in tick.opportunity_kinds
    assert tick.chosen_action == RECALL_FROM_CUE
    assert tick.outcome == "acted"
    assert SPONTANEOUS_RECALL not in tick.unclaimed_kinds
    cues = application.spontaneous_memory_cues.recent(limit=10)
    assert len(cues) == 1
    cue = cues[0]
    assert cue.outcome == "recalled"
    assert cue.retrieval_group_id
    assert cue.primary_memory_id == memory.memory_id
    assert cue.event_id

    rows = application.memories.retrievals_in_group(cue.retrieval_group_id)
    selected = [row for row in rows if row["memory_id"] == memory.memory_id]
    assert len(selected) == 1
    assert selected[0]["state"] == "spontaneously_recalled"
    assert selected[0]["practice_applied"] == 1
    after_memory = application.memories.get_memory(memory.memory_id)
    assert after_memory.recall_count == before_memory.recall_count + 1
    assert before_memory.accessibility < after_memory.accessibility <= 0.90

    events = application.event_store.by_types((MEMORY_SPONTANEOUSLY_RECALLED,))
    assert len(events) == 1
    event = events[0]
    assert event.event_id == cue.event_id
    assert event.payload.memory_ids == (memory.memory_id,)
    for subscriber in ("emotion_engine", "mood_engine", "need_engine"):
        assert application.deliveries.status_of(event.event_id, subscriber) == "completed"

    # Processing the recall is not observing/encoding a new episode.
    assert application.memories.memory_count() == before_memories
    assert application.memories.episode_count() == before_episodes


async def test_irrelevant_accessible_memory_produces_no_event(application, clock) -> None:
    memory = _store_memory(application, accessibility=0.90)
    application.memory.retriever._reranker = PassingReranker("irrelevant")
    _finish_activity(application, clock)

    tick = await application.runtime.tick(now=clock.now())

    cue = application.spontaneous_memory_cues.recent(limit=1)[0]
    assert tick.chosen_action == RECALL_FROM_CUE
    assert cue.outcome == "no_recall"
    assert cue.reason == "no_relevant_memory"
    assert application.event_store.by_types((MEMORY_SPONTANEOUSLY_RECALLED,)) == []
    assert application.memories.practices_since(memory.memory_id, clock.now() - timedelta(days=2)) == 0


async def test_relevant_inaccessible_memory_produces_no_event(application, clock) -> None:
    memory = _store_memory(application, accessibility=0.02, days_ago=400)
    application.memory.retriever._reranker = PassingReranker("strong")
    _finish_activity(application, clock)

    await application.runtime.tick(now=clock.now())

    cue = application.spontaneous_memory_cues.recent(limit=1)[0]
    assert cue.reason == "relevant_but_inaccessible"
    assert application.event_store.by_types((MEMORY_SPONTANEOUSLY_RECALLED,)) == []
    assert application.memories.practices_since(memory.memory_id, clock.now() - timedelta(days=500)) == 0


async def test_empty_memory_archive_is_a_normal_no_recall(application, clock) -> None:
    _finish_activity(application, clock)

    tick = await application.runtime.tick(now=clock.now())

    cue = application.spontaneous_memory_cues.recent(limit=1)[0]
    assert tick.chosen_action == RECALL_FROM_CUE
    assert tick.outcome == "idle"
    assert cue.outcome == "no_recall"
    assert cue.reason == "no_candidates"
    assert application.event_store.by_types((MEMORY_SPONTANEOUSLY_RECALLED,)) == []


def test_candidate_generation_does_not_change_memory(application, clock) -> None:
    memory = _store_memory(application)
    _finish_activity(application, clock)
    source = _spontaneous_source(application)
    before = application.memories.get_memory(memory.memory_id)

    opportunities = source.collect(clock.now())
    builder = application.runtime._registry.builder_for(SPONTANEOUS_RECALL)
    assert builder is not None
    candidates = [builder(opportunity, clock.now()) for opportunity in opportunities]

    assert any(candidate is not None for candidate in candidates)
    assert application.memories.get_memory(memory.memory_id) == before
    assert application.memories.practices_since(
        memory.memory_id, clock.now() - timedelta(days=2)
    ) == 0


async def test_another_selected_action_never_retrieves(application, clock) -> None:
    _store_memory(application)
    _finish_activity(application, clock)
    calls: list[str] = []

    class CompetingSource:
        name = "test_competitor"

        def collect(self, now):
            return (Opportunity(kind="test_competitor", urgency=1.0, created_at=now),)

        def next_due(self, now):
            return None

    async def explode(*args, **kwargs):
        raise AssertionError("retrieval ran before its action was selected")

    async def win(candidate, now):
        calls.append(candidate.action)
        return True

    application.memory.associate = explode
    application.runtime._registry.add_source(CompetingSource())
    application.runtime._registry.add_builder(
        "test_competitor",
        lambda opportunity, now: ActionCandidate(
            action="test_win", route="reactive", expected_value=0.99, reason="test"
        ),
    )
    application.runtime._registry.add_handler("test_win", win)

    tick = await application.runtime.tick(now=clock.now())

    assert tick.chosen_action == "test_win"
    assert calls == ["test_win"]
    assert application.spontaneous_memory_cues.recent(limit=1)[0].consumed_at is None


async def test_user_turn_defers_without_retrieval(application, clock) -> None:
    _store_memory(application)
    _finish_activity(application, clock)
    calls: list[tuple[str, ...]] = []
    original = application.memory.associate

    async def observe(cues, **kwargs):
        calls.append(tuple(cues))
        return await original(cues, **kwargs)

    application.memory.associate = observe
    with application.runtime.user_turn():
        tick = await application.runtime.tick(now=clock.now())

    assert tick.outcome == "deferred"
    assert tick.deferred_reason == "user_turn_in_flight"
    assert calls == []


def test_unchanged_cue_is_suppressed_and_survives_restart(owned_config, clock) -> None:
    first = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(first)
    _finish_activity(first, clock)
    source = _spontaneous_source(first)
    assert len(source.collect(clock.now())) == 1
    assert source.collect(clock.now()) == ()
    first.db.close()

    second = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(second)
    try:
        assert _spontaneous_source(second).collect(clock.now()) == ()
        assert second.spontaneous_memory_cues.count() == 1
    finally:
        second.db.close()


async def test_spontaneous_debug_is_read_only(application, clock) -> None:
    memory = _store_memory(application)
    _finish_activity(application, clock)
    _spontaneous_source(application).collect(clock.now())
    before = application.memories.get_memory(memory.memory_id)
    before_practice = application.memories.practices_since(
        memory.memory_id, clock.now() - timedelta(days=2)
    )

    outcome = await application.admin_router.route(
        text="!yui memory spontaneous", author_id=OWNER, channel_id=CHANNEL
    )

    assert outcome.result is not None and not outcome.result.failed
    rows = [row for section in outcome.result.sections for row in section.rows]
    assert rows and rows[0]["outcome"] == "offered"
    assert application.memories.get_memory(memory.memory_id) == before
    assert application.memories.practices_since(
        memory.memory_id, clock.now() - timedelta(days=2)
    ) == before_practice


def test_spontaneous_kind_is_fully_claimed(application) -> None:
    assert SPONTANEOUS_RECALL in application.runtime._registry.kinds
    assert RECALL_FROM_CUE in application.runtime._registry.actions


def test_repository_offer_is_atomic_and_consumption_is_final(db, clock) -> None:
    cues = SpontaneousMemoryCueRepository(db)
    first = cues.offer(
        cue_type="activity",
        source_kind="activity",
        source_id="act_one",
        detail="walk",
        cues=("walk",),
        salience=0.5,
        now=clock.now(),
        cooldown=timedelta(hours=1),
    )
    assert first is not None
    assert cues.offer(
        cue_type="activity",
        source_kind="activity",
        source_id="act_one",
        detail="walk",
        cues=("walk",),
        salience=0.5,
        now=clock.now(),
        cooldown=timedelta(hours=1),
    ) is None
    assert cues.complete(first.cue_id, outcome="no_recall", reason="empty", now=clock.now())
    clock.advance(hours=2)
    assert cues.offer(
        cue_type="activity",
        source_kind="activity",
        source_id="act_one",
        detail="walk",
        cues=("walk",),
        salience=0.5,
        now=clock.now(),
        cooldown=timedelta(hours=1),
    ) is None


def test_spontaneous_practice_transition_is_exactly_once(application) -> None:
    from app.memory.recall_mode import RecallMode
    from app.memory.recall_models import RecalledMemory, RetrievalReport

    memory = _store_memory(application, accessibility=0.5)
    report = RetrievalReport(
        query="海辺",
        mode=RecallMode.ASSOCIATIVE,
        selected=(RecalledMemory(memory=memory, relevance="strong", availability=0.8),),
        group_id="ret_test_spontaneous_once",
        retrieved_at=application.clock.now(),
    )
    application.memories.record_retrieval(
        memory_id=memory.memory_id,
        query=report.query,
        score=0.8,
        rank=0,
        now=application.clock.now(),
        used=True,
        group_id=report.group_id,
        mode=report.mode.value,
        state="selected",
    )

    first = application.memory.mark_spontaneously_recalled(report)
    second = application.memory.mark_spontaneously_recalled(report)

    assert first == (memory.memory_id,)
    assert second == ()
    rows = application.memories.retrievals_in_group(report.group_id)
    assert len(rows) == 1 and rows[0]["practice_applied"] == 1
    assert application.memories.get_memory(memory.memory_id).recall_count == 1
