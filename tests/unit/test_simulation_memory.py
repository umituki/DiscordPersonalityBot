"""A simulated life is remembered selectively (patch spec 15).

Nineteen simulated years produced zero memories. Two reasons, both fixed here:
the only episode material source read the conversation projection, so every
simulated episode was empty and discarded; and the segmentation rules were a
conversation's, so experiences a month apart never joined into an episode at
all.

What must NOT happen is the overcorrection — writing simulated experiences
straight into the memory table (prohibition 3) or keeping all of them. The
Encoding Gate decides, and forgetting runs on simulated time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.memory.engine import MemoryEngine
from app.memory.material import (
    CompositeMaterialSource,
    ConversationEpisodeSource,
    EpisodeMaterial,
    SimulationEpisodeSource,
    VirtualLifeEpisodeSource,
)
from app.memory.models import Episode
from app.memory.policy import MemoryPolicy
from app.memory.segmentation import EpisodeSegmenter
from app.llm.structured import StructuredGenerator
from app.simulation.events import SIMULATED_EXPERIENCE, SimulatedExperiencePayload
from app.storage.repositories.conversations import ConversationRepository
from app.storage.repositories.memory import MemoryRepository
from tests.unit.test_llm_structured import ScriptedClient

REPO_ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2003, 6, 1, 9, 0, tzinfo=timezone.utc)

SUMMARY = (
    '{"summary": "川沿いをよく歩いていた時期のこと。", "topics": ["散歩"], '
    '"novelty": 0.7, "felt_significance": 0.6}'
)


@pytest.fixture
def memory_policy() -> MemoryPolicy:
    return MemoryPolicy.load(REPO_ROOT / "config" / "policies" / "memory.yaml")


@pytest.fixture
def memories(db) -> MemoryRepository:
    return MemoryRepository(db)


@pytest.fixture
def material(db, event_store):
    return CompositeMaterialSource(
        (
            ConversationEpisodeSource(ConversationRepository(db)),
            SimulationEpisodeSource(event_store),
            VirtualLifeEpisodeSource(event_store),
        )
    )


@pytest.fixture
def engine(memories, memory_policy, prompt_registry, material, clock):
    def build(script: list) -> MemoryEngine:
        return MemoryEngine(
            repository=memories,
            policy=memory_policy,
            structured=StructuredGenerator(
                ScriptedClient(script), prompts=prompt_registry, clock=clock, max_attempts=1
            ),
            prompts=prompt_registry,
            material=material,
            clock=clock,
        )

    return build


def simulated(make_event, event_store, *, summary: str, at: datetime):
    event = make_event(
        event_type=SIMULATED_EXPERIENCE,
        category="internal",
        origin="simulated_past",
        actor_type="yui",
        occurred_at=at,
        payload=SimulatedExperiencePayload(
            block_id="blk_1",
            experience_class="minor",
            valence=0.4,
            summary=summary,
            text=summary,
            felt_significance=0.6,
        ),
    )
    event_store.append(event)
    return event


def episode_of(memories, origin: str) -> Episode:
    return [
        episode
        for status in ("open", "closed", "encoded", "discarded")
        for episode in memories.episodes_with_status(status)
        if episode.origin == origin
    ][0]


# --- 15.2 there is material to read at all ----------------------------------
def test_a_simulated_episode_has_material(material, memories, make_event, event_store) -> None:
    events = [
        simulated(make_event, event_store, summary="川沿いを歩いた", at=MOMENT),
        simulated(make_event, event_store, summary="友達と話した", at=MOMENT),
    ]
    episode = memories.open_episode(
        conversation_id=None,
        origin="simulated_past",
        started_at=MOMENT,
        first_event_id=events[0].event_id,
    )
    episode = memories.append_to_episode(episode.episode_id, events[1].event_id)

    read = material.material_for(episode)

    assert not read.is_empty
    assert "川沿いを歩いた" in read.text
    assert "友達と話した" in read.text
    assert read.kind == "simulated_past"


def test_a_lived_day_has_no_user_share(material, memories, make_event, event_store) -> None:
    """Nobody spoke to her, so the gate must not read a USER-directed ratio."""
    event = simulated(make_event, event_store, summary="ひとりで本を読んだ", at=MOMENT)
    episode = memories.open_episode(
        conversation_id=None,
        origin="simulated_past",
        started_at=MOMENT,
        first_event_id=event.event_id,
    )

    assert material.material_for(episode).user_turn_count == 0


def test_an_origin_without_a_source_is_empty_not_an_error(memories, make_event) -> None:
    source = CompositeMaterialSource(())
    episode = memories.open_episode(
        conversation_id=None, origin="simulated_past", started_at=MOMENT
    )
    assert source.material_for(episode).is_empty


# --- 15.1 segmentation at the life's own scale ------------------------------
def test_experiences_a_month_apart_are_one_stretch(memory_policy, memories) -> None:
    segmenter = EpisodeSegmenter(memory_policy.segmentation)
    episode = memories.open_episode(
        conversation_id=None, origin="simulated_past", started_at=MOMENT
    )

    decision = segmenter.decide(episode, now=MOMENT, next_event_at=MOMENT + timedelta(days=30))

    assert decision.close is False


def test_a_conversation_still_ends_after_half_an_hour(memory_policy, memories) -> None:
    segmenter = EpisodeSegmenter(memory_policy.segmentation)
    episode = memories.open_episode(
        conversation_id="conv_1", origin="real_discord", started_at=MOMENT
    )

    decision = segmenter.decide(episode, now=MOMENT, next_event_at=MOMENT + timedelta(hours=1))

    assert decision.close is True


def test_a_long_enough_simulated_gap_still_ends_the_stretch(memory_policy, memories) -> None:
    segmenter = EpisodeSegmenter(memory_policy.segmentation)
    episode = memories.open_episode(
        conversation_id=None, origin="simulated_past", started_at=MOMENT
    )

    decision = segmenter.decide(episode, now=MOMENT, next_event_at=MOMENT + timedelta(days=200))

    assert decision.close is True
    assert decision.reason == "silence_gap"


def test_a_simulated_stream_does_not_absorb_a_virtual_life_day(
    engine, memories, make_event, event_store
) -> None:
    """Both have no conversation id; they are still separate lives."""
    memory = engine([])
    lived = make_event(origin="virtual_life", category="world", actor_type="yui")
    event_store.append(lived)
    memory.observe(
        simulated(make_event, event_store, summary="川沿いを歩いた", at=MOMENT),
        now=MOMENT,
    )
    memory.observe(lived, now=MOMENT)

    origins = {
        episode.origin for episode in memories.episodes_with_status("open")
    }
    assert origins == {"simulated_past", "virtual_life"}


# --- 15.1/15.3 the gate decides, and it can refuse --------------------------
async def test_a_simulated_stretch_can_become_a_memory(
    engine, memories, make_event, event_store
) -> None:
    memory = engine([SUMMARY])
    for offset in (0, 30):
        memory.observe(
            simulated(
                make_event,
                event_store,
                summary=f"川沿いを歩いた（{offset}）",
                at=MOMENT + timedelta(days=offset),
            ),
            now=MOMENT + timedelta(days=offset),
        )

    later = MOMENT + timedelta(days=400)
    memory.close_due_episodes(now=later)
    results = await memory.encode_pending(now=later)

    assert [result.encoded for result in results] == [True]
    stored = results[0].memory
    assert stored.origin == "simulated_past"
    assert stored.created_at == later, "a simulated memory is created in simulated time"


async def test_a_stretch_the_gate_refuses_is_not_remembered(
    engine, memories, make_event, event_store
) -> None:
    """Patch spec 15.1: ``全Event記憶化は禁止``."""
    dull = (
        '{"summary": "とくに何もない日が続いた。", "topics": [], '
        '"novelty": 0.0, "felt_significance": 0.0}'
    )
    memory = engine([dull])
    for offset in (0, 30):
        memory.observe(
            simulated(make_event, event_store, summary="ふつうの日", at=MOMENT + timedelta(days=offset)),
            now=MOMENT + timedelta(days=offset),
        )

    later = MOMENT + timedelta(days=400)
    memory.close_due_episodes(now=later)
    results = await memory.encode_pending(now=later)

    assert results[0].encoded is False
    assert results[0].reason == "below_threshold"
    assert memories.memory_count() == 0


async def test_a_single_experience_is_not_an_episode(
    engine, memories, make_event, event_store
) -> None:
    memory = engine([])
    memory.observe(
        simulated(make_event, event_store, summary="一度きり", at=MOMENT), now=MOMENT
    )

    later = MOMENT + timedelta(days=400)
    memory.close_due_episodes(now=later)
    results = await memory.encode_pending(now=later)

    assert results[0].reason == "too_small"
    assert memories.memory_count() == 0


# --- 15.3 nineteen years are not equally accessible -------------------------
async def test_an_old_simulated_memory_has_decayed_by_the_end(
    engine, memories, make_event, event_store
) -> None:
    """Patch spec 15.3: ``19年間すべてを同じaccessibilityで保持しない``."""
    memory = engine([SUMMARY])
    for offset in (0, 30):
        memory.observe(
            simulated(make_event, event_store, summary=f"むかしのこと（{offset}）",
                      at=MOMENT + timedelta(days=offset)),
            now=MOMENT + timedelta(days=offset),
        )
    encoded_at = MOMENT + timedelta(days=200)
    memory.close_due_episodes(now=encoded_at)
    stored = (await memory.encode_pending(now=encoded_at))[0].memory
    fresh = stored.accessibility

    memory.apply_forgetting(now=encoded_at + timedelta(days=365 * 10))

    aged = memories.get_memory(stored.memory_id)
    assert aged.accessibility < fresh
    assert aged.status == "active", "forgetting lowers access; it deletes nothing (spec 10.5)"


async def test_forgetting_on_wall_time_would_not_have_aged_it(
    engine, memories, make_event, event_store, clock
) -> None:
    """The regression: decaying against the machine's clock ages nothing."""
    memory = engine([SUMMARY])
    for offset in (0, 30):
        memory.observe(
            simulated(make_event, event_store, summary=f"むかしのこと（{offset}）",
                      at=MOMENT + timedelta(days=offset)),
            now=MOMENT + timedelta(days=offset),
        )
    encoded_at = MOMENT + timedelta(days=200)
    memory.close_due_episodes(now=encoded_at)
    stored = (await memory.encode_pending(now=encoded_at))[0].memory

    # The wall clock is fixed at 2026 in tests, and the memory is stamped 2003;
    # decaying against `now=clock.now()` is not what the simulation asks for.
    memory.apply_forgetting(now=encoded_at)

    assert memories.get_memory(stored.memory_id).accessibility == stored.accessibility


