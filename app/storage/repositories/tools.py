"""Tool execution records (spec 26, 31.9).

This table is the authority on whether a tool call happened and whether it
worked. Written only by the Tool Manager.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app.clock import to_iso
from app.storage.database import Database


class ToolCallRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(
        self,
        *,
        call_id: str,
        tool_name: str,
        permission: str,
        requested_by: str,
        run_id: str | None,
        event_id: str | None,
        arguments: dict[str, Any],
        reason: str,
        now: datetime,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO tool_calls
                (call_id, tool_name, permission, requested_by, run_id, event_id,
                 arguments_json, status, reason, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                call_id, tool_name, permission, requested_by, run_id, event_id,
                json.dumps(arguments, ensure_ascii=False, default=str), reason[:500],
                to_iso(now),
            ),
        )

    def finish(
        self,
        *,
        call_id: str,
        success: bool,
        source: str,
        data: Any,
        error: str | None,
        retryable: bool,
        now: datetime,
    ) -> None:
        self._db.execute(
            """
            UPDATE tool_calls
               SET status = ?, success = ?, source = ?, data_json = ?, error = ?,
                   retryable = ?, finished_at = ?
             WHERE call_id = ?
            """,
            (
                "succeeded" if success else "failed",
                1 if success else 0,
                source,
                None if data is None else json.dumps(data, ensure_ascii=False, default=str)[:8000],
                None if error is None else error[:2000],
                1 if retryable else 0,
                to_iso(now),
                call_id,
            ),
        )

    def successful_call_ids(self, *, run_id: str | None = None, limit: int = 20) -> list[str]:
        if run_id is None:
            rows = self._db.query_all(
                "SELECT call_id FROM tool_calls WHERE success = 1 "
                "ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self._db.query_all(
                "SELECT call_id FROM tool_calls WHERE success = 1 AND run_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (run_id, limit),
            )
        return [row["call_id"] for row in rows]

    def get(self, call_id: str) -> sqlite3.Row | None:
        return self._db.query_one("SELECT * FROM tool_calls WHERE call_id = ?", (call_id,))

    def recent(self, *, limit: int = 50) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM tool_calls ORDER BY started_at DESC LIMIT ?", (limit,)
        )

    def failures(self, *, limit: int = 50) -> list[sqlite3.Row]:
        """Failed calls are kept and visible (spec 17.3)."""
        return self._db.query_all(
            "SELECT * FROM tool_calls WHERE success = 0 AND status = 'failed' "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM tool_calls") or 0)
