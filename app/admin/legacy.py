"""Diagnosing an existing database (patch spec 23.3).

    コード修正だけでは既存の壊れたGenesis stateは治らない。

The patches before this one make a *new* Genesis produce a person. They do
nothing for a database that already booted from a broken one: the experiences
are recorded, no psychology ever followed from them, and the FIRST BOOT that
let it through has already happened.

This module only looks. It answers one question — is the Genesis under this
database sound — and never changes anything, because the same database also
holds real Discord history that must not be touched (23.1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from app.storage.repositories.health import (
    GrowthMetrics,
    HealthRepository,
    KnowledgeMetrics,
    MemoryMetrics,
    PipelineMetrics,
)
from app.storage.repositories.simulation import SimulationRepository

logger = logging.getLogger(__name__)

MODULE = "legacy_health"

#: What the scan concluded.
#:
#: ``healthy``          the Genesis under this database holds up.
#: ``legacy_invalid``   it booted, and the causal chain never ran.
#: ``needs_rebuild``    same, and there is a seed to rebuild it from.
#: ``no_genesis``       nothing has booted yet; there is nothing to repair.
GenesisHealth = Literal["healthy", "legacy_invalid", "needs_rebuild", "no_genesis"]


@dataclass(frozen=True, slots=True)
class LegacyFinding:
    """One thing that is missing, in the words the owner needs to read."""

    code: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.code}: {self.detail}"


@dataclass
class LegacyReport:
    genesis_health: GenesisHealth
    findings: list[LegacyFinding] = field(default_factory=list)
    simulation_id: str | None = None
    seed_id: str | None = None
    scaffold_id: str | None = None
    real_events: int = 0
    first_boot_at: str | None = None
    pipeline: PipelineMetrics = field(default_factory=PipelineMetrics)
    growth: GrowthMetrics = field(default_factory=GrowthMetrics)
    memory: MemoryMetrics = field(default_factory=MemoryMetrics)
    knowledge: KnowledgeMetrics = field(default_factory=KnowledgeMetrics)

    @property
    def sound(self) -> bool:
        return self.genesis_health in ("healthy", "no_genesis")

    @property
    def rebuildable(self) -> bool:
        """Whether a shadow rebuild has what it needs (patch spec 23.4 step 2)."""
        return self.seed_id is not None and self.scaffold_id is not None

    @property
    def has_real_history(self) -> bool:
        """Real Discord history exists, so nothing may be deleted (23.1)."""
        return self.real_events > 0

    def as_detail(self) -> dict[str, object]:
        return {
            "genesis_health": self.genesis_health,
            "findings": [{"code": f.code, "detail": f.detail} for f in self.findings],
            "simulation_id": self.simulation_id,
            "seed_id": self.seed_id,
            "scaffold_id": self.scaffold_id,
            "first_boot_at": self.first_boot_at,
            "real_events": self.real_events,
            "rebuildable": self.rebuildable,
            "metrics": {
                "simulated_experience_events": self.pipeline.simulated_experience_events,
                "appraised_simulated_events": self.pipeline.appraised_simulated_events,
                "emotion_changes": self.pipeline.emotion_changes,
                "periodic_consolidation_runs": self.pipeline.periodic_consolidation_runs,
                "adaptations_with_evidence": self.growth.adaptations_with_evidence,
                "growth_evidence_seen": self.growth.adaptation_evidence_seen,
                "encoding_decisions": self.memory.encoding_decisions,
                "active_memories": self.memory.active_memories,
                "knowledge_sources": self.knowledge.sources,
                "knowledge_items": self.knowledge.items,
                "knowledge_exposures": self.knowledge.exposure_opportunities,
            },
        }

    def render(self) -> str:
        lines = [f"genesis_health = {self.genesis_health}"]
        if self.first_boot_at:
            lines.append(f"first boot: {self.first_boot_at}")
        lines.append(f"real Discord events: {self.real_events}")
        for finding in self.findings:
            lines.append(f"  - {finding}")
        return "\n".join(lines)


class LegacyHealthScanner:
    """Read-only diagnosis of an already-booted database (patch spec 23.3)."""

    name = MODULE

    def __init__(
        self,
        *,
        health: HealthRepository,
        simulations: SimulationRepository,
        real_origins: frozenset[str] = frozenset({"real_discord"}),
    ) -> None:
        self._health = health
        self._simulations = simulations
        self._real_origins = real_origins

    def scan(self) -> LegacyReport:
        booted = self._simulations.booted_run()
        pipeline = self._health.pipeline_metrics()
        growth = self._health.growth_metrics()
        memory = self._health.memory_metrics()
        knowledge = self._health.knowledge_metrics()
        real_events = self._health.events_by_origin(self._real_origins)

        report = LegacyReport(
            genesis_health="no_genesis",
            simulation_id=None if booted is None else booted.simulation_id,
            seed_id=None if booted is None else booted.seed_id,
            scaffold_id=None if booted is None else booted.scaffold_id,
            first_boot_at=(
                None
                if booted is None or booted.first_boot_at is None
                else booted.first_boot_at.isoformat()
            ),
            real_events=real_events,
            pipeline=pipeline,
            growth=growth,
            memory=memory,
            knowledge=knowledge,
        )
        if booted is None:
            return report

        report.findings = list(self._findings(pipeline, growth, memory, knowledge))
        if not report.findings:
            report.genesis_health = "healthy"
        elif report.rebuildable:
            report.genesis_health = "needs_rebuild"
        else:
            report.genesis_health = "legacy_invalid"

        logger.info(
            "legacy scan health=%s findings=%d real_events=%d",
            report.genesis_health,
            len(report.findings),
            real_events,
        )
        return report

    @staticmethod
    def _findings(
        pipeline: PipelineMetrics,
        growth: GrowthMetrics,
        memory: MemoryMetrics,
        knowledge: KnowledgeMetrics,
    ):
        """Patch spec 23.3's list, each stated in terms of what is missing."""
        if pipeline.simulated_experience_events == 0:
            yield LegacyFinding(
                "no_simulated_experiences",
                "the database holds no simulated past at all",
            )
        elif pipeline.appraised_simulated_events == 0:
            yield LegacyFinding(
                "appraisal_never_ran",
                f"{pipeline.simulated_experience_events} experiences and none of "
                "them was ever read: no emotion followed from any of them",
            )
        if growth.adaptations_with_evidence == 0:
            yield LegacyFinding(
                "adaptations_empty", "no characteristic adaptation ever saw evidence"
            )
        if growth.adaptation_evidence_seen == 0:
            yield LegacyFinding(
                "growth_evidence_empty", "the growth pipeline was never exercised"
            )
        if memory.encoding_decisions == 0:
            yield LegacyFinding(
                "simulated_episodic_path_inactive",
                "no episode of the simulated past ever reached the Encoding Gate",
            )
        if knowledge.items == 0:
            # A registered source with nothing behind it is still an empty
            # layer: bootstrap registers the bundle provider whether or not any
            # coverage was ever run, so `sources > 0` proves nothing.
            yield LegacyFinding(
                "historical_knowledge_empty",
                "the period has no knowledge layer: nothing was ever available "
                f"to be learned (sources={knowledge.sources}, items=0)",
            )
        elif knowledge.exposure_opportunities == 0:
            yield LegacyFinding(
                "knowledge_never_offered",
                "knowledge exists and was never put in front of her",
            )
        if pipeline.periodic_consolidation_runs <= 1:
            yield LegacyFinding(
                "periodic_consolidation_missing",
                "consolidation ran at most once, so the Deep Gate only ever saw "
                "a single evidence window",
            )


__all__ = [
    "GenesisHealth",
    "LegacyFinding",
    "LegacyHealthScanner",
    "LegacyReport",
    "MODULE",
]