# --- nothing is written straight into memory (prohibition 3) ----------------
def test_the_simulation_engine_never_inserts_a_memory() -> None:
    source = (REPO_ROOT / "app" / "simulation").rglob("*.py")
    offenders = [
        str(path.name)
        for path in source
        if "insert_memory" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "past simulation must reach memory through the Encoding Gate "
        "(patch spec 15.3, prohibition 3)"
    )


def test_the_conversation_source_still_reads_conversations(
    db, memories, make_event, event_store
) -> None:
    """The generalisation must not have broken the original source."""
    conversations = ConversationRepository(db)
    conversation = conversations.ensure_conversation(
        channel_id="1", channel_type="direct_message", now=MOMENT
    )
    said = make_event(text="やっほー", occurred_at=MOMENT)
    event_store.append(said)
    conversations.record_turn(
        conversation_id=conversation.conversation_id,
        event_id=said.event_id,
        speaker="user",
        author_id="1",
        content="やっほー",
        occurred_at=MOMENT,
    )
    episode = memories.open_episode(
        conversation_id=conversation.conversation_id,
        origin="real_discord",
        started_at=MOMENT,
        first_event_id=said.event_id,
    )

    read = ConversationEpisodeSource(conversations).material_for(episode)

    assert read.kind == "conversation"
    assert read.user_turn_count == 1
    assert "やっほー" in read.text


def test_static_material_is_still_usable_for_tests() -> None:
    from app.memory.material import StaticMaterialSource

    source = StaticMaterialSource(EpisodeMaterial(text="x", turn_count=1))
    assert source.material_for(None).text == "x"  # type: ignore[arg-type]
