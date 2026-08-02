"""FIRST BOOT and the audits in front of it (spec 22.7).

::

    Past Simulation completed
    → Deep Consolidation
    → Consistency Audit
    → Knowledge chronology audit
    → Identity audit
    → Drift audit
    → Quality evaluation
    → FIRST_BOOT
    → Real Discord history begins

    FIRST BOOT 以前に USER との関係経験を生成しない。

FIRST BOOT is a gate, not a ceremony. Every audit is recorded with its detail
whether it passes or fails, and the boot event is emitted only when all of them
passed — so "we checked" is a row in a table rather than something somebody
remembers doing.

The last line of the spec is enforced twice: the consistency audit fails if any
real-Discord or USER-actor event exists before the boot, and :meth:`first_boot`
refuses to proceed on that audit's failure. A simulated past that already
contains the USER is not a past worth keeping.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.clock import Clock, SystemClock
from app.consolidation.drift import INVALID, DriftMonitor
from app.consolidation.growth import GrowthEngine
from app.consolidation.job import ConsolidationJob
from app.events.model import Event
from app.events.store import EventStore
from app.knowledge.service import KnowledgeService
from app.simulation.events import FIRST_BOOT, FirstBootPayload
from app.simulation.models import AUDIT_KINDS, GenesisAudit, SimulationRun
from app.simulation.policy import FirstBootRules
from app.storage.repositories.events import EventRepository
from app.storage.repositories.simulation import SimulationRepository

logger = logging.getLogger(__name__)

MODULE = "genesis_service"

#: Origins and actors that must not exist before FIRST BOOT (spec 22.7).
REAL_ORIGINS: frozenset[str] = frozenset({"real_discord"})
USER_ACTOR = "user"


class FirstBootRefused(RuntimeError):
    """Raised when FIRST BOOT was attempted with an audit outstanding."""


@dataclass(frozen=True, slots=True)
class GenesisReport:
    simulation_id: str
    audits: tuple[GenesisAudit, ...]
    booted: bool
    event: Event | None = None
    refusal: str = ""

    @property
    def passed(self) -> tuple[str, ...]:
        return tuple(audit.kind for audit in self.audits if audit.passed)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(audit.kind for audit in self.audits if not audit.passed)

    @property
    def all_passed(self) -> bool:
        return bool(self.audits) and not self.failed


class GenesisService:
    name = MODULE

    def __init__(
        self,
        *,
        repository: SimulationRepository,
        events: EventRepository,
        event_store: EventStore,
        consolidation: ConsolidationJob,
        knowledge: KnowledgeService,
        growth: GrowthEngine,
        drift: DriftMonitor,
        policy: FirstBootRules,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._events = events
        self._event_store = event_store
        self._consolidation = consolidation
        self._knowledge = knowledge
        self._growth = growth
        self._drift = drift
        self._policy = policy
        self._clock = clock or SystemClock()

    # --- the gate -----------------------------------------------------------
    async def first_boot(self, run: SimulationRun) -> GenesisReport:
        """Consolidate, audit, and boot only if every audit passed."""
        if run.booted:
            return GenesisReport(run.simulation_id, (), True, refusal="already booted")

        # Spec 22.7 order: deep consolidation first, so the audits look at the
        # person the simulation actually produced.
        await self._consolidation.run(kind="genesis", force=True)

        audits = (
            self._consistency_audit(run),
            self._knowledge_chronology_audit(run),
            self._identity_audit(run),
            self._drift_audit(run),
            self._quality_audit(run),
        )
        report = GenesisReport(run.simulation_id, audits, booted=False)
        if not report.all_passed:
            logger.warning(
                "FIRST BOOT withheld simulation=%s failed=%s",
                run.simulation_id,
                list(report.failed),
            )
            return GenesisReport(
                run.simulation_id,
                audits,
                booted=False,
                refusal=f"audits failed: {', '.join(report.failed)}",
            )

        event = await self._emit_first_boot(run, report.passed)
        self._repository.record_first_boot(run.simulation_id, now=self._clock.now())
        logger.info("FIRST BOOT simulation=%s", run.simulation_id)
        return GenesisReport(run.simulation_id, audits, booted=True, event=event)

    def has_booted(self) -> bool:
        """True once a real present has begun. Nothing may precede it."""
        return self._repository.booted_run() is not None

    # --- audits (spec 22.7) -------------------------------------------------
    def _consistency_audit(self, run: SimulationRun) -> GenesisAudit:
        """No USER, no real Discord history, and time runs forwards."""
        real_events = self._events.count_by_origin(REAL_ORIGINS)
        user_events = self._events.count_by_actor(USER_ACTOR)
        blocks = self._repository.blocks(run.simulation_id)

        out_of_order = sum(
            1
            for previous, block in zip(blocks, blocks[1:])
            if block.started_at < previous.started_at
        )
        outside_period = sum(
            1
            for block in blocks
            if block.started_at < run.simulated_from or block.ended_at > run.simulated_to
        )
        passed = (
            real_events == 0
            and user_events == 0
            and out_of_order == 0
            and outside_period == 0
        )
        return self._record(
            run,
            "consistency",
            passed,
            {
                "real_discord_events": real_events,
                "user_actor_events": user_events,
                "blocks_out_of_order": out_of_order,
                "blocks_outside_period": outside_period,
            },
        )

    def _knowledge_chronology_audit(self, run: SimulationRun) -> GenesisAudit:
        report = self._knowledge.chronology_audit(until=run.simulated_to)
        return self._record(
            run,
            "knowledge_chronology",
            report.clean,
            {"checked": report.checked, "leaked": list(report.leaked)[:20]},
        )

    def _identity_audit(self, run: SimulationRun) -> GenesisAudit:
        """There has to be somebody there, and she has to be in range."""
        traits = self._growth.traits()
        out_of_range = [
            trait.name for trait in traits if not 0.0 <= trait.baseline <= 1.0
        ]
        seed = self._repository.seed(run.seed_id)
        seeded = set(seed.temperament) if seed is not None else set()
        missing = sorted(seeded - {trait.name for trait in traits})
        passed = bool(traits) and not out_of_range and not missing
        return self._record(
            run,
            "identity",
            passed,
            {
                "traits": len(traits),
                "out_of_range": out_of_range,
                "missing_from_seed": missing,
            },
        )

    def _drift_audit(self, run: SimulationRun) -> GenesisAudit:
        """A simulated life may change a lot. It may not change impossibly."""
        anomalies = self._drift.open_anomalies(limit=50)
        invalid = [item.metric for item in anomalies if item.classification == INVALID]
        return self._record(
            run, "drift", not invalid, {"invalid_metrics": invalid}
        )

    def _quality_audit(self, run: SimulationRun) -> GenesisAudit:
        blocks = self._repository.block_count(run.simulation_id)
        retained = len(self._knowledge.retained(limit=500))
        checks = {
            "experiences": run.experiences >= self._policy.min_experiences,
            "blocks": blocks >= self._policy.min_blocks,
            "retained_knowledge": retained >= self._policy.min_retained_knowledge,
        }
        return self._record(
            run,
            "quality",
            all(checks.values()),
            {
                "experiences": run.experiences,
                "blocks": blocks,
                "retained_knowledge": retained,
                "checks": checks,
            },
        )

    def _record(
        self, run: SimulationRun, kind: str, passed: bool, detail: dict
    ) -> GenesisAudit:
        return self._repository.record_audit(
            simulation_id=run.simulation_id,
            kind=kind,
            passed=passed,
            detail=detail,
            now=self._clock.now(),
        )

    # --- the boot event -----------------------------------------------------
    async def _emit_first_boot(self, run: SimulationRun, passed: tuple[str, ...]) -> Event:
        years = (run.simulated_to - run.simulated_from).total_seconds() / 31_557_600.0
        event = Event.create(
            event_type=FIRST_BOOT,
            category="system",
            actor_type="system",
            source_type=MODULE,
            origin="system",
            priority="P1",
            payload=FirstBootPayload(
                simulation_id=run.simulation_id,
                simulated_years=round(years, 3),
                experiences=run.experiences,
                blocks=self._repository.block_count(run.simulation_id),
                audits_passed=passed,
            ),
            clock=self._clock,
        )
        await asyncio.to_thread(self._event_store.append, event)
        return event

    # --- reads --------------------------------------------------------------
    def audits(self, simulation_id: str) -> list[GenesisAudit]:
        return self._repository.audits(simulation_id)

    def outstanding(self, simulation_id: str) -> list[str]:
        latest = self._repository.latest_audits(simulation_id, AUDIT_KINDS)
        return [
            kind
            for kind in AUDIT_KINDS
            if kind not in latest or not latest[kind].passed
        ]
