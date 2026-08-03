"""Runtime tick telemetry (rebuild spec 21, Phase 6).

One row per wake-up, including the quiet ones. A loop that wakes on schedule
and never does anything is indistinguishable from a loop that is not running
unless the empty passes leave a trace (spec 4.5).
"""

from __future__ import annotations

import sqlite3

from app.clock import to_iso
from app.runtime.models import RuntimeTick
from app.storage.database import Database


class RuntimeTickRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, tick: RuntimeTick) -> None:
        self._db.execute(
            """
            INSERT INTO runtime_ticks
                (tick_id, woke_at, wake_reason, opportunities, opportunity_kinds,
                 unclaimed_kinds, candidates, decision_id, chosen_action, executed,
                 outcome, deferred_reason, next_wake_at, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tick.tick_id,
                to_iso(tick.woke_at),
                tick.wake_reason,
                tick.opportunities,
                ",".join(tick.opportunity_kinds),
                ",".join(tick.unclaimed_kinds),
                tick.candidates,
                tick.decision_id,
                tick.chosen_action,
                1 if tick.executed else 0,
                tick.outcome,
                tick.deferred_reason,
                None if tick.next_wake_at is None else to_iso(tick.next_wake_at),
                tick.duration_ms,
            ),
        )

    def recent(self, *, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT tick_id, woke_at, wake_reason, opportunities, opportunity_kinds, "
            "unclaimed_kinds, candidates, chosen_action, executed, outcome, "
            "deferred_reason, next_wake_at, duration_ms "
            "FROM runtime_ticks ORDER BY woke_at DESC LIMIT ?",
            (limit,),
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM runtime_ticks") or 0)

    def executed_count(self) -> int:
        return int(
            self._db.scalar("SELECT COUNT(*) FROM runtime_ticks WHERE executed = 1") or 0
        )

    def unclaimed_kinds(self) -> list[str]:
        """Every opportunity kind that has fired and never found a builder.

        The §4.5 zero-row audit, as a query. If this is non-empty, some phase
        is scheduling work that no phase has claimed.
        """
        seen: set[str] = set()
        for row in self._db.query_all(
            "SELECT DISTINCT unclaimed_kinds FROM runtime_ticks "
            "WHERE unclaimed_kinds != ''"
        ):
            seen.update(part for part in row["unclaimed_kinds"].split(",") if part)
        return sorted(seen)


__all__ = ["RuntimeTickRepository"]
