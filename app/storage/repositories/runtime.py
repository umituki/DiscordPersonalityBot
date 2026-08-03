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


class ProactiveDeliberationRepository:
    """Every proactive deliberation, in every mode (spec 28.4).

    SHADOW is worthless without this table: the whole point of a shadow mode is
    reading back what she *would* have sent and deciding whether it was right.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, outcome) -> None:
        self._db.execute(
            """
            INSERT INTO proactive_deliberations
                (deliberation_id, considered_at, mode, trigger_kind, trigger_detail,
                 gate_passed, gate_reason, desire, unanswered, required_wait_h,
                 judged, judgment, judgment_reason, draft, guard_verdict,
                 would_send, sent, event_id, contact_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.deliberation_id,
                to_iso(outcome.considered_at),
                outcome.mode,
                outcome.trigger_kind,
                outcome.trigger_detail,
                1 if outcome.gate_passed else 0,
                outcome.gate_reason,
                float(outcome.desire),
                int(outcome.unanswered),
                float(outcome.required_wait_hours),
                1 if outcome.judged else 0,
                outcome.judgment,
                outcome.judgment_reason,
                outcome.draft,
                outcome.guard_verdict,
                1 if outcome.would_send else 0,
                1 if outcome.sent else 0,
                outcome.event_id,
                outcome.contact_id,
            ),
        )

    def recent(self, *, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT deliberation_id, considered_at, mode, trigger_kind, gate_passed, "
            "gate_reason, desire, judgment, guard_verdict, would_send, sent "
            "FROM proactive_deliberations ORDER BY considered_at DESC LIMIT ?",
            (limit,),
        )

    def count(self) -> int:
        return int(
            self._db.scalar("SELECT COUNT(*) FROM proactive_deliberations") or 0
        )

    def would_send_count(self) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM proactive_deliberations WHERE would_send = 1"
            )
            or 0
        )

    def sent_count(self) -> int:
        return int(
            self._db.scalar("SELECT COUNT(*) FROM proactive_deliberations WHERE sent = 1")
            or 0
        )


__all__ = ["ProactiveDeliberationRepository", "RuntimeTickRepository"]
