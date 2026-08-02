"""Past simulation persistence (spec 22, 31.8)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Sequence

from app import ids
from app.clock import from_iso, to_iso
from app.simulation.models import (
    GenesisAudit,
    LifePhase,
    LifeScaffold,
    SimulationBlock,
    SimulationRun,
    TemperamentSeed,
)
from app.storage.database import Database

SCAFFOLD = "scf"
SIMULATION = "sim"
PHASE = "phs"
BLOCK = "blk"
AUDIT = "aud"


class SimulationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- seed and scaffold --------------------------------------------------
    def save_seed(self, seed: TemperamentSeed) -> TemperamentSeed:
        self._db.execute(
            """
            INSERT INTO temperament_seeds
                (seed_id, answers_json, temperament_json, avoid_json, interests_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                seed.seed_id,
                json.dumps(seed.answers, ensure_ascii=False),
                json.dumps(seed.temperament, ensure_ascii=False),
                json.dumps(list(seed.avoid), ensure_ascii=False),
                json.dumps(list(seed.interests), ensure_ascii=False),
                to_iso(seed.created_at),
            ),
        )
        return self.seed(seed.seed_id)  # type: ignore[return-value]

    def seed(self, seed_id: str) -> TemperamentSeed | None:
        row = self._db.query_one(
            "SELECT * FROM temperament_seeds WHERE seed_id = ?", (seed_id,)
        )
        return None if row is None else _to_seed(row)

    def latest_seed(self) -> TemperamentSeed | None:
        row = self._db.query_one(
            "SELECT * FROM temperament_seeds ORDER BY created_at DESC LIMIT 1"
        )
        return None if row is None else _to_seed(row)

    def save_scaffold(
        self,
        *,
        seed_id: str,
        period_start: datetime,
        period_end: datetime,
        environment: str,
        education_context: str,
        social_density: float,
        technology_availability: float,
        life_stage: str,
        now: datetime,
    ) -> LifeScaffold:
        scaffold_id = ids.new_id(SCAFFOLD)
        self._db.execute(
            """
            INSERT INTO life_scaffolds
                (scaffold_id, seed_id, period_start, period_end, environment,
                 education_context, social_density, technology_availability, life_stage,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scaffold_id, seed_id, to_iso(period_start), to_iso(period_end), environment,
                education_context, social_density, technology_availability, life_stage,
                to_iso(now),
            ),
        )
        return self.scaffold(scaffold_id)  # type: ignore[return-value]

    def scaffold(self, scaffold_id: str) -> LifeScaffold | None:
        row = self._db.query_one(
            "SELECT * FROM life_scaffolds WHERE scaffold_id = ?", (scaffold_id,)
        )
        return None if row is None else _to_scaffold(row)

    # --- runs ---------------------------------------------------------------
    def start_run(
        self,
        *,
        seed_id: str,
        scaffold_id: str,
        simulated_from: datetime,
        simulated_to: datetime,
        now: datetime,
    ) -> SimulationRun:
        simulation_id = ids.new_id(SIMULATION)
        self._db.execute(
            """
            INSERT INTO simulation_runs
                (simulation_id, seed_id, scaffold_id, started_at, simulated_from, simulated_to)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                simulation_id, seed_id, scaffold_id, to_iso(now),
                to_iso(simulated_from), to_iso(simulated_to),
            ),
        )
        return self.run(simulation_id)  # type: ignore[return-value]

    def finish_run(
        self,
        simulation_id: str,
        *,
        status: str,
        blocks_run: int,
        experiences: int,
        detail: dict[str, Any],
        now: datetime,
    ) -> SimulationRun:
        self._db.execute(
            "UPDATE simulation_runs SET status = ?, ended_at = ?, blocks_run = ?, "
            "experiences = ?, detail_json = ? WHERE simulation_id = ?",
            (
                status, to_iso(now), blocks_run, experiences,
                json.dumps(detail, ensure_ascii=False, default=str), simulation_id,
            ),
        )
        return self.run(simulation_id)  # type: ignore[return-value]

    def record_first_boot(self, simulation_id: str, *, now: datetime) -> SimulationRun:
        self._db.execute(
            "UPDATE simulation_runs SET first_boot_at = ? WHERE simulation_id = ?",
            (to_iso(now), simulation_id),
        )
        return self.run(simulation_id)  # type: ignore[return-value]

    def run(self, simulation_id: str) -> SimulationRun | None:
        row = self._db.query_one(
            "SELECT * FROM simulation_runs WHERE simulation_id = ?", (simulation_id,)
        )
        return None if row is None else _to_run(row)

    def latest_run(self) -> SimulationRun | None:
        row = self._db.query_one(
            "SELECT * FROM simulation_runs ORDER BY started_at DESC LIMIT 1"
        )
        return None if row is None else _to_run(row)

    def booted_run(self) -> SimulationRun | None:
        row = self._db.query_one(
            "SELECT * FROM simulation_runs WHERE first_boot_at IS NOT NULL "
            "ORDER BY first_boot_at LIMIT 1"
        )
        return None if row is None else _to_run(row)

    def runs(self, *, limit: int = 20) -> list[SimulationRun]:
        rows = self._db.query_all(
            "SELECT * FROM simulation_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [_to_run(row) for row in rows]

    # --- phases and blocks --------------------------------------------------
    def add_phase(
        self,
        *,
        simulation_id: str,
        name: str,
        started_at: datetime,
        ended_at: datetime,
        summary: str,
        ordinal: int,
    ) -> LifePhase:
        phase_id = ids.new_id(PHASE)
        self._db.execute(
            """
            INSERT INTO life_phases
                (phase_id, simulation_id, name, started_at, ended_at, summary, ordinal)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                phase_id, simulation_id, name, to_iso(started_at), to_iso(ended_at),
                summary, ordinal,
            ),
        )
        return self.phase(phase_id)  # type: ignore[return-value]

    def phase(self, phase_id: str) -> LifePhase | None:
        row = self._db.query_one("SELECT * FROM life_phases WHERE phase_id = ?", (phase_id,))
        return None if row is None else _to_phase(row)

    def phases(self, simulation_id: str) -> list[LifePhase]:
        rows = self._db.query_all(
            "SELECT * FROM life_phases WHERE simulation_id = ? ORDER BY ordinal",
            (simulation_id,),
        )
        return [_to_phase(row) for row in rows]

    def add_block(
        self,
        *,
        simulation_id: str,
        phase_id: str | None,
        started_at: datetime,
        ended_at: datetime,
        detail_level: str,
        experience_class: str,
        summary: str,
        event_count: int,
        ordinal: int,
    ) -> SimulationBlock:
        block_id = ids.new_id(BLOCK)
        self._db.execute(
            """
            INSERT INTO simulation_blocks
                (block_id, simulation_id, phase_id, started_at, ended_at, detail_level,
                 experience_class, summary, event_count, ordinal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                block_id, simulation_id, phase_id, to_iso(started_at), to_iso(ended_at),
                detail_level, experience_class, summary, event_count, ordinal,
            ),
        )
        return self.block(block_id)  # type: ignore[return-value]

    def set_block_summary(self, block_id: str, summary: str) -> None:
        self._db.execute(
            "UPDATE simulation_blocks SET summary = ? WHERE block_id = ?",
            (summary, block_id),
        )

    def block(self, block_id: str) -> SimulationBlock | None:
        row = self._db.query_one(
            "SELECT * FROM simulation_blocks WHERE block_id = ?", (block_id,)
        )
        return None if row is None else _to_block(row)

    def blocks(self, simulation_id: str, *, limit: int = 1000) -> list[SimulationBlock]:
        rows = self._db.query_all(
            "SELECT * FROM simulation_blocks WHERE simulation_id = ? ORDER BY ordinal LIMIT ?",
            (simulation_id, limit),
        )
        return [_to_block(row) for row in rows]

    def block_count(self, simulation_id: str) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM simulation_blocks WHERE simulation_id = ?",
                (simulation_id,),
            )
            or 0
        )

    # --- audits -------------------------------------------------------------
    def record_audit(
        self,
        *,
        simulation_id: str,
        kind: str,
        passed: bool,
        detail: dict[str, Any],
        now: datetime,
    ) -> GenesisAudit:
        audit_id = ids.new_id(AUDIT)
        self._db.execute(
            """
            INSERT INTO genesis_audits
                (audit_id, simulation_id, kind, passed, detail_json, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id, simulation_id, kind, 1 if passed else 0,
                json.dumps(detail, ensure_ascii=False, default=str), to_iso(now),
            ),
        )
        return self.audit(audit_id)  # type: ignore[return-value]

    def audit(self, audit_id: str) -> GenesisAudit | None:
        row = self._db.query_one("SELECT * FROM genesis_audits WHERE audit_id = ?", (audit_id,))
        return None if row is None else _to_audit(row)

    def audits(self, simulation_id: str) -> list[GenesisAudit]:
        rows = self._db.query_all(
            "SELECT * FROM genesis_audits WHERE simulation_id = ? ORDER BY recorded_at",
            (simulation_id,),
        )
        return [_to_audit(row) for row in rows]

    def latest_audits(self, simulation_id: str, kinds: Sequence[str]) -> dict[str, GenesisAudit]:
        latest: dict[str, GenesisAudit] = {}
        for audit in self.audits(simulation_id):
            if audit.kind in kinds:
                latest[audit.kind] = audit
        return latest


# --- row mapping -----------------------------------------------------------
def _optional_dt(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


def _to_seed(row: sqlite3.Row) -> TemperamentSeed:
    return TemperamentSeed(
        seed_id=row["seed_id"],
        answers=json.loads(row["answers_json"] or "{}"),
        temperament=json.loads(row["temperament_json"] or "{}"),
        avoid=tuple(json.loads(row["avoid_json"] or "[]")),
        interests=tuple(json.loads(row["interests_json"] or "[]")),
        created_at=from_iso(row["created_at"]),
    )


def _to_scaffold(row: sqlite3.Row) -> LifeScaffold:
    return LifeScaffold(
        scaffold_id=row["scaffold_id"],
        seed_id=row["seed_id"],
        period_start=from_iso(row["period_start"]),
        period_end=from_iso(row["period_end"]),
        environment=row["environment"],
        education_context=row["education_context"],
        social_density=row["social_density"],
        technology_availability=row["technology_availability"],
        life_stage=row["life_stage"],
        created_at=from_iso(row["created_at"]),
    )


def _to_run(row: sqlite3.Row) -> SimulationRun:
    return SimulationRun(
        simulation_id=row["simulation_id"],
        seed_id=row["seed_id"],
        scaffold_id=row["scaffold_id"],
        status=row["status"],
        started_at=from_iso(row["started_at"]),
        ended_at=_optional_dt(row["ended_at"]),
        simulated_from=from_iso(row["simulated_from"]),
        simulated_to=from_iso(row["simulated_to"]),
        blocks_run=int(row["blocks_run"]),
        experiences=int(row["experiences"]),
        first_boot_at=_optional_dt(row["first_boot_at"]),
    )


def _to_phase(row: sqlite3.Row) -> LifePhase:
    return LifePhase(
        phase_id=row["phase_id"],
        simulation_id=row["simulation_id"],
        name=row["name"],
        started_at=from_iso(row["started_at"]),
        ended_at=from_iso(row["ended_at"]),
        summary=row["summary"],
        ordinal=int(row["ordinal"]),
    )


def _to_block(row: sqlite3.Row) -> SimulationBlock:
    return SimulationBlock(
        block_id=row["block_id"],
        simulation_id=row["simulation_id"],
        phase_id=row["phase_id"],
        started_at=from_iso(row["started_at"]),
        ended_at=from_iso(row["ended_at"]),
        detail_level=row["detail_level"],
        experience_class=row["experience_class"],
        summary=row["summary"],
        event_count=int(row["event_count"]),
        ordinal=int(row["ordinal"]),
    )


def _to_audit(row: sqlite3.Row) -> GenesisAudit:
    return GenesisAudit(
        audit_id=row["audit_id"],
        simulation_id=row["simulation_id"],
        kind=row["kind"],
        passed=bool(row["passed"]),
        detail=json.loads(row["detail_json"] or "{}"),
        recorded_at=from_iso(row["recorded_at"]),
    )
