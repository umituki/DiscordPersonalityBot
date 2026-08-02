"""The simulated past actually goes through psychology (patch spec 12-14, 24).

The 2026-08-02 Genesis produced 238 experiences and no psychology at all. Every
simulated experience was categorised ``internal``, the appraisal engine skips
``internal``, and with no appraisal there is no emotion, no needs, no evidence
and therefore no growth. The archive filled up; the person did not.

These tests drive the real pipeline — real processor, real appraisal engine,
real emotion/mood/needs engines — over a simulated life, and check that the
chain runs from end to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.consolidation.growth import GrowthEngine
from app.consolidation.policy import GrowthPolicy
from app.knowledge.policy import KnowledgePolicy
from app.knowledge.builder import KnowledgeBuilder
from app.knowledge.coverage import CoveragePlanner
from app.knowledge.providers import BundleProvider, ProviderRegistry
from app.knowledge.service import KnowledgeService
from app.memory.engine import MemoryEngine
from app.memory.material import (
    CompositeMaterialSource,
    SimulationEpisodeSource,
    VirtualLifeEpisodeSource,
)
from app.memory.policy import MemoryPolicy
from app.llm.structured import StructuredGenerator
from app.orchestrator.processor import EventProcessor
from app.psychology.appraisal import AppraisalEngine
from app.psychology.emotion import EmotionEngine
from app.psychology.mood import MoodEngine
from app.psychology.needs import NeedEngine
from app.psychology.policy import PsychologyPolicy
from app.resources.identity import load_identity
from app.simulation.engine import PastSimulationEngine
from app.simulation.events import (
    SIMULATED_EXPERIENCE,
    SIMULATION_BLOCK_COMPLETED,
    SimulatedExperiencePayload,
)
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
PERIOD_END = datetime(2005, 4, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def simulation_policy() -> SimulationPolicy:
    return SimulationPolicy.load(REPO_ROOT / "config" / "policies" / "simulation.yaml")


@pytest.fixture
def psychology_policy() -> PsychologyPolicy:
    return PsychologyPolicy.load(REPO_ROOT / "config" / "policies" / "psychology.yaml")


def experience_payload(**kwargs) -> SimulatedExperiencePayload:
    defaults = dict(block_id="blk_1", experience_class="routine", valence=0.3)
    defaults.update(kwargs)
    return SimulatedExperiencePayload(**defaults)


class PermissiveClient(ScriptedClient):
    """Answers narration requests forever; the cost tiers are tested elsewhere.

    A simulated life calls the model only for meaningful experiences and above,
    and how often that happens is a property of the sampler. Scripting an exact
    count here would test the sampler, not the pipeline.
    """

    NARRATION = (
        '{"summary": "川沿いを歩いた", "topics": ["散歩"], '
        '"felt_significance": 0.4, "involves_other_person": false}'
    )

    async def generate(self, request):
        if not self._script:
            self._script.append(self.NARRATION)
        return await super().generate(request)


class RecordingConsolidation:
    """Stands in for the job: records when it was asked to run, and with what."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, str | None]] = []

    async def run(self, *, kind: str, force: bool = False, now=None, mode=None):
        self.calls.append((kind, now, mode))
        return None


