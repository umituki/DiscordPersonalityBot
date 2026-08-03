"""Runtime tick telemetry (rebuild spec 21, Phase 6).

One row per wake-up, including the quiet ones. A loop that wakes on schedule
and never does anything is indistinguishable from a loop that is not running
unless the empty passes leave a trace (spec 4.5).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import ids
from app.clock import from_iso, to_iso
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


@dataclass(frozen=True, slots=True)
class SpontaneousMemoryCue:
    cue_id: str
    cue_type: str
    source_kind: str
    source_id: str
    detail: str
    cues: tuple[str, ...]
    salience: float
    first_seen_at: datetime
    last_offered_at: datetime | None
    cooldown_until: datetime | None
    consumed_at: datetime | None
    outcome: str
    reason: str
    retrieval_group_id: str | None
    primary_memory_id: str | None
    event_id: str | None


class SpontaneousMemoryCueRepository:
    """Durable authority for cue identity, cooldown and final outcome."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def offer(
        self,
        *,
        cue_type: str,
        source_kind: str,
        source_id: str,
        detail: str,
        cues: tuple[str, ...],
        salience: float,
        now: datetime,
        cooldown: timedelta,
    ) -> SpontaneousMemoryCue | None:
        """Persist a cue and atomically claim one offer window.

        Looking at an unchanged source again does not produce another
        opportunity until its durable cooldown expires. A consumed cue never
        re-enters the runtime, including after restart.
        """
        cue_hash = hashlib.sha256(
            f"{cue_type}\x1f{source_kind}\x1f{source_id}".encode("utf-8")
        ).hexdigest()
        cue_id = ids.new_id("smc")
        moment = to_iso(now)
        ready_again = to_iso(now + cooldown)
        with self._db.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO spontaneous_memory_cues
                    (cue_id, cue_type, source_kind, source_id, cue_hash, detail,
                     cues_json, salience, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cue_id,
                    cue_type,
                    source_kind,
                    source_id,
                    cue_hash,
                    detail[:500],
                    json.dumps(list(cues), ensure_ascii=False),
                    max(0.0, min(1.0, float(salience))),
                    moment,
                ),
            )
            row = connection.execute(
                "SELECT cue_id FROM spontaneous_memory_cues WHERE cue_hash = ?",
                (cue_hash,),
            ).fetchone()
            assert row is not None
            cursor = connection.execute(
                """
                UPDATE spontaneous_memory_cues
                SET last_offered_at = ?, cooldown_until = ?, outcome = 'offered',
                    reason = ''
                WHERE cue_id = ? AND consumed_at IS NULL
                  AND (cooldown_until IS NULL OR cooldown_until <= ?)
                """,
                (moment, ready_again, row["cue_id"], moment),
            )
            if cursor.rowcount != 1:
                return None
            offered = connection.execute(
                "SELECT * FROM spontaneous_memory_cues WHERE cue_id = ?",
                (row["cue_id"],),
            ).fetchone()
        return _to_spontaneous_cue(offered)

    def complete(
        self,
        cue_id: str,
        *,
        outcome: str,
        reason: str,
        now: datetime,
        retrieval_group_id: str | None = None,
        primary_memory_id: str | None = None,
        event_id: str | None = None,
    ) -> bool:
        cursor = self._db.execute(
            """
            UPDATE spontaneous_memory_cues
            SET consumed_at = ?, outcome = ?, reason = ?, retrieval_group_id = ?,
                primary_memory_id = ?, event_id = ?
            WHERE cue_id = ? AND consumed_at IS NULL
            """,
            (
                to_iso(now), outcome, reason[:300], retrieval_group_id,
                primary_memory_id, event_id, cue_id,
            ),
        )
        return bool(cursor is not None and cursor.rowcount == 1)

    def get(self, cue_id: str) -> SpontaneousMemoryCue | None:
        row = self._db.query_one(
            "SELECT * FROM spontaneous_memory_cues WHERE cue_id = ?", (cue_id,)
        )
        return None if row is None else _to_spontaneous_cue(row)

    def recent(self, *, limit: int = 20) -> list[SpontaneousMemoryCue]:
        rows = self._db.query_all(
            "SELECT * FROM spontaneous_memory_cues "
            "ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        )
        return [_to_spontaneous_cue(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM spontaneous_memory_cues") or 0)


def _to_spontaneous_cue(row: sqlite3.Row) -> SpontaneousMemoryCue:
    optional = lambda value: None if value is None else from_iso(value)
    return SpontaneousMemoryCue(
        cue_id=row["cue_id"],
        cue_type=row["cue_type"],
        source_kind=row["source_kind"],
        source_id=row["source_id"],
        detail=row["detail"],
        cues=tuple(json.loads(row["cues_json"] or "[]")),
        salience=float(row["salience"]),
        first_seen_at=from_iso(row["first_seen_at"]),
        last_offered_at=optional(row["last_offered_at"]),
        cooldown_until=optional(row["cooldown_until"]),
        consumed_at=optional(row["consumed_at"]),
        outcome=row["outcome"],
        reason=row["reason"],
        retrieval_group_id=row["retrieval_group_id"],
        primary_memory_id=row["primary_memory_id"],
        event_id=row["event_id"],
    )


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


__all__ = [
    "ProactiveDeliberationRepository",
    "RuntimeTickRepository",
    "SpontaneousMemoryCue",
    "SpontaneousMemoryCueRepository",
]
