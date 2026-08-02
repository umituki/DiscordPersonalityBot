"""Belief and self-model persistence (spec 12.4, 25, 31.5).

Written only by the Belief Engine and the Self Engine — their owned state
(spec 9.3). Nothing here decides anything; confidence and strength arrive
already computed by the engine's policy.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app import ids
from app.clock import from_iso, to_iso
from app.social.models import Belief, BeliefEvidence, PossibleSelf, SelfSchema
from app.storage.database import Database

BELIEF = "bel"
EVIDENCE = "ev"
SCHEMA = "self"
CONNECTION = "conn"
POSSIBLE = "poss"


class BeliefRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(
        self,
        *,
        statement: str,
        subject: str,
        origin: str,
        confidence: float,
        support_weight: float,
        contradiction_weight: float,
        status: str,
        now: datetime,
    ) -> Belief:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT belief_id FROM beliefs WHERE statement = ? AND subject = ? AND origin = ?",
                (statement, subject, origin),
            ).fetchone()
            if row is None:
                belief_id = ids.new_id(BELIEF)
                connection.execute(
                    """
                    INSERT INTO beliefs
                        (belief_id, statement, subject, origin, confidence, support_weight,
                         contradiction_weight, status, first_formed_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        belief_id, statement, subject, origin, confidence, support_weight,
                        contradiction_weight, status, to_iso(now), to_iso(now),
                    ),
                )
            else:
                belief_id = row["belief_id"]
                connection.execute(
                    "UPDATE beliefs SET confidence = ?, support_weight = ?, "
                    "contradiction_weight = ?, status = ?, updated_at = ?, "
                    "revision_count = revision_count + 1 WHERE belief_id = ?",
                    (
                        confidence, support_weight, contradiction_weight, status,
                        to_iso(now), belief_id,
                    ),
                )
            result = connection.execute(
                "SELECT * FROM beliefs WHERE belief_id = ?", (belief_id,)
            ).fetchone()
        return _to_belief(result)

    def add_evidence(
        self,
        *,
        belief_id: str,
        stance: str,
        weight: float,
        source_type: str,
        primary_source_id: str,
        event_id: str | None,
        now: datetime,
    ) -> bool:
        """Record evidence. The same primary source counts once (spec 25)."""
        cursor = self._db.execute(
            """
            INSERT OR IGNORE INTO belief_evidence
                (evidence_id, belief_id, stance, weight, source_type, primary_source_id,
                 event_id, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ids.new_id(EVIDENCE), belief_id, stance, weight, source_type,
                primary_source_id, event_id, to_iso(now),
            ),
        )
        return cursor.rowcount == 1

    def weights(self, belief_id: str) -> tuple[float, float]:
        """Total supporting and contradicting weight."""
        rows = self._db.query_all(
            "SELECT stance, SUM(weight) AS total FROM belief_evidence "
            "WHERE belief_id = ? GROUP BY stance",
            (belief_id,),
        )
        totals = {row["stance"]: float(row["total"] or 0.0) for row in rows}
        return totals.get("supports", 0.0), totals.get("contradicts", 0.0)

    def evidence_for(self, belief_id: str) -> list[BeliefEvidence]:
        rows = self._db.query_all(
            "SELECT * FROM belief_evidence WHERE belief_id = ? ORDER BY recorded_at",
            (belief_id,),
        )
        return [
            BeliefEvidence(
                evidence_id=row["evidence_id"],
                belief_id=row["belief_id"],
                stance=row["stance"],
                weight=row["weight"],
                source_type=row["source_type"],
                primary_source_id=row["primary_source_id"],
                event_id=row["event_id"],
                recorded_at=from_iso(row["recorded_at"]),
            )
            for row in rows
        ]

    def get(self, belief_id: str) -> Belief | None:
        row = self._db.query_one("SELECT * FROM beliefs WHERE belief_id = ?", (belief_id,))
        return None if row is None else _to_belief(row)

    def find(self, statement: str, subject: str, origin: str) -> Belief | None:
        row = self._db.query_one(
            "SELECT * FROM beliefs WHERE statement = ? AND subject = ? AND origin = ?",
            (statement, subject, origin),
        )
        return None if row is None else _to_belief(row)

    def held(self, *, subject: str | None = None, limit: int = 50) -> list[Belief]:
        if subject is None:
            rows = self._db.query_all(
                "SELECT * FROM beliefs WHERE status = 'held' "
                "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM beliefs WHERE status = 'held' AND subject = ? "
                "ORDER BY confidence DESC LIMIT ?",
                (subject, limit),
            )
        return [_to_belief(row) for row in rows]

    def set_status(self, belief_id: str, status: str, *, now: datetime) -> None:
        self._db.execute(
            "UPDATE beliefs SET status = ?, updated_at = ? WHERE belief_id = ?",
            (status, to_iso(now), belief_id),
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM beliefs") or 0)


class SelfRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def ensure(
        self, *, name: str, statement: str, strength: float, now: datetime
    ) -> SelfSchema:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT schema_id FROM self_schemas WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO self_schemas
                        (schema_id, name, statement, strength, clarity, first_formed_at,
                         updated_at)
                    VALUES (?, ?, ?, ?, 0.5, ?, ?)
                    """,
                    (ids.new_id(SCHEMA), name, statement, strength, to_iso(now), to_iso(now)),
                )
            result = connection.execute(
                "SELECT * FROM self_schemas WHERE name = ?", (name,)
            ).fetchone()
        return _to_schema(result)

    def update(
        self,
        *,
        schema_id: str,
        strength: float,
        clarity: float,
        supporting_count: int,
        contradicting_count: int,
        pending_evidence: float,
        last_behaviour_at: datetime | None,
        now: datetime,
    ) -> SelfSchema:
        self._db.execute(
            """
            UPDATE self_schemas
               SET strength = ?, clarity = ?, supporting_count = ?, contradicting_count = ?,
                   pending_evidence = ?, last_behaviour_at = ?, updated_at = ?
             WHERE schema_id = ?
            """,
            (
                strength, clarity, supporting_count, contradicting_count, pending_evidence,
                None if last_behaviour_at is None else to_iso(last_behaviour_at),
                to_iso(now), schema_id,
            ),
        )
        return self.get(schema_id)  # type: ignore[return-value]

    def connect_event(
        self, *, schema_id: str, event_id: str, relation: str, now: datetime
    ) -> bool:
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO self_event_connections "
            "(connection_id, schema_id, event_id, relation, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (ids.new_id(CONNECTION), schema_id, event_id, relation, to_iso(now)),
        )
        return cursor.rowcount == 1

    def get(self, schema_id: str) -> SelfSchema | None:
        row = self._db.query_one(
            "SELECT * FROM self_schemas WHERE schema_id = ?", (schema_id,)
        )
        return None if row is None else _to_schema(row)

    def by_name(self, name: str) -> SelfSchema | None:
        row = self._db.query_one("SELECT * FROM self_schemas WHERE name = ?", (name,))
        return None if row is None else _to_schema(row)

    def active(self, *, limit: int = 50) -> list[SelfSchema]:
        rows = self._db.query_all(
            "SELECT * FROM self_schemas WHERE status = 'active' "
            "ORDER BY strength DESC LIMIT ?",
            (limit,),
        )
        return [_to_schema(row) for row in rows]

    def connections(self, schema_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM self_event_connections WHERE schema_id = ? ORDER BY recorded_at",
            (schema_id,),
        )

    def add_possible_self(
        self,
        *,
        name: str,
        statement: str,
        valence: str,
        salience: float,
        now: datetime,
    ) -> PossibleSelf:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO possible_selves "
                "(possible_self_id, name, statement, valence, salience, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ids.new_id(POSSIBLE), name, statement, valence, salience,
                    to_iso(now), to_iso(now),
                ),
            )
            row = connection.execute(
                "SELECT * FROM possible_selves WHERE name = ?", (name,)
            ).fetchone()
        return PossibleSelf(
            possible_self_id=row["possible_self_id"],
            name=row["name"],
            statement=row["statement"],
            valence=row["valence"],
            salience=row["salience"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
            status=row["status"],
        )

    def possible_selves(self) -> list[PossibleSelf]:
        rows = self._db.query_all(
            "SELECT * FROM possible_selves WHERE status = 'active' ORDER BY salience DESC"
        )
        return [
            PossibleSelf(
                possible_self_id=row["possible_self_id"],
                name=row["name"],
                statement=row["statement"],
                valence=row["valence"],
                salience=row["salience"],
                created_at=from_iso(row["created_at"]),
                updated_at=from_iso(row["updated_at"]),
                status=row["status"],
            )
            for row in rows
        ]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM self_schemas") or 0)


def _to_belief(row: sqlite3.Row) -> Belief:
    return Belief(
        belief_id=row["belief_id"],
        statement=row["statement"],
        subject=row["subject"],
        origin=row["origin"],
        confidence=row["confidence"],
        support_weight=row["support_weight"],
        contradiction_weight=row["contradiction_weight"],
        status=row["status"],
        first_formed_at=from_iso(row["first_formed_at"]),
        updated_at=from_iso(row["updated_at"]),
        revision_count=int(row["revision_count"]),
    )


def _to_schema(row: sqlite3.Row) -> SelfSchema:
    return SelfSchema(
        schema_id=row["schema_id"],
        name=row["name"],
        statement=row["statement"],
        strength=row["strength"],
        clarity=row["clarity"],
        supporting_count=int(row["supporting_count"]),
        contradicting_count=int(row["contradicting_count"]),
        pending_evidence=row["pending_evidence"],
        last_behaviour_at=None
        if row["last_behaviour_at"] is None
        else from_iso(row["last_behaviour_at"]),
        first_formed_at=from_iso(row["first_formed_at"]),
        updated_at=from_iso(row["updated_at"]),
        status=row["status"],
    )