@pytest.fixture
def simulation(
    db, event_store, dispatcher, bus, snapshots, arbitrator, committer, runs, failures,
    prompt_registry, clock, simulation_policy, psychology_policy,
):
    """A full simulated life on the real pipeline."""

    def build(
        *,
        consolidation=None,
        narration_script: list | None = None,
        memory=True,
        knowledge=True,
    ):
        identity = load_identity(REPO_ROOT / "character")
        # No script for appraisal: routine and minor are read in Python, and
        # anything that does reach the model falls back to policy defaults.
        client = PermissiveClient(list(narration_script or []))
        structured = StructuredGenerator(
            client, prompts=prompt_registry, clock=clock, max_attempts=1
        )
        appraisal = AppraisalEngine(
            identity=identity,
            prompts=prompt_registry,
            structured=structured,
            policy=psychology_policy.appraisal,
            clock=clock,
        )
        bus.register(EmotionEngine(psychology_policy.emotion, clock=clock))
        bus.register(MoodEngine(psychology_policy.mood, clock=clock))
        bus.register(NeedEngine(psychology_policy.needs, clock=clock))

        processor = EventProcessor(
            db=db,
            event_store=event_store,
            dispatcher=dispatcher,
            snapshots=snapshots,
            arbitrator=arbitrator,
            committer=committer,
            runs=runs,
            failures=failures,
            interpreter=appraisal,
            mode="simulation",
            clock=clock,
        )
        coverage_planner = None
        if knowledge:
            from app.storage.repositories.knowledge import CoverageJobRepository

            knowledge_builder = KnowledgeBuilder(
                knowledge=KnowledgeRepository(db),
                jobs=CoverageJobRepository(db),
                policy=KnowledgePolicy.load(
                    REPO_ROOT / "config" / "policies" / "knowledge.yaml"
                ),
                clock=clock,
            )
            providers = ProviderRegistry(builder=knowledge_builder)
            providers.register(BundleProvider(REPO_ROOT / "config" / "knowledge"))
            coverage_planner = CoveragePlanner(
                builder=knowledge_builder, providers=providers, clock=clock
            )

        memory_engine = None
        if memory:
            from app.storage.repositories.memory import MemoryRepository

            memory_engine = MemoryEngine(
                repository=MemoryRepository(db),
                policy=MemoryPolicy.load(
                    REPO_ROOT / "config" / "policies" / "memory.yaml"
                ),
                structured=structured,
                prompts=prompt_registry,
                material=CompositeMaterialSource(
                    (SimulationEpisodeSource(event_store), VirtualLifeEpisodeSource(event_store))
                ),
                clock=clock,
            )
        candidates = CandidateRepository(db)
        engine = PastSimulationEngine(
            processor=processor,
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
            structured=structured,
            prompts=prompt_registry,
            policy=simulation_policy,
            consolidation=consolidation,
            memory=memory_engine,
            coverage=coverage_planner,
            clock=clock,
        )
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
        return engine, scaffold

    return build


# --- 12.1 the experiences are appraised at all ------------------------------
def test_a_simulated_experience_is_appraisable(make_event) -> None:
    """Patch spec 12.1: ``category="internal"`` だけで除外しない."""
    event = make_event(
        event_type=SIMULATED_EXPERIENCE,
        category="internal",
        origin="simulated_past",
        actor_type="yui",
        payload=experience_payload(),
    )
    assert AppraisalEngine.is_appraisable(event) is True


def test_an_internal_event_that_is_not_a_simulated_experience_is_still_skipped(
    make_event,
) -> None:
    from app.conversation.events import YuiReplySuppressedPayload

    event = make_event(
        event_type="YUI_REPLY_SUPPRESSED",
        category="internal",
        actor_type="system",
        payload=YuiReplySuppressedPayload(reason_code="x", stage="semantic", detail=""),
    )
    assert AppraisalEngine.is_appraisable(event) is False


def test_a_simulated_type_from_a_real_origin_is_not_appraised(make_event) -> None:
    """The origin is what makes it a simulated experience, not the name."""
    event = make_event(
        event_type=SIMULATED_EXPERIENCE,
        category="internal",
        origin="real_discord",
        actor_type="yui",
        payload=experience_payload(),
    )
    assert AppraisalEngine.is_appraisable(event) is False


async def test_a_simulated_life_produces_appraisals(simulation) -> None:
    engine, scaffold = simulation()
    result = await engine.run(scaffold, max_blocks=12)

    assert result.progress.experiences == 12
    assert result.progress.appraisals == result.progress.experiences, (
        "every simulated experience must be read; this is the 238-experiences bug"
    )


async def test_ordinary_experiences_are_read_without_a_model(simulation) -> None:
    """Patch spec 12.3: routine and minor are appraised in Python."""
    engine, scaffold = simulation()
    result = await engine.run(scaffold, max_blocks=12)

    by_source = result.progress.appraisals_by_source
    assert by_source.get("heuristic", 0) > 0
    assert "llm" not in by_source, "no model was scripted, so none should have been called"


