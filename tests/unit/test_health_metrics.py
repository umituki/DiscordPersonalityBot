"""What the health audits read (patch spec 17).

Each metric is asserted against a database it can see, so a query that silently
counts the wrong thing shows up here rather than as a Genesis that boots when
it should not have.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.simulation.events import SIMULATED_EXPERIENCE, SimulatedExperiencePayload
from app.storage.repositories.health import HealthRepository

MOMENT = datetime(2003, 6, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def health(db) -> HealthRepository:
    return HealthRepository(db)


def experience(make_event, event_store, *, at: datetime = MOMENT):
    event = make_event(
        event_type=SIMULATED_EXPERIENCE,
        category="internal",
        origin="simulated_past",
        actor_type="yui",
        occurred_at=at,
        payload=SimulatedExperiencePayload(
            block_id="blk_1", experience_class="routine", valence=0.3, summary="散歩した"
        ),
    )
    event_store.append(event)
    return event


def change(state_repo, runs, event, *, domain: str, key: str, clock, run_id: str):
    runs.start(
        run_id=run_id,
        root_event_id=event.event_id,
        snapshot_id=None,
        manifest_id=None,
        mode="simulation",
        priority=event.priority,
        now=clock.now(),
    )
    state_repo.write_value(
        domain=domain, key=key, value=0.5, confidence=None, now=clock.now(),
        run_id=run_id, event_id=event.event_id, expected_version=None,
    )
    state_repo.record_change(
        change_id=f"chg_{run_id}_{domain}_{key}",
        run_id=run_id,
        source_event_id=event.event_id,
        proposal_id=f"prp_{run_id}",
        source_module="test",
        domain=domain,
        key=key,
        operation="set",
        value_type="number",
        previous_value=None,
        new_value=0.5,
        delta=0.5,
        confidence=None,
        version_after=1,
        reason_codes=(),
        evidence_ids=(),
        now=clock.now(),
    )


# --- pipeline ---------------------------------------------------------------
def test_an_empty_database_reports_zero_everywhere(health) -> None:
    metrics = health.pipeline_metrics()
    assert metrics.simulated_experience_events == 0
    assert metrics.appraised_simulated_events == 0
    assert metrics.knowledge_sources_or_candidates == 0


def test_experiences_are_counted(health, make_event, event_store) -> None:
    experience(make_event, event_store)
    experience(make_event, event_store, at=MOMENT + timedelta(days=30))

    assert health.pipeline_metrics().simulated_experience_events == 2


def test_only_an_emotion_change_counts_as_having_been_read(
    health, make_event, event_store, state_repo, runs, clock
) -> None:
    """The world ticks on any event; the emotion engine needs an appraisal.

    Counting any state change would have let a Genesis with the appraisal route
    disabled pass — which is exactly the failure being guarded against.
    """
    ticked = experience(make_event, event_store)
    change(state_repo, runs, ticked, domain="world", key="sleep_pressure",
           clock=clock, run_id="run_world")

    metrics = health.pipeline_metrics()
    assert metrics.state_effect_changes == 1
    assert metrics.appraised_simulated_events == 0
    assert metrics.emotion_changes == 0

    felt = experience(make_event, event_store, at=MOMENT + timedelta(days=30))
    change(state_repo, runs, felt, domain="emotion", key="joy",
           clock=clock, run_id="run_felt")

    metrics = health.pipeline_metrics()
    assert metrics.appraised_simulated_events == 1
    assert metrics.emotion_changes == 1


def test_a_real_conversation_is_not_counted_as_a_simulated_life(
    health, make_event, event_store, state_repo, runs, clock
) -> None:
    """Spec 2.10: the two never merge, including in an audit."""
    said = make_event(text="やっほー")
    event_store.append(said)
    change(state_repo, runs, said, domain="emotion", key="joy",
           clock=clock, run_id="run_real")

    metrics = health.pipeline_metrics()
    assert metrics.appraised_simulated_events == 0
    assert metrics.state_effect_changes == 0


def test_only_genesis_consolidations_count_as_periodic(db, health, clock) -> None:
    from app.storage.repositories.growth import ConsolidationRepository

    consolidations = ConsolidationRepository(db)
    consolidations.start(kind="routine", now=clock.now())
    consolidations.start(kind="genesis_interval", now=clock.now())
    consolidations.start(kind="genesis_final", now=clock.now())

    assert health.pipeline_metrics().periodic_consolidation_runs == 2


# --- memory -----------------------------------------------------------------
def test_only_decided_episodes_count_as_encoding_decisions(db, health) -> None:
    from app.storage.repositories.memory import MemoryRepository

    memories = MemoryRepository(db)
    still_open = memories.open_episode(
        conversation_id=None, origin="simulated_past", started_at=MOMENT
    )
    decided = memories.open_episode(
        conversation_id=None, origin="simulated_past", started_at=MOMENT
    )
    memories.mark_episode(decided.episode_id, "discarded")

    metrics = health.memory_metrics()
    assert metrics.episodes == 2
    assert metrics.encoding_decisions == 1
    assert metrics.discarded_episodes == 1
    assert still_open.status == "open"


def test_a_conversation_episode_is_not_a_simulated_one(db, health) -> None:
    from app.storage.repositories.memory import MemoryRepository

    memories = MemoryRepository(db)
    episode = memories.open_episode(
        conversation_id="conv_1", origin="real_discord", started_at=MOMENT
    )
    memories.mark_episode(episode.episode_id, "encoded")

    assert health.memory_metrics().encoding_decisions == 0
    assert health.memory_metrics(origin=None).encoding_decisions == 1


# --- growth -----------------------------------------------------------------
def test_growth_metrics_read_the_ledger(db, health, clock) -> None:
    from app.storage.repositories.growth import AdaptationRepository, CandidateRepository

    adaptations = AdaptationRepository(db)
    adaptations.ensure("keeps_to_herself", initial=0.5, now=clock.now())
    adaptations.update(
        name="keeps_to_herself",
        value=0.55,
        pending_evidence=0.0,
        supporting_count=3,
        contradicting_count=1,
        contexts=("alone",),
        evidence_ids=("evd_1",),
        now=clock.now(),
    )
    CandidateRepository(db).create(
        domain="personality", key="openness", direction=1,
        source_adaptation=None, now=clock.now(),
    )

    metrics = health.growth_metrics()
    assert metrics.adaptation_evidence_seen == 4
    assert metrics.adaptations_with_evidence == 1
    assert metrics.candidates_raised == 1
    assert metrics.deep_gate_evaluations == 0


# --- blocks -----------------------------------------------------------------
def test_block_metrics(db, health, clock) -> None:
    from app.storage.repositories.simulation import SimulationRepository

    repository = SimulationRepository(db)
    seed_id = "sed_1"
    from app.simulation.models import TemperamentSeed

    repository.save_seed(
        TemperamentSeed(
            seed_id=seed_id, temperament={"openness": 0.5}, created_at=clock.now()
        )
    )
    scaffold = repository.save_scaffold(
        seed_id=seed_id,
        period_start=MOMENT,
        period_end=MOMENT + timedelta(days=365),
        environment="町",
        education_context="",
        social_density=0.5,
        technology_availability=0.5,
        life_stage="",
        now=clock.now(),
    )
    run = repository.start_run(
        seed_id=seed_id,
        scaffold_id=scaffold.scaffold_id,
        simulated_from=MOMENT,
        simulated_to=MOMENT + timedelta(days=365),
        now=clock.now(),
    )
    for index, (summary, count) in enumerate(
        (("春の町。歩いた。", 3), ("夏の町。読んだ。", 2), ("", 0))
    ):
        block = repository.add_block(
            simulation_id=run.simulation_id,
            phase_id=None,
            started_at=MOMENT + timedelta(days=30 * index),
            ended_at=MOMENT + timedelta(days=30 * index + 29),
            detail_level="compressed",
            experience_class="routine",
            summary=summary,
            event_count=0,
            ordinal=index,
        )
        if count:
            repository.set_block_event_count(block.block_id, count)

    assert health.blocks_with_zero_event_count(run.simulation_id) == 1
    assert health.distinct_block_summaries(run.simulation_id) == 2
    assert health.blocks_by_class(run.simulation_id) == {"routine": 3}
