"""Genesis v2 persistence (rebuild spec 34, 35 — Phase 12)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app import ids
from app.clock import from_iso, to_iso
from app.storage.database import Database

RUN = "gen"
CHECKPOINT = "ckp"
YEAR = "lyr"
MONTH = "lmo"
ENTITY = "ent"
SNAPSHOT = "esn"


class GenesisRunRepository:
    """The run, its stage, and the checkpoints it has reached (34.18, 34.19)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def start(
        self, *, birth: datetime, present: datetime, years: int, now: datetime
    ) -> str:
        run_id = ids.new_id(RUN)
        self._db.execute(
            "INSERT INTO genesis_runs (genesis_run_id, started_at, birth_datetime, "
            "present_datetime, years) VALUES (?, ?, ?, ?, ?)",
            (run_id, to_iso(now), to_iso(birth), to_iso(present), years),
        )
        return run_id

    def set_stage(self, run_id: str, stage: str) -> None:
        self._db.execute(
            "UPDATE genesis_runs SET stage = ? WHERE genesis_run_id = ?", (stage, run_id)
        )

    def finish(
        self, run_id: str, *, now: datetime, status: str = "complete", reason: str = ""
    ) -> None:
        self._db.execute(
            "UPDATE genesis_runs SET finished_at = ?, status = ?, failure_reason = ? "
            "WHERE genesis_run_id = ?",
            (to_iso(now), status, reason, run_id),
        )

    def get(self, run_id: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM genesis_runs WHERE genesis_run_id = ?", (run_id,)
        )

    def latest(self) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM genesis_runs ORDER BY started_at DESC LIMIT 1"
        )

    def unfinished(self) -> sqlite3.Row | None:
        """A run that stopped partway. What a resume picks up."""
        return self._db.query_one(
            "SELECT * FROM genesis_runs WHERE finished_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM genesis_runs") or 0)

    # --- checkpoints (34.18) -------------------------------------------------
    def checkpoint(
        self,
        run_id: str,
        *,
        name: str,
        now: datetime,
        year_number: int | None = None,
        detail: str = "",
    ) -> bool:
        """Record that a stage is done. Returns False if it already was.

        34.19: the uniqueness is the idempotency. A resume that reaches a
        checkpoint it already passed must not replay the work behind it, and
        the cheapest way to know is that writing the row fails.
        """
        try:
            self._db.execute(
                "INSERT INTO genesis_checkpoints (checkpoint_id, genesis_run_id, name, "
                "year_number, reached_at, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ids.new_id(CHECKPOINT), run_id, name, year_number,
                    to_iso(now), detail,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def reached(self, run_id: str, name: str, year_number: int | None = None) -> bool:
        row = self._db.query_one(
            "SELECT 1 FROM genesis_checkpoints WHERE genesis_run_id = ? AND name = ? "
            "AND COALESCE(year_number, -1) = COALESCE(?, -1)",
            (run_id, name, year_number),
        )
        return row is not None

    def checkpoints(self, run_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT name, year_number, reached_at, detail FROM genesis_checkpoints "
            "WHERE genesis_run_id = ? ORDER BY reached_at",
            (run_id,),
        )

    # --- anchors (34.1) ------------------------------------------------------
    def save_anchors(self, run_id: str, anchors: Any) -> None:
        self._db.execute(
            """
            INSERT OR REPLACE INTO life_anchors
                (genesis_run_id, birth_datetime, present_datetime, gender_identity,
                 embodiment, language, culture, home, family, social, education,
                 immutable_rules, temperament_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                to_iso(anchors.birth_datetime),
                to_iso(anchors.present_datetime),
                anchors.gender_identity,
                anchors.embodiment,
                anchors.language,
                anchors.culture,
                anchors.home,
                anchors.family,
                anchors.social,
                anchors.education,
                anchors.immutable_rules,
                json.dumps(anchors.temperament.as_dict(), ensure_ascii=False),
            ),
        )

    def anchors(self, run_id: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM life_anchors WHERE genesis_run_id = ?", (run_id,)
        )


class LifeRecordRepository:
    """Years and months: the objective life record, not her memory of it."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add_year(
        self,
        *,
        run_id: str,
        year_number: int,
        calendar_start: datetime,
        calendar_end: datetime,
        age_start: int,
        age_end: int,
        scaffold_text: str,
        prompt_version: str,
        model_version: str,
        now: datetime,
    ) -> str:
        existing = self.year(run_id, year_number)
        if existing is not None:
            return existing["year_id"]
        year_id = ids.new_id(YEAR)
        self._db.execute(
            """
            INSERT INTO life_years
                (year_id, genesis_run_id, year_number, calendar_start, calendar_end,
                 age_start, age_end, scaffold_text, prompt_version, model_version,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                year_id, run_id, year_number, to_iso(calendar_start),
                to_iso(calendar_end), age_start, age_end, scaffold_text,
                prompt_version, model_version, to_iso(now),
            ),
        )
        return year_id

    def synthesise(self, year_id: str, *, summary: str) -> None:
        """Stage C (34.6). The scaffold is kept, not overwritten.

        The months win when they disagree with the original sketch — and the
        sketch stays on the row, because "these two disagreed and the months
        were preferred" is a fact worth being able to see.
        """
        self._db.execute(
            "UPDATE life_years SET final_summary = ?, status = 'synthesised' "
            "WHERE year_id = ?",
            (summary, year_id),
        )

    def year(self, run_id: str, year_number: int) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM life_years WHERE genesis_run_id = ? AND year_number = ?",
            (run_id, year_number),
        )

    def years(self, run_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM life_years WHERE genesis_run_id = ? ORDER BY year_number",
            (run_id,),
        )

    def add_month(
        self,
        *,
        year_id: str,
        month_number: int,
        month_start: datetime,
        month_end: datetime,
        age_start: int,
        age_end: int,
        narrative: str,
        importance_class: str,
        prompt_version: str,
        model_version: str,
    ) -> str:
        existing = self.month(year_id, month_number)
        if existing is not None:
            return existing["month_id"]
        month_id = ids.new_id(MONTH)
        self._db.execute(
            """
            INSERT INTO life_months
                (month_id, year_id, month_number, month_start, month_end,
                 age_start, age_end, narrative, importance_class, status,
                 prompt_version, model_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'drafted', ?, ?)
            """,
            (
                month_id, year_id, month_number, to_iso(month_start),
                to_iso(month_end), age_start, age_end, narrative,
                importance_class, prompt_version, model_version,
            ),
        )
        return month_id

    def set_month_status(self, month_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE life_months SET status = ? WHERE month_id = ?", (status, month_id)
        )

    def month(self, year_id: str, month_number: int) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM life_months WHERE year_id = ? AND month_number = ?",
            (year_id, month_number),
        )

    def months(self, year_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM life_months WHERE year_id = ? ORDER BY month_number",
            (year_id,),
        )

    def years_overview(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """One row per year, with how many months it has. For the debug view."""
        rows = self._db.query_all(
            "SELECT y.year_number, y.age_start, y.age_end, y.status, y.year_id, "
            "COUNT(m.month_id) AS months, "
            "CASE WHEN y.final_summary != '' THEN 1 ELSE 0 END AS synthesised "
            "FROM life_years y LEFT JOIN life_months m ON m.year_id = y.year_id "
            "GROUP BY y.year_id ORDER BY y.year_number LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def month_count(self, *, importance_class: str | None = None) -> int:
        if importance_class is None:
            return int(self._db.scalar("SELECT COUNT(*) FROM life_months") or 0)
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM life_months WHERE importance_class = ?",
                (importance_class,),
            )
            or 0
        )

    def importance_spread(self) -> dict[str, int]:
        """How the months were classified. A run that is all `major` is wrong."""
        rows = self._db.query_all(
            "SELECT importance_class, COUNT(*) AS n FROM life_months "
            "GROUP BY importance_class"
        )
        return {row["importance_class"]: row["n"] for row in rows}


class LifeEntityRepository:
    """The continuity ledger's storage (34.7, 34.8)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(
        self,
        *,
        genesis_run_id: str,
        type: str,
        canonical_name: str,
        introduced_at: datetime,
        objective: dict[str, Any],
    ) -> str:
        row = self._db.query_one(
            "SELECT entity_id FROM life_entities WHERE genesis_run_id = ? "
            "AND type = ? AND canonical_name = ?",
            (genesis_run_id, type, canonical_name),
        )
        if row is not None:
            return row["entity_id"]
        entity_id = ids.new_id(ENTITY)
        self._db.execute(
            "INSERT INTO life_entities (entity_id, genesis_run_id, type, "
            "canonical_name, introduced_at, objective_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                entity_id, genesis_run_id, type, canonical_name,
                to_iso(introduced_at),
                json.dumps(objective, ensure_ascii=False, default=str),
            ),
        )
        return entity_id

    def touch(
        self, entity_id: str, *, seen_at: datetime, month_id: str | None = None
    ) -> None:
        self._db.execute(
            "UPDATE life_entities SET last_seen_at = ? WHERE entity_id = ?",
            (to_iso(seen_at), entity_id),
        )
        self._db.execute(
            "INSERT INTO life_entity_snapshots (snapshot_id, entity_id, month_id, "
            "recorded_at) VALUES (?, ?, ?, ?)",
            (ids.new_id(SNAPSHOT), entity_id, month_id, to_iso(seen_at)),
        )

    def retire(self, entity_id: str, *, retired_at: datetime, reason: str = "") -> None:
        self._db.execute(
            "UPDATE life_entities SET retired_at = ?, status = 'retired' "
            "WHERE entity_id = ?",
            (to_iso(retired_at), entity_id),
        )

    def link_npc(self, entity_id: str, npc_id: str) -> None:
        """Record which runtime NPC this person became (34.8)."""
        self._db.execute(
            "UPDATE life_entities SET npc_id = ? WHERE entity_id = ?",
            (npc_id, entity_id),
        )

    def npc_for(self, entity_id: str) -> str | None:
        """Which runtime NPC this person became, if any. Point 7's idempotency."""
        row = self._db.query_one(
            "SELECT npc_id FROM life_entities WHERE entity_id = ?", (entity_id,)
        )
        return None if row is None else row["npc_id"]

    def all_for(self, genesis_run_id: str) -> list[dict[str, Any]]:
        rows = self._db.query_all(
            "SELECT * FROM life_entities WHERE genesis_run_id = ? "
            "ORDER BY introduced_at",
            (genesis_run_id,),
        )
        return [
            {
                "entity_id": row["entity_id"],
                "type": row["type"],
                "canonical_name": row["canonical_name"],
                "introduced_at": from_iso(row["introduced_at"]),
                "last_seen_at": (
                    None if row["last_seen_at"] is None else from_iso(row["last_seen_at"])
                ),
                "status": row["status"],
                "objective_json": row["objective_json"],
                "npc_id": row["npc_id"],
            }
            for row in rows
        ]

    def count(self, *, type: str | None = None) -> int:
        if type is None:
            return int(self._db.scalar("SELECT COUNT(*) FROM life_entities") or 0)
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM life_entities WHERE type = ?", (type,)
            )
            or 0
        )