async def test_the_appraisals_reach_emotion_and_state(simulation, state_repo) -> None:
    """Patch spec 12.2: the ordinary causal chain, end to end."""
    engine, scaffold = simulation()
    await engine.run(scaffold, max_blocks=12)

    emotions = {value.key: value.numeric for value in state_repo.list_all()
                if value.domain == "emotion"}
    assert emotions, "appraisal ran but no emotion state was ever committed"
    assert any((value or 0.0) > 0.0 for value in emotions.values())


async def test_no_user_relationship_is_created_by_a_simulated_life(
    simulation, state_repo
) -> None:
    """Patch spec 12.2: ``USER relationshipはsimulated pastで生成禁止``."""
    engine, scaffold = simulation()
    await engine.run(scaffold, max_blocks=12)

    assert [value for value in state_repo.list_all() if value.domain == "relationship"] == []


# --- 13 simulated time is the psychological time ----------------------------
async def test_the_run_reasons_at_the_simulated_moment(simulation, event_store) -> None:
    """Patch spec 13: ``simulationではsimulated timeを使う``."""
    engine, scaffold = simulation()
    await engine.run(scaffold, max_blocks=6)

    simulated = [
        event for event in event_store.recent(limit=200)
        if event.event_type == SIMULATED_EXPERIENCE
    ]
    assert simulated
    for event in simulated:
        assert PERIOD_START <= event.occurred_at <= PERIOD_END
        # recorded_at is the machine's time and stays the machine's time.
        assert event.recorded_at > event.occurred_at


async def test_the_simulated_timeline_is_monotonic(simulation, event_store) -> None:
    """Patch spec 13: ``1 run内のsimulated timeはmonotonicでなければならない``.

    The block event used to be created with a wall-clock ``occurred_at``, which
    put every 2003 block after every 2026 one.
    """
    engine, scaffold = simulation()
    await engine.run(scaffold, max_blocks=8)

    # ``by_types`` returns them oldest-first in the order they were appended,
    # which is the order the life was lived in.
    moments = [
        event.occurred_at
        for event in event_store.by_types(
            (SIMULATED_EXPERIENCE, SIMULATION_BLOCK_COMPLETED)
        )
    ]
    assert moments == sorted(moments)
    assert all(moment <= PERIOD_END for moment in moments)


def test_a_normal_run_uses_the_wall_clock(clock, make_event) -> None:
    processor = EventProcessor.__new__(EventProcessor)
    processor._clock = clock  # noqa: SLF001
    event = make_event(occurred_at=clock.now() - timedelta(days=400))

    assert processor._effective_now(event, "normal") == clock.now()  # noqa: SLF001
    assert processor._effective_now(event, "simulation") == event.occurred_at  # noqa: SLF001


# --- 14 consolidation happens during the life -------------------------------
async def test_consolidation_runs_more_than_once_during_a_life(simulation) -> None:
    """Patch spec 14: ``Genesis終了時1回だけを禁止する``."""
    recorder = RecordingConsolidation()
    engine, scaffold = simulation(consolidation=recorder)

    result = await engine.run(scaffold, max_blocks=24)

    assert result.progress.consolidations > 1
    assert len(recorder.calls) == result.progress.consolidations
    assert set(result.progress.consolidation_triggers) > {"final"}


async def test_each_consolidation_runs_at_its_simulated_moment(simulation) -> None:
    recorder = RecordingConsolidation()
    engine, scaffold = simulation(consolidation=recorder)

    await engine.run(scaffold, max_blocks=24)

    moments = [moment for _, moment, _ in recorder.calls]
    assert all(PERIOD_START <= moment <= PERIOD_END for moment in moments)
    assert moments == sorted(moments)
    assert all(mode == "simulation" for _, _, mode in recorder.calls)


