"""Shadow decision persistence (rebuild spec 47 — Phase 14).

Every time one of the four dangerous capabilities asked whether it could act,
and what the answer was. Append-only in practice — the OWNER's review adds a
note, and nothing rewrites what was decided.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app import ids
from app.clock import to_iso
from app.storage.database import Database

SHADOW = "shd"


class ShadowDecisionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        capability: str,
        mode: str,
        would_act: bool,
        acted: bool,
        subject: str = "",
        reason: str = "",
        detail_json: str = "",
        gates_json: str = "",
        run_id: str | None = None,
        event_id: str | None = None,
        now: datetime,
    ) -> str:
        shadow_id = ids.new_id(SHADOW)
        self._db.execute(
            """
            INSERT INTO shadow_decisions
                (shadow_id, capability, mode, decided_at, would_act, acted,
                 subject, reason, detail_json, gates_json, run_id, event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shadow_id, capability, mode, to_iso(now),
                1 if would_act else 0, 1 if acted else 0,
                subject, reason, detail_json, gates_json, run_id, event_id,
            ),
        )
        return shadow_id

    def recent(
        self, *, capability: str = "", limit: int = 20
    ) -> list[sqlite3.Row]:
        if capability:
            return self._db.query_all(
                "SELECT * FROM shadow_decisions WHERE capability = ? "
                "ORDER BY decided_at DESC LIMIT ?",
                (capability, limit),
            )
        return self._db.query_all(
            "SELECT * FROM shadow_decisions ORDER BY decided_at DESC LIMIT ?",
            (limit,),
        )

    def suppressed(self, *, limit: int = 20) -> list[sqlite3.Row]:
        """The rows the review is actually about: she would have, and did not."""
        return self._db.query_all(
            "SELECT * FROM shadow_decisions WHERE would_act = 1 AND acted = 0 "
            "ORDER BY decided_at DESC LIMIT ?",
            (limit,),
        )

    def get(self, shadow_id: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM shadow_decisions WHERE shadow_id = ?", (shadow_id,)
        )

    def tally(self) -> list[sqlite3.Row]:
        """Per capability: how often it was considered, wanted and allowed.

        The gap between `wanted` and `acted` is the whole content of a shadow
        evaluation — a capability with `wanted = 0` has not been evaluated at
        all, however many rows it has.
        """
        return self._db.query_all(
            """
            SELECT capability,
                   mode,
                   COUNT(*)              AS considered,
                   SUM(would_act)        AS wanted,
                   SUM(acted)            AS acted,
                   MAX(decided_at)       AS last_at
            FROM shadow_decisions
            GROUP BY capability, mode
            ORDER BY capability
            """
        )

    def review(self, shadow_id: str, *, note: str, now: datetime) -> None:
        """The OWNER's verdict on one decision (spec 46, 47).

        Separate columns from the decision itself: what she would have done is
        a fact, and what the OWNER thought of it is an opinion added later.
        """
        self._db.execute(
            "UPDATE shadow_decisions SET reviewed_at = ?, review_note = ? "
            "WHERE shadow_id = ?",
            (to_iso(now), note[:1000], shadow_id),
        )

    def unreviewed_count(self, *, capability: str = "") -> int:
        if capability:
            return int(
                self._db.scalar(
                    "SELECT COUNT(*) FROM shadow_decisions WHERE reviewed_at IS NULL "
                    "AND would_act = 1 AND capability = ?",
                    (capability,),
                )
                or 0
            )
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM shadow_decisions WHERE reviewed_at IS NULL "
                "AND would_act = 1"
            )
            or 0
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM shadow_decisions") or 0)


__all__ = ["SHADOW", "ShadowDecisionRepository"]