class GenesisExperienceRepository:
    """Extracted experiences, with how far replay got (34.11, 34.19).

    The granularity that matters. Extraction and replay used to be one
    in-memory pass, so a crash during replay left the year checkpoint unwritten
    and resuming replayed every experience in it — she lived the same fortnight
    twice. Now each candidate is a row, replay status lives on the row, and a
    resume takes the ones still `pending`.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def stage(
        self,
        *,
        genesis_run_id: str,
        month_id: str,
        year_number: int,
        sequence: int,
        candidate: Any,
    ) -> str:
        """Record a candidate. Idempotent on (month, sequence).

        A month re-extracted after a crash produces the same rows rather than
        a second set, which is what makes re-extraction safe to retry.
        """
        existing = self._db.query_one(
            "SELECT experience_id FROM genesis_experiences WHERE month_id = ? "
            "AND sequence = ?",
            (month_id, sequence),
        )
        if existing is not None:
            return existing["experience_id"]
        experience_id = ids.new_id("exp")
        self._db.execute(
            """
            INSERT INTO genesis_experiences
                (experience_id, genesis_run_id, month_id, year_number, sequence,
                 occurred_at, actors, context, action, outcome,
                 social_significance, importance, compressed, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experience_id, genesis_run_id, month_id, year_number, sequence,
                to_iso(candidate.occurred_at), ",".join(candidate.actors),
                candidate.context, candidate.action, candidate.outcome,
                candidate.social_significance, candidate.importance,
                1 if candidate.compressed else 0, candidate.confidence,
            ),
        )
        return experience_id

    def mark_replayed(self, experience_id: str, *, event_id: str, now: datetime) -> None:
        self._db.execute(
            "UPDATE genesis_experiences SET replay_status = 'replayed', "
            "event_id = ?, replayed_at = ? WHERE experience_id = ?",
            (event_id, to_iso(now), experience_id),
        )

    def pending(
        self, genesis_run_id: str, *, year_number: int | None = None, limit: int = 5000
    ) -> list[sqlite3.Row]:
        """What still has to be lived. The only thing a resume replays."""
        if year_number is None:
            return self._db.query_all(
                "SELECT * FROM genesis_experiences WHERE genesis_run_id = ? "
                "AND replay_status = 'pending' ORDER BY occurred_at, sequence LIMIT ?",
                (genesis_run_id, limit),
            )
        return self._db.query_all(
            "SELECT * FROM genesis_experiences WHERE genesis_run_id = ? "
            "AND year_number = ? AND replay_status = 'pending' "
            "ORDER BY occurred_at, sequence LIMIT ?",
            (genesis_run_id, year_number, limit),
        )

    def for_year(self, genesis_run_id: str, year_number: int) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM genesis_experiences WHERE genesis_run_id = ? "
            "AND year_number = ? ORDER BY occurred_at, sequence",
            (genesis_run_id, year_number),
        )

    def for_month(self, month_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM genesis_experiences WHERE month_id = ? ORDER BY sequence",
            (month_id,),
        )

    def count(
        self, *, genesis_run_id: str | None = None, replay_status: str | None = None
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if genesis_run_id is not None:
            clauses.append("genesis_run_id = ?")
            params.append(genesis_run_id)
        if replay_status is not None:
            clauses.append("replay_status = ?")
            params.append(replay_status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return int(
            self._db.scalar(f"SELECT COUNT(*) FROM genesis_experiences{where}", tuple(params))
            or 0
        )

    def recent(self, *, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT occurred_at, year_number, importance, action, replay_status, "
            "replayed_at FROM genesis_experiences ORDER BY occurred_at DESC LIMIT ?",
            (limit,),
        )

    def replayed_event_ids(self, genesis_run_id: str) -> list[str]:
        rows = self._db.query_all(
            "SELECT event_id FROM genesis_experiences WHERE genesis_run_id = ? "
            "AND replay_status = 'replayed' AND event_id IS NOT NULL",
            (genesis_run_id,),
        )
        return [row["event_id"] for row in rows]


class GenerationAuditRepository:
    """Every critic verdict, including — especially — the failures."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        audit_id: str,
        genesis_run_id: str | None,
        target_type: str,
        target_id: str,
        critic_type: str,
        passed: bool,
        severity: str,
        issues_json: str,
        now: datetime,
        repaired: bool = False,
        model_version: str = "",
        prompt_version: str = "",
    ) -> None:
        self._db.execute(
            """
            INSERT INTO generation_audits
                (audit_id, genesis_run_id, target_type, target_id, critic_type,
                 passed, severity, issues_json, repaired, model_version,
                 prompt_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id, genesis_run_id, target_type, target_id, critic_type,
                1 if passed else 0, severity, issues_json, 1 if repaired else 0,
                model_version, prompt_version, to_iso(now),
            ),
        )

    def mark_repaired(self, target_id: str) -> None:
        self._db.execute(
            "UPDATE generation_audits SET repaired = 1 WHERE target_id = ? AND passed = 0",
            (target_id,),
        )

    def recent(self, *, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT created_at, target_type, target_id, critic_type, passed, "
            "severity, repaired FROM generation_audits ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    def failures(self, *, unrepaired_only: bool = True) -> list[sqlite3.Row]:
        """GEN-CRITIC-001's query: what failed and was never dealt with."""
        clause = " AND repaired = 0" if unrepaired_only else ""
        return self._db.query_all(
            "SELECT * FROM generation_audits WHERE passed = 0 "
            "AND severity IN ('high', 'fatal')" + clause + " ORDER BY created_at",
        )

    def count(self, *, passed: bool | None = None) -> int:
        if passed is None:
            return int(self._db.scalar("SELECT COUNT(*) FROM generation_audits") or 0)
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM generation_audits WHERE passed = ?",
                (1 if passed else 0,),
            )
            or 0
        )


__all__ = [
    "GenerationAuditRepository",
    "GenesisExperienceRepository",
    "GenesisRunRepository",
    "LifeEntityRepository",
    "LifeRecordRepository",
]