async def test_the_final_consolidation_still_happens(simulation) -> None:
    recorder = RecordingConsolidation()
    engine, scaffold = simulation(consolidation=recorder)

    await engine.run(scaffold, max_blocks=6)

    assert recorder.calls[-1][0] == "genesis_final"


async def test_a_life_without_a_consolidation_job_still_runs(simulation) -> None:
    """Degraded, not broken — and the FIRST BOOT audits are what refuse it."""
    engine, scaffold = simulation(consolidation=None)
    result = await engine.run(scaffold, max_blocks=6)

    assert result.completed
    assert result.progress.consolidations == 0


# --- 24 the block's event count is real -------------------------------------
async def test_a_block_records_how_many_events_it_produced(simulation, db) -> None:
    engine, scaffold = simulation()
    await engine.run(scaffold, max_blocks=6)

    blocks = SimulationRepository(db).blocks(engine.latest_run().simulation_id)
    assert blocks
    assert all(block.event_count > 1 for block in blocks), (
        "an experience, its knowledge exposures and the block event all count"
    )
    assert len({block.event_count for block in blocks}) >= 1


# --- 15 the life is remembered, selectively ---------------------------------
async def test_a_simulated_life_offers_its_experiences_to_memory(simulation) -> None:
    """Patch spec 15.1: the pipeline runs; it is not bypassed and not skipped."""
    engine, scaffold = simulation()
    result = await engine.run(scaffold, max_blocks=24)

    assert result.progress.episode_events == result.progress.experiences
    assert result.progress.encoding_attempts > 0, (
        "nineteen years used to reach the Encoding Gate zero times"
    )


async def test_not_everything_is_remembered(simulation, db) -> None:
    """Patch spec 15.1: ``全Event記憶化は禁止``.

    What the gate does with any particular stretch depends on what the stretch
    was like — that is tested directly in ``test_simulation_memory.py``. What
    is asserted here is that a life does not come out as one memory per
    experience: the experiences are grouped, and the group is what is judged.
    """
    from app.storage.repositories.memory import MemoryRepository

    engine, scaffold = simulation()
    result = await engine.run(scaffold, max_blocks=24)

    encoded = MemoryRepository(db).memory_count()
    assert 0 < encoded < result.progress.experiences
    assert encoded == result.progress.memories_encoded


async def test_a_life_without_a_memory_engine_still_runs(simulation) -> None:
    engine, scaffold = simulation(memory=False)
    result = await engine.run(scaffold, max_blocks=6)

    assert result.completed
    assert result.progress.encoding_attempts == 0


# --- 16 the period has knowledge in it --------------------------------------
async def test_a_simulated_life_has_a_knowledge_layer(simulation, db) -> None:
    """Patch spec 16.1: ``source/candidate/exposure pipelineが0件`` is a failure."""
    engine, scaffold = simulation()
    result = await engine.run(scaffold, max_blocks=12)

    assert result.progress.knowledge_requests > 0
    assert result.progress.knowledge_candidates > 0
    assert KnowledgeRepository(db).count() == result.progress.knowledge_candidates


async def test_the_period_knowledge_is_offered_to_her(simulation) -> None:
    """Patch spec 16.4: existing is not knowing; the funnel still decides."""
    engine, scaffold = simulation()
    result = await engine.run(scaffold, max_blocks=12)

    assert result.progress.knowledge_exposures > 0
    assert result.progress.knowledge_acquired <= result.progress.knowledge_exposures


async def test_nothing_from_after_the_simulated_moment_leaks_in(simulation) -> None:
    """Patch spec 16.3: the temporal fields are kept, so the guard can work."""
    engine, scaffold = simulation()
    result = await engine.run(scaffold, max_blocks=12)

    assert result.progress.leakage_attempts == 0


async def test_a_life_without_a_coverage_planner_has_an_empty_layer(simulation, db) -> None:
    """The regression, kept visible: this is what the health audit refuses."""
    engine, scaffold = simulation(knowledge=False)
    result = await engine.run(scaffold, max_blocks=6)

    assert result.progress.knowledge_candidates == 0
    assert KnowledgeRepository(db).count() == 0
