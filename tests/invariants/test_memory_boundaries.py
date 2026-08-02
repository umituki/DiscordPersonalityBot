"""INVARIANT: memory is selective, subjective and separate from the archive.

Spec 34.2-1 heads the critical list: ``Objective Archive から forgotten memory
を勝手に回答しない``. Everything here defends that, plus the forgetting rules of
spec 10.5 and the reconstruction rules of spec 10.6.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from app.memory.engine import MemoryEngine
from app.memory.retrieval import MemoryRetriever
from tests.unit.test_memory import (  # noqa: F401 - shared fixtures
    SUMMARY,
    build_engine,
    memories,
    memory_policy,
)

pytestmark = pytest.mark.invariant

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


async def encode_one(engine, event_store, make_event, clock, *, count: int = 2):
    for _ in range(count):
        event = make_event()
        event_store.append(event)
        engine.observe(event, conversation_id="conv_1")
    clock.advance(seconds=3600)
    engine.close_due_episodes()
    results = await engine.encode_pending()
    return results[0].memory


# --- archive vs memory ------------------------------------------------------


def test_retrieval_module_never_reads_the_event_store() -> None:
    """Spec 10.1 / 37: the archive is not a recall fallback."""
    source = (APP_ROOT / "memory" / "retrieval.py").read_text(encoding="utf-8")
    assert "event_store" not in source
    assert "EventStore" not in source
    assert not re.search(r"from app\.events", source)


def test_retriever_has_no_access_to_events(memories, memory_policy, clock) -> None:
    retriever = MemoryRetriever(memories, memory_policy.retrieval, clock=clock)
    assert not hasattr(retriever, "_events")
    assert not hasattr(retriever, "_event_store")


async def test_unencoded_events_are_not_recallable(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    """An event in the archive that never became a memory cannot be recalled."""
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    event = make_event(payload=None)
    event_store.append(event)
    engine.observe(event, conversation_id="conv_1")
    clock.advance(seconds=3600)
    engine.close_due_episodes()
    await engine.encode_pending()  # single-event episode is discarded

    assert event_store.count() == 1
    assert memories.memory_count() == 0
    assert engine.recall("なにか", now=clock.now()) == ()


async def test_suppressed_memory_disappears_from_recall_but_not_from_disk(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    """Spec 30: suppression is not deletion, and recall must respect it."""
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    memory = await encode_one(engine, event_store, make_event, clock)

    assert engine.recall("海に行った話", now=clock.now())

    engine.suppress(memory.memory_id)

    assert engine.recall("海に行った話", now=clock.now()) == ()
    stored = memories.get_memory(memory.memory_id)
    assert stored is not None
    assert stored.status == "suppressed"
    assert stored.summary == memory.summary


async def test_restoring_a_suppressed_memory_brings_it_back(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    memory = await encode_one(engine, event_store, make_event, clock)
    engine.suppress(memory.memory_id)
    engine.restore(memory.memory_id)
    assert engine.recall("海に行った話", now=clock.now())


# --- forgetting -------------------------------------------------------------


async def test_forgetting_never_deletes(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    """Spec 10.5: forgetting is a loss of accessibility, not a delete."""
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    memory = await encode_one(engine, event_store, make_event, clock)

    for years in range(1, 6):
        engine.apply_forgetting(now=clock.now() + timedelta(days=365 * years))

    faded = memories.get_memory(memory.memory_id)
    assert faded is not None
    assert memories.memory_count() == 1
    assert faded.accessibility <= memory_policy.forgetting.min_accessibility + 1e-9
    assert faded.accessibility > 0.0
    # Importance is untouched by forgetting: it mattered, it is just far away.
    assert faded.importance == memory.importance


async def test_accessibility_and_importance_are_independent(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    memory = await encode_one(engine, event_store, make_event, clock)

    engine.apply_forgetting(now=clock.now() + timedelta(days=60))
    faded = memories.get_memory(memory.memory_id)

    assert faded.accessibility < memory.accessibility
    assert faded.importance == memory.importance


async def test_rumination_cannot_pin_a_memory_at_the_ceiling(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    """Spec 10.5: strengthening by repetition has diminishing returns."""
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    memory = await encode_one(engine, event_store, make_event, clock)
    engine.apply_forgetting(now=clock.now() + timedelta(days=30))
    start = memories.get_memory(memory.memory_id).accessibility

    gains = []
    for _ in range(4):
        before = memories.get_memory(memory.memory_id).accessibility
        engine.recall("海に行った話", now=clock.now() + timedelta(days=30))
        after = memories.get_memory(memory.memory_id).accessibility
        gains.append(after - before)

    assert gains[0] > gains[-1]
    assert memories.get_memory(memory.memory_id).accessibility <= 1.0
    assert memories.get_memory(memory.memory_id).accessibility > start


# --- reconstruction ---------------------------------------------------------


async def test_revision_does_not_touch_the_source_events(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    """Spec 10.6: recall may reinterpret; the original event is never destroyed."""
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    memory = await encode_one(engine, event_store, make_event, clock)
    source_ids = memory.source_event_ids
    originals = [event_store.get(event_id) for event_id in source_ids]

    engine.revise(
        memory.memory_id, new_summary="別の話だった気がする。", reason_code="reinterpretation"
    )

    for original, event_id in zip(originals, source_ids, strict=True):
        assert event_store.get(event_id).payload == original.payload
    revisions = memories.revisions(memory.memory_id)
    assert len(revisions) == 1
    assert revisions[0]["previous_summary"] == memory.summary


# --- separation of origins --------------------------------------------------


async def test_memories_keep_the_origin_of_their_experience(
    memories, memory_policy, prompt_registry, clock, make_event, event_store
) -> None:
    """Spec 2.10: a simulated-past memory is never silently real history."""
    engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    for _ in range(2):
        event = make_event(origin="simulated_past", actor_type="npc")
        event_store.append(event)
        engine.observe(event, conversation_id=None)
    # A simulated stretch is bounded in simulated time (patch spec 15.1), so an
    # hour of silence does not end one — a hundred days does.
    clock.advance(seconds=100 * 86400)
    engine.close_due_episodes()
    memory = (await engine.encode_pending())[0].memory

    assert memory.origin == "simulated_past"
    assert not memory.is_from_real_history
    real_only = engine.recall(
        "海に行った話", now=clock.now(), origins=("real_discord",)
    )
    assert real_only == ()


async def test_encoding_survives_a_restart(
    temp_config, clock, make_event
) -> None:
    """Spec 41: memory is continuous across restarts."""
    from app.bootstrap import Application

    first = Application.build(temp_config, clock=clock, configure_logs=False)
    event = make_event()
    first.event_store.append(event)
    first.memory.observe(event, conversation_id="conv_1")
    second_event = make_event()
    first.event_store.append(second_event)
    first.memory.observe(second_event, conversation_id="conv_1")
    episode_id = first.memories.open_episode_for("conv_1").episode_id
    first.db.close()

    reopened = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        episode = reopened.memories.get_episode(episode_id)
        assert episode is not None
        assert episode.event_count == 2
    finally:
        reopened.db.close()
