"""INVARIANT: a Genesis that did not happen cannot boot (patch spec 17, 18).

The 2026-08-02 Genesis passed every audit it had. It had 238 experiences, an
identity in range, no leakage and no drift — and no psychology at all, because
nothing checked whether any of those experiences had had an effect.

The audits here read committed rows rather than what the run reported about
itself, and each one is exercised in both directions: a healthy Genesis passes
it, and a Genesis with one stage disabled fails it. An audit that has never
been seen to fail is not evidence of anything.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.bootstrap import Application
from app.orchestrator.run_view import Interpretation
from app.simulation.seed import SeedRequest
from app.storage.repositories.health import HealthRepository
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]

# Long enough to be held to the multi-year thresholds (patch spec 17.2, 17.3).
PERIOD_START = datetime(2003, 4, 1, 9, 0, tzinfo=timezone.utc)
PERIOD_END = datetime(2013, 4, 1, 9, 0, tzinfo=timezone.utc)

BLOCKS = 40


@pytest.fixture
def genesis(temp_config, clock):
    """A built application, plus a way to run a whole life through it."""
    built: list[Application] = []

    async def run(*, blocks: int = BLOCKS, break_stage: str | None = None):
        application = Application.build(temp_config, clock=clock, configure_logs=False)
        built.append(application)
        use_offline_model(application)
        _break(application, break_stage)

        seed = application.seed_builder.build(
            SeedRequest(answers={}, interests=("音楽", "読書"))
        )
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        result = await application.simulation.run(scaffold, max_blocks=blocks)
        report = await application.genesis.first_boot(result.run)
        return application, result, report

    yield run

    for application in built:
        application.db.close()


def _break(application: Application, stage: str | None) -> None:
    """Disable exactly one stage of the causal chain (patch spec 17 injection)."""
    if stage is None:
        return
    if stage == "appraisal":
        # Scenario 6: the route is disabled with a test double, so experiences
        # still happen and nothing reads them.
        async def no_interpretation(event, snapshot):
            return Interpretation()

        application.processor._interpreter.interpret = no_interpretation  # noqa: SLF001
    elif stage == "memory":
        application.simulation._memory = None  # noqa: SLF001
    elif stage == "knowledge":
        application.simulation._coverage = None  # noqa: SLF001
    elif stage == "consolidation":
        application.simulation._consolidation = None  # noqa: SLF001
    else:  # pragma: no cover - a typo in a test should be loud
        raise AssertionError(f"unknown stage {stage!r}")


def detail_of(report, kind: str) -> dict:
    return next(audit.detail for audit in report.audits if audit.kind == kind)


# --- a healthy Genesis passes ------------------------------------------------
async def test_a_genesis_that_actually_happened_boots(genesis) -> None:
    _, result, report = await genesis()

    assert report.booted is True, f"{report.refusal}: {json.dumps(_failed(report))}"
    assert report.failed == ()
    assert result.progress.appraisals == result.progress.experiences


async def test_the_pipeline_audit_sees_every_stage(genesis) -> None:
    """Patch spec 17's list, each read from a committed row."""
    _, _, report = await genesis()
    metrics = detail_of(report, "pipeline_health")["metrics"]

    assert metrics["simulated_experience_events"] > 0
    assert metrics["appraised_simulated_events"] > 0
    assert metrics["state_effect_changes"] > 0
    assert metrics["emotion_changes"] > 0
    assert metrics["memory_encoding_attempts"] > 0
    assert metrics["periodic_consolidation_runs"] > 1
    assert metrics["knowledge_sources"] + metrics["knowledge_candidates"] > 0
    assert metrics["knowledge_exposure_opportunities"] > 0


# --- 17 / Scenario 6: a broken chain is refused ------------------------------
async def test_experiences_without_appraisal_do_not_boot(genesis) -> None:
    """Patch spec 27 Scenario 6, and the 2026-08-02 failure exactly.

    The experiences are all still there. Nothing read any of them, and that
    alone must be enough to refuse the boot.
    """
    _, result, report = await genesis(break_stage="appraisal")

    assert result.progress.experiences > 0, "the experiences still happened"
    assert report.booted is False
    assert "pipeline_health" in report.failed
    failed = detail_of(report, "pipeline_health")["failed"]
    assert "appraisal_processed" in failed
    assert "emotion_effects" in failed
    # The world still ticked — sleep pressure and the day advance on any event —
    # which is exactly why a bare state change cannot stand in for a reading.
    assert detail_of(report, "pipeline_health")["metrics"]["state_effect_changes"] > 0


async def test_a_life_nobody_remembers_does_not_boot(genesis) -> None:
    """Patch spec 17.2."""
    _, _, report = await genesis(break_stage="memory")

    assert report.booted is False
    assert "memory_health" in report.failed
    assert "encoding_decisions" in detail_of(report, "memory_health")["failed"]


