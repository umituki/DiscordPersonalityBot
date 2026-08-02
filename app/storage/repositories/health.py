"""Durable evidence that a Genesis actually happened (patch spec 17).

Every number here is read from what was committed, not from a counter the run
kept in memory. That is the whole point: the 2026-08-02 Genesis reported 238
experiences and passed every audit, because nothing checked whether any of
those experiences had had an effect. A run can be wrong about what it did; the
tables cannot.

Reads only. SQL lives here because repositories are the SQL boundary
(``.claude/rules/architecture.md``); the audits themselves are in
:mod:`app.simulation.genesis`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from app.clock import to_iso
from app.storage.database import Database

SIMULATED_ORIGIN = "simulated_past"
SIMULATED_EXPERIENCE = "SIMULATED_EXPERIENCE"

#: Consolidation runs started by the simulation carry this prefix (patch 14).
GENESIS_KIND_PREFIX = "genesis"


@dataclass(frozen=True, slots=True)
class PipelineMetrics:
    """What the causal chain left behind, stage by stage (patch spec 17)."""

    simulated_experience_events: int = 0
    appraised_simulated_events: int = 0
    state_effect_changes: int = 0
    emotion_changes: int = 0
    memory_encoding_attempts: int = 0
    periodic_consolidation_runs: int = 0
    knowledge_sources: int = 0
    knowledge_candidates: int = 0
    knowledge_exposure_opportunities: int = 0

    @property
    def knowledge_sources_or_candidates(self) -> int:
        return self.knowledge_sources + self.knowledge_candidates


@dataclass(frozen=True, slots=True)
class GrowthMetrics:
    """Whether the growth pipeline was exercised (patch spec 17.1).

    Deliberately not "did personality change". A life that left someone the
    same is a possible life; a life whose growth machinery never ran is a
    broken pipeline. These measure the second thing.
    """

    adaptation_evidence_seen: int = 0
    adaptations_with_evidence: int = 0
    candidates_raised: int = 0
    deep_gate_evaluations: int = 0
    trait_history_entries: int = 0


@dataclass(frozen=True, slots=True)
class MemoryMetrics:
    """What the memory pipeline decided (patch spec 17.2)."""

    episodes: int = 0
    episodes_with_material: int = 0
    encoding_decisions: int = 0
    active_memories: int = 0
    discarded_episodes: int = 0


@dataclass(frozen=True, slots=True)
class KnowledgeMetrics:
    """Whether the knowledge layer exists at all (patch spec 17.3)."""

    sources: int = 0
    items: int = 0
    coverage_jobs: int = 0
    coverage_classes: int = 0
    exposure_opportunities: int = 0
    acquisitions: int = 0
    retained: int = 0


class HealthRepository:
    """Counts the FIRST BOOT audits read."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # --- pipeline (17) ------------------------------------------------------
    def pipeline_metrics(self, *, origin: str = SIMULATED_ORIGIN) -> PipelineMetrics:
        return PipelineMetrics(
            simulated_experience_events=self._count(
                "SELECT COUNT(*) FROM events WHERE event_type = ? AND origin = ?",
                (SIMULATED_EXPERIENCE, origin),
            ),
            # Evidence that the event was *read*, not merely processed.
            #
            # It has to be the emotion domain specifically. The world service
            # advances sleep pressure and the day on every event it sees, and
            # mood and needs drift towards their baselines on their own, so a
            # change in any of those proves only that a run happened. The
            # emotion engine returns nothing at all without an appraisal, so an
            # emotion change traceable to a simulated experience is proof the
            # reading happened — proof that outlives the run that made it.
            appraised_simulated_events=self._count(
                """
                SELECT COUNT(DISTINCT c.source_event_id)
                FROM state_changes c
                JOIN events e ON e.event_id = c.source_event_id
                WHERE e.event_type = ? AND e.origin = ? AND c.domain = 'emotion'
                """,
                (SIMULATED_EXPERIENCE, origin),
            ),
            state_effect_changes=self._count(
                """
                SELECT COUNT(*)
                FROM state_changes c
                JOIN events e ON e.event_id = c.source_event_id
                WHERE e.origin = ?
                """,
                (origin,),
            ),
            emotion_changes=self._count(
                """
                SELECT COUNT(*)
                FROM state_changes c
                JOIN events e ON e.event_id = c.source_event_id
                WHERE e.origin = ? AND c.domain = 'emotion'
                """,
                (origin,),
            ),
            memory_encoding_attempts=self._count(
                "SELECT COUNT(*) FROM episodes WHERE origin = ? AND status IN "
                "('encoded', 'discarded')",
                (origin,),
            ),
            periodic_consolidation_runs=self._count(
                "SELECT COUNT(*) FROM consolidation_runs WHERE kind LIKE ?",
                (f"{GENESIS_KIND_PREFIX}%",),
            ),
            knowledge_sources=self._count("SELECT COUNT(*) FROM knowledge_sources"),
            knowledge_candidates=self._count("SELECT COUNT(*) FROM external_knowledge"),
            knowledge_exposure_opportunities=self._count(
                "SELECT COUNT(*) FROM knowledge_exposure_opportunities"
            ),
        )

    # --- growth (17.1) ------------------------------------------------------
    def growth_metrics(self) -> GrowthMetrics:
        return GrowthMetrics(
            adaptation_evidence_seen=self._count(
                "SELECT COALESCE(SUM(supporting_count + contradicting_count), 0) "
                "FROM characteristic_adaptations"
            ),
            adaptations_with_evidence=self._count(
                "SELECT COUNT(*) FROM characteristic_adaptations "
                "WHERE last_evidence_at IS NOT NULL"
            ),
            candidates_raised=self._count("SELECT COUNT(*) FROM deep_update_candidates"),
            # A candidate that reached a terminal status was judged by the gate,
            # whichever way it went. Refusal is an evaluation.
            deep_gate_evaluations=self._count(
                "SELECT COUNT(*) FROM deep_update_candidates WHERE status != 'accumulating'"
            ),
            trait_history_entries=self._count("SELECT COUNT(*) FROM personality_history"),
        )

    # --- memory (17.2) ------------------------------------------------------
    def memory_metrics(self, *, origin: str | None = SIMULATED_ORIGIN) -> MemoryMetrics:
        where, params = ("WHERE origin = ?", (origin,)) if origin else ("", ())
        return MemoryMetrics(
            episodes=self._count(f"SELECT COUNT(*) FROM episodes {where}", params),
            episodes_with_material=self._count(
                f"SELECT COUNT(*) FROM episodes {where}{' AND' if where else 'WHERE'} "
                "event_count > 0",
                params,
            ),
            encoding_decisions=self._count(
                f"SELECT COUNT(*) FROM episodes {where}"
                f"{' AND' if where else 'WHERE'} status IN ('encoded', 'discarded')",
                params,
            ),
            active_memories=self._count(
                "SELECT COUNT(*) FROM episodic_memories WHERE status = 'active'"
                + (" AND origin = ?" if origin else ""),
                params,
            ),
            discarded_episodes=self._count(
                f"SELECT COUNT(*) FROM episodes {where}"
                f"{' AND' if where else 'WHERE'} status = 'discarded'",
                params,
            ),
        )

    # --- knowledge (17.3) ---------------------------------------------------
    def knowledge_metrics(self) -> KnowledgeMetrics:
        return KnowledgeMetrics(
            sources=self._count("SELECT COUNT(*) FROM knowledge_sources"),
            items=self._count("SELECT COUNT(*) FROM external_knowledge"),
            coverage_jobs=self._count("SELECT COUNT(*) FROM historical_coverage_jobs"),
            coverage_classes=self._count(
                "SELECT COUNT(DISTINCT coverage_class) FROM external_knowledge"
            ),
            exposure_opportunities=self._count(
                "SELECT COUNT(*) FROM knowledge_exposure_opportunities"
            ),
            acquisitions=self._count("SELECT COUNT(*) FROM knowledge_acquisitions"),
            retained=self._count(
                "SELECT COUNT(*) FROM knowledge_acquisitions WHERE status = 'retained'"
            ),
        )

    # --- legacy scan (23.3) -------------------------------------------------
    def events_by_origin(self, origins: Iterable[str]) -> int:
        values = tuple(origins)
        if not values:
            return 0
        placeholders = ", ".join("?" for _ in values)
        return self._count(
            f"SELECT COUNT(*) FROM events WHERE origin IN ({placeholders})", values
        )

    def events_after(self, moment: datetime, *, origins: Iterable[str]) -> int:
        values = tuple(origins)
        if not values:
            return 0
        placeholders = ", ".join("?" for _ in values)
        return self._count(
            f"SELECT COUNT(*) FROM events WHERE origin IN ({placeholders}) "
            "AND occurred_at >= ?",
            (*values, to_iso(moment)),
        )

    # --- blocks (18) --------------------------------------------------------
    def blocks_with_zero_event_count(self, simulation_id: str) -> int:
        return self._count(
            "SELECT COUNT(*) FROM simulation_blocks WHERE simulation_id = ? "
            "AND event_count <= 0",
            (simulation_id,),
        )

    def distinct_block_summaries(self, simulation_id: str) -> int:
        return self._count(
            "SELECT COUNT(DISTINCT summary) FROM simulation_blocks "
            "WHERE simulation_id = ? AND summary != ''",
            (simulation_id,),
        )

    def blocks_by_class(self, simulation_id: str) -> dict[str, int]:
        rows = self._db.query_all(
            "SELECT experience_class, COUNT(*) AS n FROM simulation_blocks "
            "WHERE simulation_id = ? GROUP BY experience_class",
            (simulation_id,),
        )
        return {str(row["experience_class"]): int(row["n"]) for row in rows}

    # --- helper -------------------------------------------------------------
    def _count(self, sql: str, params: tuple = ()) -> int:
        return int(self._db.scalar(sql, params) or 0)


__all__ = [
    "GrowthMetrics",
    "HealthRepository",
    "KnowledgeMetrics",
    "MemoryMetrics",
    "PipelineMetrics",
]
