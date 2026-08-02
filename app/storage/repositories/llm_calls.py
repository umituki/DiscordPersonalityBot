"""LLM call traces (spec 29)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from app.clock import to_iso
from app.storage.database import Database

CallStatus = Literal["pending", "succeeded", "failed"]


class LLMCallRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(
        self,
        *,
        call_id: str,
        run_id: str | None,
        event_id: str | None,
        manifest_id: str | None,
        purpose: str,
        priority: str,
        model: str,
        prompt_id: str | None,
        prompt_version: str | None,
        structured: bool,
        request_fingerprint: str,
        request_transcript: str | None,
        now: datetime,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO llm_calls
                (call_id, run_id, event_id, manifest_id, purpose, priority, model, prompt_id,
                 prompt_version, structured, request_fingerprint, request_transcript,
                 status, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                call_id, run_id, event_id, manifest_id, purpose, priority, model, prompt_id,
                prompt_version, 1 if structured else 0, request_fingerprint, request_transcript,
                to_iso(now),
            ),
        )

    def finish(
        self,
        *,
        call_id: str,
        status: CallStatus,
        now: datetime,
        latency_ms: int | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        response_text: str | None,
        error_type: str | None,
        error_detail: str | None,
    ) -> None:
        self._db.execute(
            """
            UPDATE llm_calls
               SET status = ?, finished_at = ?, latency_ms = ?, prompt_tokens = ?,
                   completion_tokens = ?, response_text = ?, error_type = ?, error_detail = ?
             WHERE call_id = ?
            """,
            (
                status, to_iso(now), latency_ms, prompt_tokens, completion_tokens,
                response_text, error_type, error_detail, call_id,
            ),
        )

    def record_attempt(
        self, *, call_id: str, attempts: int, rejection_stage: str | None, reason_code: str | None
    ) -> None:
        """Structured generation may need several attempts (spec 28.2)."""
        self._db.execute(
            "UPDATE llm_calls SET attempts = ?, rejection_stage = ?, reason_code = ? "
            "WHERE call_id = ?",
            (attempts, rejection_stage, reason_code, call_id),
        )

    def get(self, call_id: str) -> sqlite3.Row | None:
        return self._db.query_one("SELECT * FROM llm_calls WHERE call_id = ?", (call_id,))

    def recent(self, *, limit: int = 50, purpose: str | None = None) -> list[sqlite3.Row]:
        if purpose is None:
            return self._db.query_all(
                "SELECT * FROM llm_calls ORDER BY started_at DESC, call_id DESC LIMIT ?", (limit,)
            )
        return self._db.query_all(
            "SELECT * FROM llm_calls WHERE purpose = ? ORDER BY started_at DESC LIMIT ?",
            (purpose, limit),
        )

    def for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM llm_calls WHERE run_id = ? ORDER BY started_at", (run_id,)
        )

    def pending(self) -> list[sqlite3.Row]:
        """Calls a crash left open (spec 32 recovery)."""
        return self._db.query_all(
            "SELECT * FROM llm_calls WHERE status = 'pending' ORDER BY started_at"
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM llm_calls") or 0)
