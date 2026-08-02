"""Processing run records (spec 7.2, 29 traceability)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from app.clock import to_iso
from app.storage.database import Database

#: ``conflicted`` is distinct from ``failed`` on purpose (patch spec 7.2): the
#: run lost a version race and was reprocessed, which is not the same as the
#: work having been lost.
RunStatus = Literal[
    "running", "committed", "rejected", "failed", "interrupted", "conflicted"
]


class ProcessingRunRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(
        self,
        *,
        run_id: str,
        root_event_id: str,
        snapshot_id: str | None,
        manifest_id: str | None,
        mode: str,
        priority: str,
        now: datetime,
        retry_of_run_id: str | None = None,
        commit_attempt: int = 1,
        snapshot_fingerprint: str | None = None,
    ) -> None:
        """Open a run. Patch spec 7.5 keeps the retry chain traceable."""
        self._db.execute(
            """
            INSERT INTO processing_runs
                (run_id, root_event_id, state_snapshot_id, runtime_manifest_id, mode,
                 priority, status, started_at, retry_of_run_id, commit_attempt,
                 snapshot_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                run_id, root_event_id, snapshot_id, manifest_id, mode, priority,
                to_iso(now), retry_of_run_id, commit_attempt, snapshot_fingerprint,
            ),
        )

    def record_conflict(self, run_id: str) -> None:
        """A commit lost a version race (patch spec 7.5)."""
        self._db.execute(
            "UPDATE processing_runs SET conflict_count = conflict_count + 1 "
            "WHERE run_id = ?",
            (run_id,),
        )

    def finish(
        self,
        *,
        run_id: str,
        status: RunStatus,
        now: datetime,
        proposals_received: int = 0,
        proposals_accepted: int = 0,
        proposals_rejected: int = 0,
        error: str | None = None,
    ) -> None:
        self._db.execute(
            """
            UPDATE processing_runs
               SET status = ?, finished_at = ?, proposals_received = ?,
                   proposals_accepted = ?, proposals_rejected = ?, error = ?
             WHERE run_id = ?
            """,
            (
                status,
                to_iso(now),
                proposals_received,
                proposals_accepted,
                proposals_rejected,
                error[:2000] if error else None,
                run_id,
            ),
        )

    def get(self, run_id: str) -> sqlite3.Row | None:
        return self._db.query_one("SELECT * FROM processing_runs WHERE run_id = ?", (run_id,))

    def unfinished(self) -> list[sqlite3.Row]:
        """Runs left ``running`` by a crash; recovery must resolve them."""
        return self._db.query_all(
            "SELECT * FROM processing_runs WHERE status = 'running' ORDER BY started_at"
        )

    def mark_interrupted(self, *, now: datetime) -> int:
        """Close out runs that a previous process never finished (spec 32)."""
        cursor = self._db.execute(
            "UPDATE processing_runs SET status = 'interrupted', finished_at = ?, "
            "error = 'process terminated before commit' WHERE status = 'running'",
            (to_iso(now),),
        )
        return cursor.rowcount

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM processing_runs") or 0)
