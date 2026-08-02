"""Historical knowledge persistence (spec 21, 31.8)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app import ids
from app.clock import from_iso, to_iso
from app.knowledge.models import (
    Acquisition,
    CoverageJob,
    ExposureOpportunity,
    KnowledgeItem,
    KnowledgeSource,
)
from app.storage.database import Database

SOURCE = "src"
KNOWLEDGE = "knw"
VERSION = "kvr"
OPPORTUNITY = "exo"
ACQUISITION = "acq"
COVERAGE = "cvj"


class KnowledgeRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- sources -----------------------------------------------------------
    def add_source(
        self,
        *,
        name: str,
        kind: str = "reference",
        url: str | None = None,
        published_at: datetime | None = None,
        reliability: float = 0.5,
        now: datetime,
    ) -> KnowledgeSource:
        source_id = ids.new_id(SOURCE)
        self._db.execute(
            """
            INSERT INTO knowledge_sources
                (source_id, name, kind, url, published_at, reliability, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id, name, kind, url,
                None if published_at is None else to_iso(published_at),
                reliability, to_iso(now),
            ),
        )
        return self.source(source_id)  # type: ignore[return-value]

    def source(self, source_id: str) -> KnowledgeSource | None:
        row = self._db.query_one(
            "SELECT * FROM knowledge_sources WHERE source_id = ?", (source_id,)
        )
        return None if row is None else _to_source(row)

    # --- knowledge ---------------------------------------------------------
    def add_knowledge(self, item: KnowledgeItem) -> KnowledgeItem:
        self._db.execute(
            """
            INSERT INTO external_knowledge
                (knowledge_id, statement, coverage_class, topic, geography, language,
                 available_from, available_until, valid_from, valid_until,
                 source_published_at, stability, truth_confidence, complexity, salience,
                 source_id, version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.knowledge_id, item.statement, item.coverage_class, item.topic,
                item.geography, item.language, to_iso(item.available_from),
                None if item.available_until is None else to_iso(item.available_until),
                None if item.valid_from is None else to_iso(item.valid_from),
                None if item.valid_until is None else to_iso(item.valid_until),
                None if item.source_published_at is None else to_iso(item.source_published_at),
                item.stability, item.truth_confidence, item.complexity, item.salience,
                item.source_id, item.version, to_iso(item.created_at),
            ),
        )
        return self.knowledge(item.knowledge_id)  # type: ignore[return-value]

    def knowledge(self, knowledge_id: str) -> KnowledgeItem | None:
        row = self._db.query_one(
            "SELECT * FROM external_knowledge WHERE knowledge_id = ?", (knowledge_id,)
        )
        return None if row is None else _to_knowledge(row)

    def by_statement(self, statement: str) -> KnowledgeItem | None:
        row = self._db.query_one(
            "SELECT * FROM external_knowledge WHERE statement = ? "
            "ORDER BY available_from LIMIT 1",
            (statement,),
        )
        return None if row is None else _to_knowledge(row)

    def available_at(
        self,
        moment: datetime,
        *,
        coverage_class: str | None = None,
        topic: str | None = None,
        limit: int = 200,
    ) -> list[KnowledgeItem]:
        """Only what was already public. The SQL is the first line of defence.

        The temporal guard checks every candidate again before exposure — this
        query is an optimisation, never the safety property (spec 21.4).
        """
        clauses = ["available_from <= ?", "(available_until IS NULL OR available_until > ?)"]
        params: list[Any] = [to_iso(moment), to_iso(moment)]
        if coverage_class:
            clauses.append("coverage_class = ?")
            params.append(coverage_class)
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        params.append(limit)
        rows = self._db.query_all(
            f"SELECT * FROM external_knowledge WHERE {' AND '.join(clauses)} "
            "ORDER BY salience DESC LIMIT ?",
            tuple(params),
        )
        return [_to_knowledge(row) for row in rows]

    def all_knowledge(self, *, limit: int = 500) -> list[KnowledgeItem]:
        rows = self._db.query_all(
            "SELECT * FROM external_knowledge ORDER BY available_from LIMIT ?", (limit,)
        )
        return [_to_knowledge(row) for row in rows]

    def record_version(
        self,
        *,
        knowledge_id: str,
        version: int,
        statement: str,
        valid_from: datetime | None,
        valid_until: datetime | None,
        truth_confidence: float,
        reason: str,
        now: datetime,
    ) -> str:
        version_id = ids.new_id(VERSION)
        self._db.execute(
            """
            INSERT INTO knowledge_versions
                (version_id, knowledge_id, version, statement, valid_from, valid_until,
                 truth_confidence, reason, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id, knowledge_id, version, statement,
                None if valid_from is None else to_iso(valid_from),
                None if valid_until is None else to_iso(valid_until),
                truth_confidence, reason, to_iso(now),
            ),
        )
        return version_id

    def versions(self, knowledge_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM knowledge_versions WHERE knowledge_id = ? ORDER BY version",
            (knowledge_id,),
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM external_knowledge") or 0)


class ExposureRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        knowledge_id: str,
        occurred_at: datetime,
        channel: str,
        salience: float,
        reach: float,
        stage_reached: str,
        acquired: bool,
        reason: str,
        simulation_block_id: str | None,
        now: datetime,
    ) -> ExposureOpportunity:
        opportunity_id = ids.new_id(OPPORTUNITY)
        self._db.execute(
            """
            INSERT INTO knowledge_exposure_opportunities
                (opportunity_id, knowledge_id, occurred_at, channel, salience, reach,
                 stage_reached, acquired, reason, simulation_block_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity_id, knowledge_id, to_iso(occurred_at), channel, salience, reach,
                stage_reached, 1 if acquired else 0, reason, simulation_block_id, to_iso(now),
            ),
        )
        return self.get(opportunity_id)  # type: ignore[return-value]

    def get(self, opportunity_id: str) -> ExposureOpportunity | None:
        row = self._db.query_one(
            "SELECT * FROM knowledge_exposure_opportunities WHERE opportunity_id = ?",
            (opportunity_id,),
        )
        return None if row is None else _to_opportunity(row)

    def for_knowledge(self, knowledge_id: str, *, limit: int = 50) -> list[ExposureOpportunity]:
        rows = self._db.query_all(
            "SELECT * FROM knowledge_exposure_opportunities WHERE knowledge_id = ? "
            "ORDER BY occurred_at LIMIT ?",
            (knowledge_id, limit),
        )
        return [_to_opportunity(row) for row in rows]

    def recent(self, *, limit: int = 100) -> list[ExposureOpportunity]:
        rows = self._db.query_all(
            "SELECT * FROM knowledge_exposure_opportunities ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        )
        return [_to_opportunity(row) for row in rows]

    def latest_occurred_at(self) -> datetime | None:
        value = self._db.scalar(
            "SELECT MAX(occurred_at) FROM knowledge_exposure_opportunities"
        )
        return None if value is None else from_iso(value)

    def count(self, *, acquired: bool | None = None) -> int:
        if acquired is None:
            return int(
                self._db.scalar("SELECT COUNT(*) FROM knowledge_exposure_opportunities") or 0
            )
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM knowledge_exposure_opportunities WHERE acquired = ?",
                (1 if acquired else 0,),
            )
            or 0
        )


class AcquisitionRepository:
    """The only place that can say YUI knows something (spec 21.1)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        knowledge_id: str,
        opportunity_id: str | None,
        acquired_at: datetime,
        comprehension: float,
        retention: float,
        semantic_memory_id: str | None = None,
        origin: str = "simulated_past",
    ) -> Acquisition:
        acquisition_id = ids.new_id(ACQUISITION)
        self._db.execute(
            """
            INSERT OR IGNORE INTO knowledge_acquisitions
                (acquisition_id, knowledge_id, opportunity_id, acquired_at, comprehension,
                 retention, semantic_memory_id, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                acquisition_id, knowledge_id, opportunity_id, to_iso(acquired_at),
                comprehension, retention, semantic_memory_id, origin,
            ),
        )
        return self.for_knowledge(knowledge_id)  # type: ignore[return-value]

    def set_status(self, acquisition_id: str, status: str, *, retention: float) -> None:
        self._db.execute(
            "UPDATE knowledge_acquisitions SET status = ?, retention = ? WHERE acquisition_id = ?",
            (status, retention, acquisition_id),
        )

    def for_knowledge(self, knowledge_id: str) -> Acquisition | None:
        row = self._db.query_one(
            "SELECT * FROM knowledge_acquisitions WHERE knowledge_id = ? "
            "ORDER BY acquired_at DESC LIMIT 1",
            (knowledge_id,),
        )
        return None if row is None else _to_acquisition(row)

    def all(self, *, status: str | None = None, limit: int = 500) -> list[Acquisition]:
        if status is None:
            rows = self._db.query_all(
                "SELECT * FROM knowledge_acquisitions ORDER BY acquired_at LIMIT ?", (limit,)
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM knowledge_acquisitions WHERE status = ? "
                "ORDER BY acquired_at LIMIT ?",
                (status, limit),
            )
        return [_to_acquisition(row) for row in rows]

    def count(self, *, status: str | None = None) -> int:
        if status is None:
            return int(self._db.scalar("SELECT COUNT(*) FROM knowledge_acquisitions") or 0)
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM knowledge_acquisitions WHERE status = ?", (status,)
            )
            or 0
        )


class CoverageJobRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        *,
        coverage_class: str,
        period_start: datetime,
        period_end: datetime,
        now: datetime,
    ) -> CoverageJob:
        job_id = ids.new_id(COVERAGE)
        self._db.execute(
            """
            INSERT INTO historical_coverage_jobs
                (job_id, coverage_class, period_start, period_end, requested_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, coverage_class, to_iso(period_start), to_iso(period_end), to_iso(now)),
        )
        return self.get(job_id)  # type: ignore[return-value]

    def complete(
        self, job_id: str, *, produced: int, detail: dict[str, Any], now: datetime,
        status: str = "completed",
    ) -> CoverageJob:
        self._db.execute(
            "UPDATE historical_coverage_jobs SET status = ?, completed_at = ?, "
            "produced_count = ?, detail_json = ? WHERE job_id = ?",
            (
                status, to_iso(now), produced,
                json.dumps(detail, ensure_ascii=False, default=str), job_id,
            ),
        )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> CoverageJob | None:
        row = self._db.query_one(
            "SELECT * FROM historical_coverage_jobs WHERE job_id = ?", (job_id,)
        )
        return None if row is None else _to_job(row)

    def pending(self, *, limit: int = 50) -> list[CoverageJob]:
        rows = self._db.query_all(
            "SELECT * FROM historical_coverage_jobs WHERE status = 'pending' "
            "ORDER BY requested_at LIMIT ?",
            (limit,),
        )
        return [_to_job(row) for row in rows]

    def all(self, *, limit: int = 100) -> list[CoverageJob]:
        rows = self._db.query_all(
            "SELECT * FROM historical_coverage_jobs ORDER BY requested_at LIMIT ?", (limit,)
        )
        return [_to_job(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM historical_coverage_jobs") or 0)


# --- row mapping -----------------------------------------------------------
def _optional_dt(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


def _to_source(row: sqlite3.Row) -> KnowledgeSource:
    return KnowledgeSource(
        source_id=row["source_id"],
        name=row["name"],
        kind=row["kind"],
        url=row["url"],
        published_at=_optional_dt(row["published_at"]),
        reliability=row["reliability"],
        created_at=from_iso(row["created_at"]),
    )


def _to_knowledge(row: sqlite3.Row) -> KnowledgeItem:
    return KnowledgeItem(
        knowledge_id=row["knowledge_id"],
        statement=row["statement"],
        coverage_class=row["coverage_class"],
        topic=row["topic"],
        geography=row["geography"],
        language=row["language"],
        available_from=from_iso(row["available_from"]),
        available_until=_optional_dt(row["available_until"]),
        valid_from=_optional_dt(row["valid_from"]),
        valid_until=_optional_dt(row["valid_until"]),
        source_published_at=_optional_dt(row["source_published_at"]),
        stability=row["stability"],
        truth_confidence=row["truth_confidence"],
        complexity=row["complexity"],
        salience=row["salience"],
        source_id=row["source_id"],
        version=int(row["version"]),
        created_at=from_iso(row["created_at"]),
    )


def _to_opportunity(row: sqlite3.Row) -> ExposureOpportunity:
    return ExposureOpportunity(
        opportunity_id=row["opportunity_id"],
        knowledge_id=row["knowledge_id"],
        occurred_at=from_iso(row["occurred_at"]),
        channel=row["channel"],
        salience=row["salience"],
        reach=row["reach"],
        stage_reached=row["stage_reached"],
        acquired=bool(row["acquired"]),
        reason=row["reason"],
        simulation_block_id=row["simulation_block_id"],
        created_at=from_iso(row["created_at"]),
    )


def _to_acquisition(row: sqlite3.Row) -> Acquisition:
    return Acquisition(
        acquisition_id=row["acquisition_id"],
        knowledge_id=row["knowledge_id"],
        opportunity_id=row["opportunity_id"],
        acquired_at=from_iso(row["acquired_at"]),
        comprehension=row["comprehension"],
        retention=row["retention"],
        status=row["status"],
        semantic_memory_id=row["semantic_memory_id"],
        origin=row["origin"],
    )


def _to_job(row: sqlite3.Row) -> CoverageJob:
    return CoverageJob(
        job_id=row["job_id"],
        coverage_class=row["coverage_class"],
        period_start=from_iso(row["period_start"]),
        period_end=from_iso(row["period_end"]),
        status=row["status"],
        requested_at=from_iso(row["requested_at"]),
        completed_at=_optional_dt(row["completed_at"]),
        produced_count=int(row["produced_count"]),
    )
