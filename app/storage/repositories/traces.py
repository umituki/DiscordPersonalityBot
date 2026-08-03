"""Conversation end-to-end traces (patch spec 19.2)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.clock import to_iso
from app.storage.database import Database

_COLUMNS = (
    "trace_id",
    "event_id",
    "run_id",
    "channel_id",
    "outcome",
    "received_at",
    "admitted_at",
    "typing_started_at",
    "appraisal_started_at",
    "appraisal_ended_at",
    "state_commit_started_at",
    "state_commit_ended_at",
    "memory_recall_started_at",
    "memory_recall_ended_at",
    "dialogue_started_at",
    "dialogue_ended_at",
    "social_interpretation_started_at",
    "social_interpretation_ended_at",
    "reference_retrieval_started_at",
    "reference_retrieval_ended_at",
    "reply_started_at",
    "realization_started_at",
    "realization_ended_at",
    "reply_ended_at",
    "discord_send_started_at",
    "discord_send_ended_at",
    "outbound_projected_at",
    "typing_stopped_at",
    "total_ms",
    "queue_wait_ms",
    "inference_ms",
    "model_calls",
    "detail_json",
)


class ConversationTraceRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, trace) -> None:
        """Store one trace. Re-recording the same trace is a no-op."""
        marks = {
            name: (None if value is None else to_iso(value))
            for name, value in ((stage, trace.at(stage)) for stage in _STAGE_COLUMNS)
        }
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(
            f"{column} = excluded.{column}" for column in _COLUMNS if column != "trace_id"
        )
        self._db.execute(
            f"INSERT INTO conversation_traces ({', '.join(_COLUMNS)}) "
            f"VALUES ({placeholders}) ON CONFLICT(trace_id) DO UPDATE SET {updates}",
            (
                trace.trace_id,
                trace.event_id,
                trace.run_id,
                trace.channel_id,
                trace.outcome,
                to_iso(trace.received_at),
                *(marks[stage] for stage in _STAGE_COLUMNS[1:]),
                trace.total_ms,
                trace.queue_wait_ms,
                trace.inference_ms,
                trace.model_calls,
                json.dumps(trace.as_detail(), ensure_ascii=False, sort_keys=True),
            ),
        )

    def calls_for_run(self, run_id: str) -> list[sqlite3.Row]:
        """The model calls this run made, for the queue-wait / inference split."""
        return self._db.query_all(
            "SELECT queue_wait_ms, model_total_duration_ms, latency_ms "
            "FROM llm_calls WHERE run_id = ?",
            (run_id,),
        )

    # --- reads ---------------------------------------------------------------
    def recent(self, *, limit: int = 100, outcome: str | None = None) -> list[sqlite3.Row]:
        if outcome is None:
            return self._db.query_all(
                "SELECT * FROM conversation_traces ORDER BY received_at DESC LIMIT ?",
                (limit,),
            )
        return self._db.query_all(
            "SELECT * FROM conversation_traces WHERE outcome = ? "
            "ORDER BY received_at DESC LIMIT ?",
            (outcome, limit),
        )

    def latencies(self, *, since: datetime | None = None, limit: int = 500) -> list[int]:
        """Total turn latencies, newest first, for the percentiles of spec 20."""
        if since is None:
            rows = self._db.query_all(
                "SELECT total_ms FROM conversation_traces WHERE total_ms IS NOT NULL "
                "ORDER BY received_at DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self._db.query_all(
                "SELECT total_ms FROM conversation_traces WHERE total_ms IS NOT NULL "
                "AND received_at >= ? ORDER BY received_at DESC LIMIT ?",
                (to_iso(since), limit),
            )
        return [int(row["total_ms"]) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM conversation_traces") or 0)


#: The timestamp columns, in the order they appear in ``_COLUMNS``.
_STAGE_COLUMNS: tuple[str, ...] = (
    "received_at",
    "admitted_at",
    "typing_started_at",
    "appraisal_started_at",
    "appraisal_ended_at",
    "state_commit_started_at",
    "state_commit_ended_at",
    "memory_recall_started_at",
    "memory_recall_ended_at",
    "dialogue_started_at",
    "dialogue_ended_at",
    "social_interpretation_started_at",
    "social_interpretation_ended_at",
    "reference_retrieval_started_at",
    "reference_retrieval_ended_at",
    "reply_started_at",
    "realization_started_at",
    "realization_ended_at",
    "reply_ended_at",
    "discord_send_started_at",
    "discord_send_ended_at",
    "outbound_projected_at",
    "typing_stopped_at",
)


__all__ = ["ConversationTraceRepository"]
