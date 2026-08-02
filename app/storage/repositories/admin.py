"""Admin action and backup persistence (spec 30, 31.9, 32).

The control plane decides *whether* an operation may run; this module is the
only thing that knows how to ask the database about it. Keeping the memory
admin queries here as well means the destructive path has no SQL of its own to
get subtly wrong.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app import ids
from app.admin.models import AdminAction, RiskClass
from app.clock import from_iso, to_iso
from app.storage.database import Database

ACTION = "adm"


class AdminActionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def open(
        self,
        *,
        operation: str,
        risk_class: RiskClass,
        target_type: str,
        target_id: str | None,
        dry_run: bool,
        reason: str,
        now: datetime,
    ) -> AdminAction:
        action_id = ids.new_id(ACTION)
        self._db.execute(
            """
            INSERT INTO admin_actions
                (action_id, operation, risk_class, target_type, target_id, dry_run,
                 reason, requested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id, operation, risk_class, target_type, target_id,
                1 if dry_run else 0, reason, to_iso(now),
            ),
        )
        return self.get(action_id)  # type: ignore[return-value]

    def advance(
        self,
        action_id: str,
        stage: str,
        *,
        impact: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        confirmed_by: str | None = None,
        snapshot_path: str | None = None,
        completed_at: datetime | None = None,
    ) -> AdminAction:
        self._db.execute(
            """
            UPDATE admin_actions
               SET stage = ?,
                   impact_json = COALESCE(?, impact_json),
                   result_json = COALESCE(?, result_json),
                   confirmed_by = COALESCE(?, confirmed_by),
                   snapshot_path = COALESCE(?, snapshot_path),
                   completed_at = COALESCE(?, completed_at)
             WHERE action_id = ?
            """,
            (
                stage,
                None if impact is None else json.dumps(impact, ensure_ascii=False, default=str),
                None if result is None else json.dumps(result, ensure_ascii=False, default=str),
                confirmed_by,
                snapshot_path,
                None if completed_at is None else to_iso(completed_at),
                action_id,
            ),
        )
        return self.get(action_id)  # type: ignore[return-value]

    def get(self, action_id: str) -> AdminAction | None:
        row = self._db.query_one(
            "SELECT * FROM admin_actions WHERE action_id = ?", (action_id,)
        )
        return None if row is None else _to_action(row)

    def history(self, *, limit: int = 50) -> list[AdminAction]:
        rows = self._db.query_all(
            "SELECT * FROM admin_actions ORDER BY requested_at DESC LIMIT ?", (limit,)
        )
        return [_to_action(row) for row in rows]

    def for_target(self, target_id: str) -> list[AdminAction]:
        rows = self._db.query_all(
            "SELECT * FROM admin_actions WHERE target_id = ? ORDER BY requested_at",
            (target_id,),
        )
        return [_to_action(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM admin_actions") or 0)


class MemoryAdminRepository:
    """The queries a memory admin operation needs, and nothing else."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def exists(self, memory_id: str) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM episodic_memories WHERE memory_id = ?", (memory_id,)
            )
            or 0
        )

    def link_count(self, memory_id: str) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM memory_links "
                "WHERE from_memory_id = ? OR to_memory_id = ?",
                (memory_id, memory_id),
            )
            or 0
        )

    def retrieval_count(self, memory_id: str) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM memory_retrievals WHERE memory_id = ?", (memory_id,)
            )
            or 0
        )

    def status_of(self, memory_id: str) -> str | None:
        row = self._db.query_one(
            "SELECT status FROM episodic_memories WHERE memory_id = ?", (memory_id,)
        )
        return None if row is None else row["status"]

    def hard_delete(self, memory_id: str) -> None:
        """Remove the subjective memory and everything pointing at it.

        The objective archive is untouched: events are immutable (spec 8.3),
        and this deletes what YUI remembers, not what happened.
        """
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM episodic_memories_fts WHERE memory_id = ?", (memory_id,)
            )
            connection.execute(
                "DELETE FROM memory_retrievals WHERE memory_id = ?", (memory_id,)
            )
            connection.execute(
                "DELETE FROM memory_links WHERE from_memory_id = ? OR to_memory_id = ?",
                (memory_id, memory_id),
            )
            connection.execute(
                "DELETE FROM memory_revisions WHERE memory_id = ?", (memory_id,)
            )
            connection.execute(
                "DELETE FROM episodic_memories WHERE memory_id = ?", (memory_id,)
            )


class BackupRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        backup_id: str,
        kind: str,
        path: Path,
        size_bytes: int,
        schema_version: int,
        integrity: str,
        restore_tested: bool,
        reason: str,
        now: datetime,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO backups
                (backup_id, kind, path, size_bytes, schema_version, integrity,
                 restore_tested, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                backup_id, kind, str(path), size_bytes, schema_version, integrity,
                1 if restore_tested else 0, reason, to_iso(now),
            ),
        )

    def get(self, backup_id: str) -> sqlite3.Row | None:
        return self._db.query_one("SELECT * FROM backups WHERE backup_id = ?", (backup_id,))

    def recent(self, *, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM backups ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM backups") or 0)


def _to_action(row: sqlite3.Row) -> AdminAction:
    return AdminAction(
        action_id=row["action_id"],
        operation=row["operation"],
        risk_class=row["risk_class"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        stage=row["stage"],
        dry_run=bool(row["dry_run"]),
        confirmed_by=row["confirmed_by"],
        snapshot_path=row["snapshot_path"],
        impact=json.loads(row["impact_json"] or "{}"),
        result=json.loads(row["result_json"] or "{}"),
        reason=row["reason"],
        requested_at=from_iso(row["requested_at"]),
        completed_at=None if row["completed_at"] is None else from_iso(row["completed_at"]),
    )
