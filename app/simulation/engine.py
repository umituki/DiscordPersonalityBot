"""Past Simulation Engine (spec 22).

    Temperament / Current State → Opportunity → Decision → Action → Outcome
    → Appraisal → Emotion → Experience → Memory
    → Beliefs / Self / Relationship / Habits → Consolidation → next State

    最終人格に合わせて逆算して Event を生成してはならない。

The causal loop of spec 22.6 is not reimplemented here. Each simulated
experience becomes an ordinary event and goes through the ordinary processor:
the same appraisal, the same engines, the same arbitrator, the same
transaction. That is the rule from ``.claude/rules/architecture.md`` — past
simulation must not grow a second, simpler personality engine — and it is also
the only way the resulting person is one this system could have produced.

Two things this engine structurally cannot do:

* **Work backwards from a desired personality.** There is no parameter for a
  target. What comes out depends on the seed, the scaffold and what happens.
* **Give YUI a relationship with the USER.** Nothing here creates a ``user``
  actor or a ``real_discord`` origin, and :meth:`first_boot` is the only thing
  that opens that door (spec 22.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from random import Random
from typing import Sequence

from app.clock import Clock, FixedClock, SystemClock
from app.consolidation.growth import GrowthEngine
from app.events.model import Event
from app.knowledge.service import KnowledgeService
from app.knowledge.temporal import TemporalLeakageError
from app.orchestrator.processor import EventProcessor
from app.simulation.events import (
    FIRST_BOOT,
    LIFE_PHASE_STARTED,
    SIMULATED_EXPERIENCE,
    SIMULATION_BLOCK_COMPLETED,
    BlockCompletedPayload,
    LifePhaseStartedPayload,
    SimulatedExperiencePayload,
)
from app.simulation.experiences import ExperienceBudget, ExperienceSampler
from app.simulation.models import (
    LifeScaffold,
    SimulationBlock,
    SimulationRun,
    TemperamentSeed,
)
from app.simulation.policy import SimulationPolicy
from app.storage.repositories.simulation import SimulationRepository

logger = logging.getLogger(__name__)

MODULE = "past_simulation_engine"
ORIGIN = "simulated_past"

#: Spec 22.3: life stages are scaffold, not personality. Names only.
DEFAULT_PHASES: tuple[str, ...] = ("childhood", "adolescence", "young_adulthood")


@dataclass
class SimulationProgress:
    """Running totals for one simulation."""

    blocks: int = 0
    experiences: int = 0
    expanded_blocks: int = 0
    knowledge_exposures: int = 0
    knowledge_acquired: int = 0
    leakage_attempts: int = 0
    demotions: list[str] = field(default_factory=list)
    by_class: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SimulationResult:
    run: SimulationRun
    progress: SimulationProgress
    blocks: tuple[SimulationBlock, ...] = ()

    @property
    def completed(self) -> bool:
        return self.run.status == "completed"


class PastSimulationEngine:
    name = MODULE

    def __init__(
        self,
        *,
        processor: EventProcessor,
        repository: SimulationRepository,
        knowledge: KnowledgeService,
        growth: GrowthEngine,
        policy: SimulationPolicy,
        clock: Clock | None = None,
        rng: Random | None = None,
    ) -> None:
        self._processor = processor
        self._repository = repository
        self._knowledge = knowledge
        self._growth = growth
        self._policy = policy
        self._clock = clock or SystemClock()
        self._rng = rng or Random(20260101)
        self._sampler = ExperienceSampler(policy.experience, rng=self._rng)

    # --- setup --------------------------------------------------------------
    def prepare(
        self,
        seed: TemperamentSeed,
        *,
        period_start: datetime,
        period_end: datetime,
        environment: str = "",
        education_context: str = "",
        social_density: float = 0.5,
        technology_availability: float = 0.5,
        life_stage: str = "",
    ) -> LifeScaffold:
        """Store the seed and the circumstances (spec 22.3).

        Everything the scaffold may contain is a circumstance. Hobbies, values,
        habits and the resulting personality are deliberately not arguments.

        The seed's temperament becomes the trait *baselines* the simulated life
        starts from — the bottom layer of spec 12.1, and the only thing the
        questionnaire is allowed to reach (spec 22.2).
        """
        self._repository.save_seed(seed)
        self._growth.ensure_seeded(seed.temperament)
        return self._repository.save_scaffold(
            seed_id=seed.seed_id,
            period_start=period_start,
            period_end=period_end,
            environment=environment,
            education_context=education_context,
            social_density=social_density,
            technology_availability=technology_availability,
            life_stage=life_stage,
            now=self._clock.now(),
        )

    # --- the run ------------------------------------------------------------
    async def run(
        self,
        scaffold: LifeScaffold,
        *,
        max_blocks: int | None = None,
    ) -> SimulationResult:
        """Live a simulated life, one block at a time, through the real pipeline."""
        seed = self._repository.seed(scaffold.seed_id)
        if seed is None:
            raise ValueError(f"unknown seed: {scaffold.seed_id!r}")

        run = self._repository.start_run(
            seed_id=seed.seed_id,
            scaffold_id=scaffold.scaffold_id,
            simulated_from=scaffold.period_start,
            simulated_to=scaffold.period_end,
            now=self._clock.now(),
        )
        progress = SimulationProgress()
        budget = ExperienceBudget(years=scaffold.years)
        limit = min(max_blocks or self._policy.blocks.max_blocks, self._policy.blocks.max_blocks)

        phases = self._create_phases(run.simulation_id, scaffold)
        blocks: list[SimulationBlock] = []
        moment = scaffold.period_start
        ordinal = 0

        while moment < scaffold.period_end and ordinal < limit:
            phase = self._phase_at(phases, moment)
            block, moment = await self._run_block(
                run=run,
                scaffold=scaffold,
                seed=seed,
                phase_id=None if phase is None else phase.phase_id,
                started_at=moment,
                ordinal=ordinal,
                budget=budget,
                progress=progress,
            )
            blocks.append(block)
            ordinal += 1

        progress.blocks = len(blocks)
        progress.demotions = list(budget.demotions)
        finished = self._repository.finish_run(
            run.simulation_id,
            status="completed",
            blocks_run=progress.blocks,
            experiences=progress.experiences,
            detail={
                "expanded_blocks": progress.expanded_blocks,
                "knowledge_acquired": progress.knowledge_acquired,
                "demotions": progress.demotions[:20],
                "by_class": progress.by_class,
            },
            now=self._clock.now(),
        )
        logger.info(
            "simulation complete blocks=%d experiences=%d acquired=%d",
            progress.blocks,
            progress.experiences,
            progress.knowledge_acquired,
        )
        return SimulationResult(finished, progress, tuple(blocks))

    async def _run_block(
        self,
        *,
        run: SimulationRun,
        scaffold: LifeScaffold,
        seed: TemperamentSeed,
        phase_id: str | None,
        started_at: datetime,
        ordinal: int,
        budget: ExperienceBudget,
        progress: SimulationProgress,
    ) -> tuple[SimulationBlock, datetime]:
        rules = self._policy.blocks
        experience = self._sampler.sample(budget)

        # Spec 22.5: a stretch is expanded only when something in it warrants
        # it. Otherwise it is one compressed block.
        expanded = experience.is_significant or abs(experience.valence) >= (
            rules.expansion_threshold
        )
        span_days = rules.expanded_block_days if expanded else rules.compressed_block_days
        ended_at = min(scaffold.period_end, started_at + timedelta(days=span_days))

        block = self._repository.add_block(
            simulation_id=run.simulation_id,
            phase_id=phase_id,
            started_at=started_at,
            ended_at=ended_at,
            detail_level="expanded" if expanded else "compressed",
            experience_class=experience.experience_class,
            summary="",
            event_count=0,
            ordinal=ordinal,
        )
        progress.expanded_blocks += 1 if expanded else 0
        progress.by_class[experience.experience_class] = (
            progress.by_class.get(experience.experience_class, 0) + 1
        )

        # --- the experience goes through the ordinary pipeline --------------
        event = self._experience_event(
            block_id=block.block_id,
            occurred_at=started_at,
            experience=experience,
            scaffold=scaffold,
        )
        await self._processor.process(event, mode="simulation")
        progress.experiences += 1

        # --- and so does whatever the world was saying at the time ----------
        self._expose_period_knowledge(
            moment=started_at,
            seed=seed,
            block_id=block.block_id,
            progress=progress,
        )

        await self._processor.process(
            self._block_event(block, event), mode="simulation"
        )
        return block, ended_at

    def _expose_period_knowledge(
        self,
        *,
        moment: datetime,
        seed: TemperamentSeed,
        block_id: str,
        progress: SimulationProgress,
    ) -> None:
        """Offer the period's knowledge. The funnel decides what sticks."""
        rules = self._policy.knowledge
        interests = {topic: 0.7 for topic in seed.interests}
        try:
            results = self._knowledge.expose_available(
                moment=moment,
                interest_by_topic=interests,
                curiosity=rules.base_curiosity,
                limit=rules.exposures_per_block,
                simulation_block_id=block_id,
                origin=ORIGIN,
            )
        except TemporalLeakageError:
            # The guard fired inside the funnel: a candidate slipped through
            # the availability query. Record it and continue without it —
            # leaking is never the fallback (spec 21.4).
            progress.leakage_attempts += 1
            logger.error("temporal leakage blocked at %s", moment.isoformat())
            return
        progress.knowledge_exposures += len(results)
        progress.knowledge_acquired += sum(1 for result in results if result.learned)

    # --- events -------------------------------------------------------------
    def _experience_event(
        self,
        *,
        block_id: str,
        occurred_at: datetime,
        experience,
        scaffold: LifeScaffold,
    ) -> Event:
        """A simulated experience: ``yui`` actor, ``simulated_past`` origin.

        ``occurred_at`` is the simulated moment and ``recorded_at`` is now, so
        the archive never claims this was lived in real time (spec 8.3).
        """
        return Event.create(
            event_type=SIMULATED_EXPERIENCE,
            category="internal",
            actor_type="yui",
            source_type=MODULE,
            origin=ORIGIN,
            priority="P5",
            occurred_at=occurred_at,
            payload=SimulatedExperiencePayload(
                block_id=block_id,
                experience_class=experience.experience_class,
                valence=experience.valence,
                summary=self._summarise(experience, scaffold),
                text=self._summarise(experience, scaffold),
                demoted_from=experience.demoted_from,
            ),
            clock=self._clock,
        )

    def _block_event(self, block: SimulationBlock, parent: Event) -> Event:
        return parent.child(
            event_type=SIMULATION_BLOCK_COMPLETED,
            category="internal",
            actor_type="yui",
            source_type=MODULE,
            clock=self._clock,
            priority="P5",
            payload=BlockCompletedPayload(
                block_id=block.block_id,
                detail_level=block.detail_level,
                experience_class=block.experience_class,
                days=round(block.days, 2),
                event_count=1,
            ),
        )

    def _summarise(self, experience, scaffold: LifeScaffold) -> str:
        """A placeholder description.

        Spec 22.4 reserves detailed generation for major experiences; wiring
        the LLM in here is a later concern. What matters structurally is that
        the *class* decides the cost, and that nothing is written backwards
        from a desired personality.
        """
        where = scaffold.environment or "そのころの暮らし"
        tone = "よかったこと" if experience.valence >= 0 else "つらかったこと"
        return f"{where}での{tone}（{experience.experience_class}）"

    # --- phases -------------------------------------------------------------
    def _create_phases(self, simulation_id: str, scaffold: LifeScaffold):
        span = (scaffold.period_end - scaffold.period_start) / max(1, len(DEFAULT_PHASES))
        phases = []
        for index, name in enumerate(DEFAULT_PHASES):
            started = scaffold.period_start + span * index
            ended = (
                scaffold.period_end
                if index == len(DEFAULT_PHASES) - 1
                else scaffold.period_start + span * (index + 1)
            )
            phases.append(
                self._repository.add_phase(
                    simulation_id=simulation_id,
                    name=name,
                    started_at=started,
                    ended_at=ended,
                    summary="",
                    ordinal=index,
                )
            )
        return phases

    @staticmethod
    def _phase_at(phases: Sequence, moment: datetime):
        for phase in phases:
            if phase.started_at <= moment < phase.ended_at:
                return phase
        return phases[-1] if phases else None

    # --- reads --------------------------------------------------------------
    def blocks(self, simulation_id: str) -> list[SimulationBlock]:
        return self._repository.blocks(simulation_id)

    def latest_run(self) -> SimulationRun | None:
        return self._repository.latest_run()


def simulated_clock(moment: datetime) -> FixedClock:
    """A clock pinned to a simulated instant, for engines that read ``now``."""
    return FixedClock(moment)


__all__ = [
    "FIRST_BOOT",
    "LIFE_PHASE_STARTED",
    "LifePhaseStartedPayload",
    "PastSimulationEngine",
    "SimulationProgress",
    "SimulationResult",
    "simulated_clock",
]
