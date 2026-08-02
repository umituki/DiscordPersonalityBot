"""Experience narration and its cost tiers (spec 22.4, 28.3).

``Routine は主に Python`` — the class decides what may reach the model, which
is what makes simulating years of ordinary life affordable at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.consolidation.growth import GrowthEngine
from app.consolidation.policy import GrowthPolicy
from app.knowledge.policy import KnowledgePolicy
from app.knowledge.service import KnowledgeService
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.orchestrator.processor import EventProcessor
from app.simulation.engine import MAX_NARRATION_FAILURES, PastSimulationEngine
from app.simulation.experiences import Experience
from app.simulation.policy import SimulationPolicy
from app.simulation.seed import SeedBuilder, SeedRequest
from app.storage.repositories.growth import (
    CandidateRepository,
    NarrativeRepository,
    PersonalityRepository,
)
from app.storage.repositories.knowledge import (
    AcquisitionRepository,
    ExposureRepository,
    KnowledgeRepository,
)
from app.storage.repositories.simulation import SimulationRepository
from tests.unit.test_llm_structured import ScriptedClient

REPO_ROOT = Path(__file__).resolve().parents[2]

PERIOD_START = datetime(2003, 4, 1, 9, 0, tzinfo=timezone.utc)
PERIOD_END = datetime(2006, 4, 1, 9, 0, tzinfo=timezone.utc)

NARRATION = (
    '{"summary": "近所の川沿いを歩いた", "topics": ["散歩", "川"], '
    '"felt_significance": 0.4, "involves_other_person": false}'
)


@pytest.fixture
def simulation_policy() -> SimulationPolicy:
    return SimulationPolicy.load(REPO_ROOT / "config" / "policies" / "simulation.yaml")


def build_engine(db, clock, script: list, simulation_policy):
    prompts = PromptRegistry.load(REPO_ROOT / "config" / "prompts")
    client = ScriptedClient(script)
    candidates = CandidateRepository(db)
    engine = PastSimulationEngine(
        processor=EventProcessor.__new__(EventProcessor),  # not used by _narrate
        repository=SimulationRepository(db),
        knowledge=KnowledgeService(
            knowledge=KnowledgeRepository(db),
            exposures=ExposureRepository(db),
            acquisitions=AcquisitionRepository(db),
            policy=KnowledgePolicy.load(
                REPO_ROOT / "config" / "policies" / "knowledge.yaml"
            ),
            clock=clock,
        ),
        growth=GrowthEngine(
            candidates=candidates,
            traits=PersonalityRepository(db),
            narratives=NarrativeRepository(db),
            policy=GrowthPolicy.load(REPO_ROOT / "config" / "policies" / "growth.yaml"),
            clock=clock,
        ),
        structured=StructuredGenerator(
            client, prompts=prompts, clock=clock, max_attempts=1
        ),
        prompts=prompts,
        policy=simulation_policy,
        clock=clock,
    )
    return engine, client


def scaffold_for(db, clock, simulation_policy):
    seed = SeedBuilder(simulation_policy.seed, clock=clock).build(
        SeedRequest(answers={}, interests=("音楽",))
    )
    repository = SimulationRepository(db)
    repository.save_seed(seed)
    scaffold = repository.save_scaffold(
        seed_id=seed.seed_id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        environment="小さな町",
        education_context="学校",
        social_density=0.5,
        technology_availability=0.4,
        life_stage="こども",
        now=clock.now(),
    )
    return seed, scaffold


async def narrate(engine, seed, scaffold, experience_class: str, valence: float = 0.3):
    return await engine._narrate(  # noqa: SLF001 - the cost rule is what is under test
        Experience(experience_class=experience_class, valence=valence),
        scaffold=scaffold,
        seed=seed,
        occurred_at=PERIOD_START,
        recent=(),
    )


# --- the cost tiers (spec 22.4) ---------------------------------------------
@pytest.mark.parametrize("experience_class", ["routine", "minor"])
async def test_ordinary_days_never_reach_the_model(
    db, clock, simulation_policy, experience_class
) -> None:
    engine, client = build_engine(db, clock, [], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    narration = await narrate(engine, seed, scaffold, experience_class)

    assert client.requests == []
    assert narration.summary
    # Patch spec 18.3: the plain summary is composed from activity, context,
    # sociality, topic and life stage — it deliberately no longer reads as a
    # template with the class name in it.
    assert experience_class not in narration.summary


@pytest.mark.parametrize("experience_class", ["meaningful", "major", "turning_point"])
async def test_a_significant_experience_is_described_by_the_model(
    db, clock, simulation_policy, experience_class
) -> None:
    engine, client = build_engine(db, clock, [NARRATION], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    narration = await narrate(engine, seed, scaffold, experience_class)

    assert len(client.requests) == 1
    assert narration.summary == "近所の川沿いを歩いた"
    assert narration.topics == ["散歩", "川"]


async def test_a_turning_point_is_given_more_room_than_a_meaningful_day(
    db, clock, simulation_policy
) -> None:
    engine, client = build_engine(db, clock, [NARRATION, NARRATION], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    await narrate(engine, seed, scaffold, "meaningful")
    await narrate(engine, seed, scaffold, "turning_point")

    light, detailed = client.requests
    assert detailed.max_tokens > light.max_tokens


async def test_the_prompt_is_versioned_and_traced(db, clock, simulation_policy) -> None:
    """Spec 29: which prompt produced this must be answerable later."""
    engine, client = build_engine(db, clock, [NARRATION], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    await narrate(engine, seed, scaffold, "major")

    request = client.requests[0]
    assert request.prompt_id == "simulated_experience"
    assert request.prompt_version
    # Spec 33: the simulated past is the lowest priority there is.
    assert request.priority == "P7"


async def test_the_narration_prompt_carries_the_scaffold_not_a_target(
    db, clock, simulation_policy
) -> None:
    """Spec 22.6: nothing is written backwards from a desired personality."""
    engine, client = build_engine(db, clock, [NARRATION], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    await narrate(engine, seed, scaffold, "major")

    content = client.requests[0].messages[0].content
    assert "小さな町" in content
    assert "音楽" in content
    # No trait, value or outcome is ever handed to the generator.
    for forbidden in ("extraversion", "openness", "self_direction", "personality"):
        assert forbidden not in content


# --- degrading (spec 28.3) ---------------------------------------------------
async def test_an_unusable_answer_still_produces_a_life(
    db, clock, simulation_policy
) -> None:
    engine, _ = build_engine(db, clock, ["not json at all"], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    narration = await narrate(engine, seed, scaffold, "major")

    assert narration.summary
    assert "major" not in narration.summary


async def test_a_dead_model_stops_being_asked(db, clock, simulation_policy) -> None:
    """A simulated life must not spend itself retrying an unreachable model."""
    engine, client = build_engine(
        db, clock, ["bad"] * (MAX_NARRATION_FAILURES + 5), simulation_policy
    )
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    for _ in range(MAX_NARRATION_FAILURES + 3):
        narration = await narrate(engine, seed, scaffold, "major")
        assert narration.summary  # every experience still gets a description

    assert len(client.requests) == MAX_NARRATION_FAILURES


async def test_a_good_answer_clears_the_failure_count(db, clock, simulation_policy) -> None:
    engine, client = build_engine(db, clock, ["bad", NARRATION, "bad"], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    await narrate(engine, seed, scaffold, "major")
    recovered = await narrate(engine, seed, scaffold, "major")
    await narrate(engine, seed, scaffold, "major")

    assert recovered.summary == "近所の川沿いを歩いた"
    assert len(client.requests) == 3


# --- ordinary days do not all read the same (patch spec 18.3) --------------
async def test_ordinary_days_are_not_all_the_same_sentence(
    db, clock, simulation_policy
) -> None:
    """The 2026-08-02 Genesis wrote sixty blocks in two sentences."""
    from datetime import timedelta

    engine, client = build_engine(db, clock, [], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    summaries = set()
    for index in range(30):
        narration = await engine._narrate(  # noqa: SLF001
            Experience(experience_class="routine", valence=0.3 if index % 2 else -0.3),
            scaffold=scaffold,
            seed=seed,
            occurred_at=PERIOD_START + timedelta(days=30 * index),
            recent=(),
        )
        summaries.add(narration.summary)

    assert client.requests == []
    assert len(summaries) > 5, f"only {len(summaries)} distinct summaries in 30 days"


async def test_the_same_day_is_described_the_same_way_twice(
    db, clock, simulation_policy
) -> None:
    """Spec 29: a replay of the same simulation produces the same life."""
    engine, _ = build_engine(db, clock, [], simulation_policy)
    seed, scaffold = scaffold_for(db, clock, simulation_policy)

    first = await narrate(engine, seed, scaffold, "routine", valence=0.4)
    second = await narrate(engine, seed, scaffold, "routine", valence=0.4)

    assert first.summary == second.summary