async def test_a_period_with_no_knowledge_does_not_boot(genesis) -> None:
    """Patch spec 17.3: ``source=0 / opportunity=0はfailure``."""
    _, _, report = await genesis(break_stage="knowledge")

    assert report.booted is False
    assert "knowledge_health" in report.failed
    metrics = detail_of(report, "knowledge_health")["metrics"]
    assert metrics["items"] == 0


async def test_one_consolidation_at_the_end_does_not_boot(genesis) -> None:
    """Patch spec 14 and 17: ``periodic_consolidation_runs > 1``."""
    _, _, report = await genesis(break_stage="consolidation")

    assert report.booted is False
    assert "pipeline_health" in report.failed
    assert "periodic_consolidation" in detail_of(report, "pipeline_health")["failed"]


# --- 17.1 growth health ------------------------------------------------------
async def test_growth_health_checks_the_machinery_not_the_outcome(genesis) -> None:
    """Patch spec 17.1: ``Personality変化を強制しない``.

    A life that left her much the same is a possible life. What the audit
    requires is that evidence was seen and the layer was exercised — never that
    a trait moved, which would be the shortcut prohibition 2 and 4 forbid.
    """
    _, _, report = await genesis()
    detail = detail_of(report, "growth_health")

    assert detail["checks"]["growth_evidence_seen"] is True
    assert detail["checks"]["adaptation_pipeline_exercised"] is True
    # Nothing in here demands a personality change.
    assert "personality_changed" not in detail["checks"]
    assert "trait_moved" not in detail["checks"]


# --- 18 block audit ----------------------------------------------------------
async def test_blocks_count_what_they_produced(genesis) -> None:
    """Patch spec 18.1-18.2: ``固定0は禁止``."""
    application, result, report = await genesis()
    detail = detail_of(report, "block")

    assert detail["blocks_with_zero_event_count"] == 0
    blocks = application.simulation.blocks(result.run.simulation_id)
    assert all(block.event_count > 0 for block in blocks)


async def test_ordinary_days_are_not_all_described_the_same_way(genesis) -> None:
    """Patch spec 18.3: sixty blocks in two sentences is not a life."""
    application, result, report = await genesis()
    detail = detail_of(report, "block")

    assert detail["distinct_summaries"] > 2
    health = HealthRepository(application.db)
    assert health.distinct_block_summaries(result.run.simulation_id) > 2


async def test_a_block_with_no_events_fails_the_audit(genesis) -> None:
    application, result, _ = await genesis(blocks=12)
    blocks = application.simulation.blocks(result.run.simulation_id)
    application.db.execute(
        "UPDATE simulation_blocks SET event_count = 0 WHERE block_id = ?",
        (blocks[0].block_id,),
    )

    audit = application.genesis._block_audit(result.run)  # noqa: SLF001

    assert audit.passed is False
    assert "event_counts_are_real" in audit.detail["failed"]


# --- 17.4 the gate is a gate -------------------------------------------------
async def test_no_boot_event_is_emitted_when_an_audit_fails(genesis) -> None:
    """Patch spec 17.4: ``critical auditが1件でもfailならbootしない``."""
    application, _, report = await genesis(break_stage="appraisal")

    assert report.booted is False
    assert report.event is None
    assert application.genesis.has_booted() is False
    types = {event.event_type for event in application.event_store.recent(limit=500)}
    assert "FIRST_BOOT" not in types


async def test_every_audit_is_recorded_even_when_it_fails(genesis) -> None:
    """A refusal has to be inspectable afterwards, not just logged."""
    _, result, report = await genesis(break_stage="memory")

    kinds = {audit.kind for audit in report.audits}
    assert {"pipeline_health", "growth_health", "memory_health", "knowledge_health"} <= kinds
    for audit in report.audits:
        assert audit.detail, f"{audit.kind} recorded no detail"


# --- the thresholds are not decorative --------------------------------------
def test_a_multi_year_run_requires_memories_and_knowledge(temp_config) -> None:
    """Patch spec prohibition 5: not ``min_memories=0`` with an audit bolted on."""
    from app.simulation.policy import SimulationPolicy

    rules = SimulationPolicy.load(
        temp_config.policies_dir / "simulation.yaml"
    ).first_boot

    assert rules.memories_floor(10.0) >= 1
    assert rules.retained_knowledge_floor(10.0) >= 1
    assert rules.min_periodic_consolidations > 1
    # A short smoke run is still allowed to be small.
    assert rules.memories_floor(0.5) == rules.min_memories


def _failed(report) -> dict:
    return {
        audit.kind: audit.detail.get("failed", audit.detail)
        for audit in report.audits
        if not audit.passed
    }
