"""Consolidation job mechanics (spec 9.5, 10.2, 12.4, 23.3)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app import ids
from app.consolidation.adaptations import AdaptationEngine
from app.consolidation.drift import DriftMonitor
from app.consolidation.growth import GrowthEngine
from app.consolidation.job import ConsolidationJob
from app.consolidation.policy import GrowthPolicy
from app.consolidation.values import ValueEngine, redistribute
from app.memory.models import EpisodicMemory
from app.orchestrator.processor import EventProcessor
from app.storage.repositories.growth import (
    AdaptationRepository,
    CandidateRepository,
    ConsolidationRepository,
    DriftRepository,
    NarrativeRepository,
    PersonalityRepository,
    ValueRepository,
)
from app.storage.repositories.memory import MemoryRepository
from app.storage.repositories.state import StateRepository
from tests.unit.test_memory import SUMMARY, build_engine, memories, memory_policy  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def growth_policy() -> GrowthPolicy:
    return GrowthPolicy.load(REPO_ROOT / "config" / "policies" / "growth.yaml")


@pytest.fixture
def job(
    db,
    clock,
    growth_policy,
    memories,  # noqa: F811
    memory_policy,  # noqa: F811
    prompt_registry,
    event_store,
    dispatcher,
    snapshots,
    arbitrator,
    committer,
    runs,
    failures,
    state_repo,
) -> ConsolidationJob:
    memory_engine = build_engine(memories, memory_policy, prompt_registry, clock, [SUMMARY])
    candidates = CandidateRepository(db)
    return ConsolidationJob(
        processor=EventProcessor(
            db=db,
            event_store=event_store,
            dispatcher=dispatcher,
            snapshots=snapshots,
            arbitrator=arbitrator,
            committer=committer,
            runs=runs,
            failures=failures,
            clock=clock,
        ),
        event_store=event_store,
        state=state_repo,
        consolidations=ConsolidationRepository(db),
        candidates=candidates,
        narratives=NarrativeRepository(db),
        memories=memories,
        adaptations=AdaptationEngine(
            AdaptationRepository(db), growth_policy.adaptation, clock=clock
        ),
        growth=GrowthEngine(
            candidates=candidates,
            traits=PersonalityRepository(db),
            narratives=NarrativeRepository(db),
            policy=growth_policy,
            clock=clock,
        ),
        values=ValueEngine(
            candidates=candidates,
            values=ValueRepository(db),
            policy=growth_policy,
            clock=clock,
        ),
        drift=DriftMonitor(
            state=state_repo,
            drifts=DriftRepository(db),
            policy=growth_policy.drift,
            clock=clock,
        ),
        memory=memory_engine,
        policy=growth_policy,
        clock=clock,
    )


def remember(memories, clock, *, topics: tuple[str, ...], count: int = 1) -> None:  # noqa: F811
    for index in range(count):
        now = clock.now() + timedelta(minutes=index)
        episode = memories.open_episode(
            conversation_id=None, origin="virtual_life", started_at=now
        )
        memories.insert_memory(
            EpisodicMemory(
                memory_id=ids.new_id("mem"),
                episode_id=episode.episode_id,
                origin="virtual_life",
                summary=f"できごと {index}",
                topics=topics,
                importance=0.5,
                emotional_intensity=0.3,
                accessibility=0.8,
                content_confidence=0.7,
                source_confidence=0.7,
                temporal_confidence=0.7,
                novelty=0.5,
                prediction_error=0.1,
                occurred_at=now,
                created_at=now,
                updated_at=now,
                last_decayed_at=now,
                source_event_ids=(),
            )
        )


# --- scheduling -------------------------------------------------------------
async def test_the_first_consolidation_is_always_due(job) -> None:
    assert job.due() is True


async def test_consolidation_waits_between_runs(job, clock, growth_policy) -> None:
    await job.run()
    assert job.due() is False

    clock.advance(hours=growth_policy.consolidation.min_hours_between_runs + 0.1)
    assert job.due() is True


async def test_a_skipped_run_changes_nothing(job) -> None:
    await job.run()
    result = await job.run()

    assert result.ran is False
    assert result.skipped_reason == "not_due"
    assert result.deep_change_count == 0


# --- narrative and semantic consolidation ----------------------------------
async def test_one_memory_is_not_a_theme(job, memories, clock, growth_policy) -> None:  # noqa: F811
    remember(memories, clock, topics=("海",), count=1)
    result = await job.run()

    assert result.themes_updated == 0


async def test_a_repeated_topic_becomes_a_theme(job, db, memories, clock) -> None:  # noqa: F811
    remember(memories, clock, topics=("海",), count=4)
    result = await job.run()

    themes = NarrativeRepository(db).all()
    assert result.themes_updated >= 1
    assert [theme.theme for theme in themes] == ["海"]
    assert themes[0].supporting_memory_count == 4
    assert 0.0 < themes[0].strength < 1.0


async def test_a_theme_strengthens_but_saturates(job, db, memories, clock) -> None:  # noqa: F811
    remember(memories, clock, topics=("海",), count=4)
    await job.run()
    first = NarrativeRepository(db).by_theme("海").strength

    clock.advance(hours=12)
    remember(memories, clock, topics=("海",), count=8)
    await job.run()
    second = NarrativeRepository(db).by_theme("海").strength

    assert second > first
    assert second < 1.0


async def test_repeated_episodes_become_a_general_fact(job, memories, clock) -> None:  # noqa: F811
    remember(memories, clock, topics=("散歩",), count=3)
    result = await job.run()

    assert result.semantic_facts == 1


# --- the pipeline itself ----------------------------------------------------
async def test_consolidation_records_its_own_run(job, db) -> None:
    result = await job.run()

    record = ConsolidationRepository(db).get(result.record.consolidation_id)
    assert record.status == "completed"
    assert record.ended_at is not None


async def test_the_review_event_is_a_system_event(job) -> None:
    """Consolidation is maintenance, not an experience (spec 8.1)."""
    result = await job.run()

    assert result.outcome is not None
    assert result.outcome.event.category == "system"
    assert result.outcome.event.origin == "system"


async def test_drift_is_measured_every_run(job) -> None:
    result = await job.run()

    assert result.drift is not None
    assert {observation.metric for observation in result.drift.observations} >= {
        "trait_velocity",
        "value_velocity",
    }


# --- value redistribution edge cases ---------------------------------------
def test_redistribute_cannot_push_a_value_below_zero() -> None:
    before = {"a": 0.02, "b": 0.98}
    after = redistribute(before, raise_name="a", step=-0.5)

    assert after["a"] >= 0.0
    assert sum(after.values()) == pytest.approx(1.0)


def test_redistribute_leaves_an_unknown_name_alone() -> None:
    before = {"a": 0.5, "b": 0.5}
    assert redistribute(before, raise_name="missing", step=0.1) == before


def test_redistribute_is_reversible_within_rounding() -> None:
    before = {"a": 0.3, "b": 0.3, "c": 0.4}
    there = redistribute(before, raise_name="a", step=0.05)
    back = redistribute(there, raise_name="a", step=-0.05)

    for name, value in before.items():
        assert back[name] == pytest.approx(value, abs=1e-3)
