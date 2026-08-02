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
from typing import TYPE_CHECKING, Sequence

from app.clock import Clock, FixedClock, SystemClock, to_iso
from app.consolidation.growth import GrowthEngine
from app.events.model import Event
from app.knowledge.service import KnowledgeService
from app.knowledge.temporal import TemporalLeakageError
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage
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
from app.simulation.experiences import (
    ExperienceBudget,
    ExperienceNarration,
    ExperienceSampler,
)
from app.simulation.models import (
    LifeScaffold,
    SimulationBlock,
    SimulationRun,
    TemperamentSeed,
)
from app.simulation.policy import SimulationPolicy
from app.storage.repositories.simulation import SimulationRepository

if TYPE_CHECKING:  # pragma: no cover - the job imports this module's siblings
    from app.consolidation.job import ConsolidationJob
    from app.knowledge.coverage import CoveragePlanner
    from app.memory.engine import MemoryEngine

logger = logging.getLogger(__name__)

MODULE = "past_simulation_engine"
ORIGIN = "simulated_past"
NARRATION_PROMPT_ID = "simulated_experience"
NARRATION_PURPOSE = "simulated_experience"

#: After this many consecutive failures the model is treated as unavailable
#: for the rest of the run (spec 28.3).
MAX_NARRATION_FAILURES = 3

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
    #: Experiences that were worth a model call (spec 22.4).
    narrated: int = 0
    demotions: list[str] = field(default_factory=list)
    by_class: dict[str, int] = field(default_factory=dict)
    recent_summaries: list[str] = field(default_factory=list)
    #: Patch spec 14: consolidations that ran *during* the life, by trigger.
    consolidations: int = 0
    consolidation_triggers: dict[str, int] = field(default_factory=dict)
    #: Patch spec 12: appraisals the experiences actually produced, by source.
    appraisals: int = 0
    appraisals_by_source: dict[str, int] = field(default_factory=dict)
    #: Patch spec 16: what the period's knowledge layer was planned as.
    knowledge_requests: int = 0
    knowledge_candidates: int = 0
    knowledge_classes: list[str] = field(default_factory=list)
    knowledge_missing_classes: list[str] = field(default_factory=list)
    #: Patch spec 15: what the memory pipeline was offered and what it kept.
    episode_events: int = 0
    encoding_attempts: int = 0
    memories_encoded: int = 0
    encoding_refusals: dict[str, int] = field(default_factory=dict)


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
        structured: StructuredGenerator,
        prompts: PromptRegistry,
        policy: SimulationPolicy,
        consolidation: "ConsolidationJob | None" = None,
        memory: "MemoryEngine | None" = None,
        coverage: "CoveragePlanner | None" = None,
        clock: Clock | None = None,
        rng: Random | None = None,
    ) -> None:
        self._processor = processor
        self._repository = repository
        self._knowledge = knowledge
        self._growth = growth
        self._structured = structured
        self._prompts = prompts
        self._policy = policy
        #: Patch spec 14. Optional so the engine can still be exercised alone,
        #: but a Genesis run without it produces no growth — which is what the
        #: FIRST BOOT audits are there to refuse.
        self._consolidation = consolidation
        #: Patch spec 15.1. Optional for the same reason, and audited for the
        #: same reason: a life nobody remembers any of is not a life.
        self._memory = memory
        #: Patch spec 16.3. Without it the period has no knowledge to offer,
        #: the exposure funnel has nothing to run on, and the knowledge health
        #: audit refuses the boot (16.1).
        self._coverage = coverage
        self._clock = clock or SystemClock()
        self._rng = rng or Random(20260101)
        self._sampler = ExperienceSampler(policy.experience, rng=self._rng)
        #: Consecutive failed narrations. A simulated life must not spend
        #: itself retrying an unreachable model (spec 28.3).
        self._narration_failures = 0

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
        # Patch spec 16.3: what the world knew during this period is planned
        # and stored *before* the life is lived, so there is something for the
        # exposure funnel to offer her as she goes.
        self._plan_knowledge(scaffold, seed, progress)
        budget = ExperienceBudget(years=scaffold.years)
        limit = min(max_blocks or self._policy.blocks.max_blocks, self._policy.blocks.max_blocks)

        phases = self._create_phases(run.simulation_id, scaffold)
        blocks: list[SimulationBlock] = []
        moment = scaffold.period_start
        ordinal = 0
        # Patch spec 14: consolidation happens *during* the life, on simulated
        # time. Tracked here rather than inside the block so a phase change
        # between blocks is visible.
        last_consolidated_at = scaffold.period_start
        previous_phase_id: str | None = None

        while moment < scaffold.period_end and ordinal < limit:
            phase = self._phase_at(phases, moment)
            phase_id = None if phase is None else phase.phase_id
            block, moment = await self._run_block(
                run=run,
                scaffold=scaffold,
                seed=seed,
                phase_id=phase_id,
                started_at=moment,
                ordinal=ordinal,
                budget=budget,
                progress=progress,
            )
            blocks.append(block)
            ordinal += 1

            trigger = self._consolidation_trigger(
                block=block,
                moment=moment,
                last_consolidated_at=last_consolidated_at,
                phase_id=phase_id,
                previous_phase_id=previous_phase_id,
                first_block=ordinal == 1,
            )
            previous_phase_id = phase_id
            if trigger is not None:
                # Memory before consolidation: consolidation reads what is
                # remembered, so encoding has to have happened by then.
                await self._encode_due_memories(moment, progress)
                if await self._consolidate(moment, trigger, progress):
                    last_consolidated_at = moment

        await self._encode_due_memories(moment, progress)
        if self._policy.genesis.final_consolidation:
            await self._consolidate(moment, "final", progress)

        progress.blocks = len(blocks)
        progress.demotions = list(budget.demotions)
        finished = self._repository.finish_run(
            run.simulation_id,
            status="completed",
            blocks_run=progress.blocks,
            experiences=progress.experiences,
            detail={
                "expanded_blocks": progress.expanded_blocks,
                "narrated": progress.narrated,
                "knowledge_acquired": progress.knowledge_acquired,
                "demotions": progress.demotions[:20],
                "by_class": progress.by_class,
                # Patch spec 12 and 14: the evidence that the causal chain
                # actually ran, kept where the FIRST BOOT audits can read it.
                "appraisals": progress.appraisals,
                "appraisals_by_source": progress.appraisals_by_source,
                "consolidations": progress.consolidations,
                "consolidation_triggers": progress.consolidation_triggers,
                "episode_events": progress.episode_events,
                "encoding_attempts": progress.encoding_attempts,
                "memories_encoded": progress.memories_encoded,
                "encoding_refusals": progress.encoding_refusals,
                "knowledge_requests": progress.knowledge_requests,
                "knowledge_candidates": progress.knowledge_candidates,
                "knowledge_classes": progress.knowledge_classes,
                "knowledge_missing_classes": progress.knowledge_missing_classes,
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

        # --- describe it, at the cost its class justifies (spec 22.4) -------
        narration = await self._narrate(
            experience,
            scaffold=scaffold,
            seed=seed,
            occurred_at=started_at,
            recent=progress.recent_summaries[-3:],
        )
        if experience.llm_cost != "none":
            progress.narrated += 1

        # --- the experience goes through the ordinary pipeline --------------
        event = self._experience_event(
            block_id=block.block_id,
            occurred_at=started_at,
            experience=experience,
            narration=narration,
        )
        outcome = await self._processor.process(event, mode="simulation")
        self._record_appraisal(outcome, progress)
        # Patch spec 15.1: the experience becomes episode material and goes
        # through segmentation and the Encoding Gate like anything else. It is
        # never written straight into the memory table (prohibition 3).
        self._remember(event, progress)
        progress.experiences += 1
        progress.recent_summaries.append(narration.summary)
        self._repository.set_block_summary(block.block_id, narration.summary)

        # --- and so does whatever the world was saying at the time ----------
        block_exposures = self._expose_period_knowledge(
            moment=started_at,
            seed=seed,
            block_id=block.block_id,
            progress=progress,
        )

        # Patch spec 24 (Patch D): the count is the events this block really
        # produced, not a constant. The experience, plus one event per piece of
        # period knowledge that reached her, plus the block event itself.
        event_count = 1 + block_exposures + 1
        block_event = self._block_event(
            block, event, occurred_at=ended_at, event_count=event_count
        )
        await self._processor.process(block_event, mode="simulation")
        self._repository.set_block_event_count(block.block_id, event_count)
        return block.model_copy(update={"event_count": event_count}), ended_at

    @staticmethod
    def _record_appraisal(outcome, progress: SimulationProgress) -> None:
        """Patch spec 12.1: an experience that produced no appraisal was not lived.

        Counted per run so a Genesis that silently stopped appraising is
        visible in the run detail, and refusable by the FIRST BOOT audits,
        rather than showing up only as a personality that never moved.
        """
        appraisal = None
        if outcome.interpretation is not None:
            appraisal = outcome.interpretation.appraisal
        if appraisal is None:
            return
        progress.appraisals += 1
        source = getattr(appraisal, "source", "unknown")
        progress.appraisals_by_source[source] = progress.appraisals_by_source.get(source, 0) + 1

    # --- period knowledge (patch spec 16) -----------------------------------
    def _plan_knowledge(
        self, scaffold: LifeScaffold, seed: TemperamentSeed, progress: SimulationProgress
    ) -> None:
        """Fill the external-knowledge layer for the simulated period.

        This says nothing about what YUI knows — only what was public and when.
        Whether any of it reached her is the exposure funnel's decision, one
        item at a time (patch spec 16.4).
        """
        if self._coverage is None:
            return
        try:
            report = self._coverage.run(
                period_start=scaffold.period_start,
                period_end=scaffold.period_end,
                topics=tuple(seed.interests),
            )
        except Exception:  # noqa: BLE001 - recorded; the audits refuse an empty layer
            logger.exception("knowledge coverage failed for %s", scaffold.scaffold_id)
            return
        progress.knowledge_requests = report.requests
        progress.knowledge_candidates = report.candidates_stored
        progress.knowledge_classes = list(report.classes_covered)
        progress.knowledge_missing_classes = list(report.missing_classes)

    # --- memory during the life (patch spec 15) -----------------------------
    def _remember(self, event: Event, progress: SimulationProgress) -> None:
        """Offer one experience to the memory pipeline. It may refuse it."""
        if self._memory is None:
            return
        try:
            self._memory.observe(event, conversation_id=None, now=event.occurred_at)
        except Exception:  # noqa: BLE001 - recorded, then the life continues
            logger.exception("memory observation failed at %s", event.occurred_at.isoformat())
            return
        progress.episode_events += 1

    async def _encode_due_memories(
        self, moment: datetime, progress: SimulationProgress
    ) -> None:
        """Close finished stretches, encode what the gate accepts, forget the rest.

        Patch spec 15.3: ``19年間すべてを同じaccessibilityで保持しない``. Forgetting
        runs on simulated time, so a memory from the first year has had years
        of decay by the last one — which is what makes what survives a
        selection rather than a complete record.
        """
        if self._memory is None:
            return
        try:
            self._memory.close_due_episodes(now=moment)
            results = await self._memory.encode_pending(limit=20, now=moment)
            self._memory.apply_forgetting(now=moment)
        except Exception:  # noqa: BLE001 - recorded, then the life continues
            logger.exception("memory maintenance failed at %s", moment.isoformat())
            return
        progress.encoding_attempts += len(results)
        progress.memories_encoded += sum(1 for result in results if result.encoded)
        for result in results:
            if not result.encoded:
                progress.encoding_refusals[result.reason] = (
                    progress.encoding_refusals.get(result.reason, 0) + 1
                )

    # --- consolidation during the life (patch spec 14) ----------------------
    def _consolidation_trigger(
        self,
        *,
        block: SimulationBlock,
        moment: datetime,
        last_consolidated_at: datetime,
        phase_id: str | None,
        previous_phase_id: str | None,
        first_block: bool,
    ) -> str | None:
        """Why consolidation should run now, or ``None``.

        Deliberately several triggers rather than one interval: a life reviews
        itself when enough time has passed, when a chapter of it ends, and
        after something large. Each produces a separate evidence window, which
        is what the Deep Gate needs and what one final run cannot give it.
        """
        rules = self._policy.genesis
        if rules.consolidate_after_major_event and block.experience_class in rules.major_classes:
            return "major_event"
        if (
            rules.consolidate_on_phase_boundary
            and not first_block
            and phase_id != previous_phase_id
        ):
            return "phase_boundary"
        elapsed = (moment - last_consolidated_at).total_seconds() / 86400.0
        if elapsed >= rules.consolidation_interval_simulated_days:
            return "interval"
        return None

    async def _consolidate(
        self, moment: datetime, trigger: str, progress: SimulationProgress
    ) -> bool:
        """Run one consolidation at a simulated moment.

        ``force=True`` because the job's own interval is measured in wall-clock
        hours between real runs, which says nothing about a simulated life; the
        interval that matters here is the simulated one, already checked by
        :meth:`_consolidation_trigger`.

        A failure degrades the life rather than ending it — the run is still a
        life, and the FIRST BOOT audits will refuse it if too little happened.
        """
        if self._consolidation is None:
            return False
        try:
            await self._consolidation.run(
                kind=f"genesis_{trigger}", force=True, now=moment, mode="simulation"
            )
        except Exception:  # noqa: BLE001 - recorded, then the life continues
            logger.exception("consolidation failed during simulation at %s", moment.isoformat())
            return False
        progress.consolidations += 1
        progress.consolidation_triggers[trigger] = (
            progress.consolidation_triggers.get(trigger, 0) + 1
        )
        return True

    def _expose_period_knowledge(
        self,
        *,
        moment: datetime,
        seed: TemperamentSeed,
        block_id: str,
        progress: SimulationProgress,
    ) -> int:
        """Offer the period's knowledge. The funnel decides what sticks.

        Returns how many exposures actually happened, which is part of the
        block's real event count.
        """
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
            return 0
        progress.knowledge_exposures += len(results)
        progress.knowledge_acquired += sum(1 for result in results if result.learned)
        return len(results)

    # --- events -------------------------------------------------------------
    def _experience_event(
        self,
        *,
        block_id: str,
        occurred_at: datetime,
        experience,
        narration: ExperienceNarration,
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
                summary=narration.summary,
                text=narration.summary,
                topics=tuple(narration.topics)[:4],
                felt_significance=narration.felt_significance,
                involves_other_person=narration.involves_other_person,
                demoted_from=experience.demoted_from,
            ),
            clock=self._clock,
        )

    def _block_event(
        self,
        block: SimulationBlock,
        parent: Event,
        *,
        occurred_at: datetime,
        event_count: int,
    ) -> Event:
        """The block is closed at the simulated moment it ended.

        Patch spec 13: without ``occurred_at`` this event lands at wall-clock
        now, which puts a 2003 block after a 2026 one and makes the simulated
        timeline non-monotonic in the archive.
        """
        return parent.child(
            event_type=SIMULATION_BLOCK_COMPLETED,
            category="internal",
            actor_type="yui",
            source_type=MODULE,
            clock=self._clock,
            occurred_at=occurred_at,
            priority="P5",
            payload=BlockCompletedPayload(
                block_id=block.block_id,
                detail_level=block.detail_level,
                experience_class=block.experience_class,
                days=round(block.days, 2),
                event_count=event_count,
            ),
        )

    async def _narrate(
        self,
        experience,
        *,
        scaffold: LifeScaffold,
        seed: TemperamentSeed,
        occurred_at: datetime,
        recent: Sequence[str],
    ) -> ExperienceNarration:
        """Describe one experience, at the cost its class justifies (spec 22.4).

        ``Routine`` は主に Python — routine and minor never reach the model at
        all, which is what makes simulating years of ordinary life affordable.
        Meaningful gets a light call; only major and turning points get a
        detailed one.

        A failed or unusable generation falls back to the plain description
        rather than inventing something: a degraded life is still a life, and
        spec 28.2 forbids committing unvalidated output.
        """
        cost = experience.llm_cost
        if cost == "none" or not self._narration_available:
            return ExperienceNarration(summary=self._plain_summary(experience, scaffold))

        template = self._prompts.get(NARRATION_PROMPT_ID)
        content = template.render(
            occurred_at=to_iso(occurred_at),
            life_stage=scaffold.life_stage or "そのころ",
            environment=scaffold.environment or "ふつうの暮らし",
            education_context=scaffold.education_context or "とくに定まっていない",
            experience_class=experience.experience_class,
            tone="よい方向" if experience.valence >= 0 else "つらい方向",
            interests="、".join(seed.interests) or "まだはっきりしない",
            recent="\n".join(f"- {line}" for line in recent) or "- （とくにない）",
        )
        outcome = await self._structured.generate(
            ExperienceNarration,
            (LLMMessage(role="user", content=content),),
            purpose=NARRATION_PURPOSE,
            # Spec 33: the simulated past is P7 and yields to everything.
            priority="P7",
            prompt_id=NARRATION_PROMPT_ID,
            prompt_version=template.prompt_version,
            temperature=0.9 if cost == "detailed" else 0.7,
            max_tokens=400 if cost == "detailed" else 200,
        )
        if not outcome.accepted or outcome.value is None:
            self._narration_failures += 1
            if not self._narration_available:
                logger.warning(
                    "narration failed %d times; the rest of this life is described "
                    "without the model (spec 28.3)",
                    self._narration_failures,
                )
            return ExperienceNarration(summary=self._plain_summary(experience, scaffold))
        self._narration_failures = 0
        return outcome.value

    @property
    def _narration_available(self) -> bool:
        """Stop asking once the model has clearly stopped answering.

        A degraded run still produces a life — every experience still goes
        through the full psychology pipeline — it is simply described plainly
        rather than narrated (spec 28.3).
        """
        return self._narration_failures < MAX_NARRATION_FAILURES

    @staticmethod
    def _plain_summary(experience, scaffold: LifeScaffold) -> str:
        """What an ordinary day looks like without asking a model (spec 22.4)."""
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
