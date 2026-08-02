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
        logical_call_id: str | None = None,
        attempt: int = 1,
        thinking_enabled: bool = False,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO llm_calls
                (call_id, run_id, event_id, manifest_id, purpose, priority, model, prompt_id,
                 prompt_version, structured, request_fingerprint, request_transcript,
                 logical_call_id, attempt, thinking_enabled, status, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                call_id, run_id, event_id, manifest_id, purpose, priority, model, prompt_id,
                prompt_version, 1 if structured else 0, request_fingerprint, request_transcript,
                logical_call_id, attempt, 1 if thinking_enabled else 0,
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
        queue_wait_ms: int | None = None,
        transport_latency_ms: int | None = None,
        model_total_duration_ms: int | None = None,
        load_duration_ms: int | None = None,
        prompt_eval_duration_ms: int | None = None,
        eval_duration_ms: int | None = None,
        thinking_present: bool = False,
        thinking_char_count: int = 0,
    ) -> None:
        """Close a call trace.

        Patch spec 19.1 keeps queue time and inference time apart: a slow reply
        must be attributable to waiting for a slot or to the model itself,
        never to a guess.
        """
        self._db.execute(
            """
            UPDATE llm_calls
               SET status = ?, finished_at = ?, latency_ms = ?, prompt_tokens = ?,
                   completion_tokens = ?, response_text = ?, error_type = ?, error_detail = ?,
                   queue_wait_ms = ?, transport_latency_ms = ?, model_total_duration_ms = ?,
                   load_duration_ms = ?, prompt_eval_duration_ms = ?, eval_duration_ms = ?,
                   thinking_present = ?, thinking_char_count = ?
             WHERE call_id = ?
            """,
            (
                status, to_iso(now), latency_ms, prompt_tokens, completion_tokens,
                response_text, error_type, error_detail,
                queue_wait_ms, transport_latency_ms, model_total_duration_ms,
                load_duration_ms, prompt_eval_duration_ms, eval_duration_ms,
                1 if thinking_present else 0, thinking_char_count, call_id,
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
