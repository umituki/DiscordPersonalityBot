"""Rebuild epochs (rebuild spec 3)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Iterable

from app import ids
from app.clock import to_iso
from app.storage.database import Database

EPOCH = "eph"


class RebuildEpochRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        started_at: datetime,
        reason: str = "",
        spec_version: str = "",
        schema_version: int = 0,
        backup_path: str | None = None,
        archived_db_path: str | None = None,
        previous_epoch_id: str | None = None,
        genesis_status: str = "pending",
        detail: dict | None = None,
    ) -> str:
        epoch_id = ids.new_id(EPOCH)
        self._db.execute(
            """
            INSERT INTO rebuild_epochs
                (epoch_id, started_at, reason, spec_version, schema_version,
                 backup_path, archived_db_path, previous_epoch_id, genesis_status,
                 detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                epoch_id,
                to_iso(started_at),
                reason,
                spec_version,
                int(schema_version),
                backup_path,
                archived_db_path,
                previous_epoch_id,
                genesis_status,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return epoch_id

    def set_genesis_status(self, epoch_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE rebuild_epochs SET genesis_status = ? WHERE epoch_id = ?",
            (status, epoch_id),
        )

    def current(self) -> sqlite3.Row | None:
        """The epoch this database is living in, or ``None`` before any rebuild."""
        return self._db.query_one(
            "SELECT * FROM rebuild_epochs ORDER BY started_at DESC, epoch_id DESC LIMIT 1"
        )

    def all(self, *, limit: int = 50) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM rebuild_epochs ORDER BY started_at DESC LIMIT ?", (limit,)
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM rebuild_epochs") or 0)

    # --- proving a fresh database is fresh (rebuild spec 3.3) ---------------
    def existing_tables(self) -> set[str]:
        rows = self._db.query_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        return {str(row["name"]) for row in rows}

    def non_empty_tables(self, tables: Iterable[str]) -> dict[str, int]:
        """Which of these tables still hold rows.

        Tables the current schema does not have yet are skipped rather than
        raising: the list names tables from phases that have not built theirs,
        on purpose, so the sweep covers them the moment they appear.
        """
        present = self.existing_tables()
        counts: dict[str, int] = {}
        for table in tables:
            if table not in present:
                continue
            count = int(self._db.scalar(f"SELECT COUNT(*) FROM {table}") or 0)
            if count:
                counts[table] = count
        return counts


__all__ = ["RebuildEpochRepository"]
