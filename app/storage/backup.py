"""Backup and restore (spec 32).

    SQLite 稼働中の単純 Explorer copy を正式 Backup としない。
    SQLite backup mechanism を使用する。
    Backup 作成後に integrity を検証し、restore test を行う。
    Live DB を OneDrive / Dropbox / iCloud 等で同期しない。

A file copied out from under a running SQLite database is not a backup; with
WAL enabled it is a half-transaction with a confident filename. This module
uses ``sqlite3.Connection.backup``, which is safe against a live writer, and
then does the two things that make a backup a backup: verifies the copy's
integrity, and actually opens it to prove it restores.

A backup whose verification failed is recorded as such and never returned as a
usable one — a broken backup that everyone believes in is worse than none.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import ids
from app.clock import Clock, SystemClock, from_iso
from app.storage.database import Database, restore_test, verify_file
from app.storage.repositories.admin import BackupRepository

logger = logging.getLogger(__name__)

MODULE = "backup_service"
BACKUP = "bkp"

#: Spec 32: a live database must not live in one of these.
CLOUD_MARKERS: tuple[str, ...] = (
    "onedrive",
    "dropbox",
    "icloud",
    "google drive",
    "googledrive",
    "box sync",
)


class BackupError(RuntimeError):
    """Raised when a backup could not be produced or verified."""


@dataclass(frozen=True, slots=True)
class BackupRecord:
    backup_id: str
    kind: str
    path: Path
    size_bytes: int
    schema_version: int
    integrity: str
    restore_tested: bool
    reason: str
    created_at: datetime

    @property
    def usable(self) -> bool:
        """Only a verified, restore-tested copy counts (spec 32)."""
        return self.integrity == "ok" and self.restore_tested


def looks_cloud_synced(path: Path) -> bool:
    """Whether a path sits inside a consumer sync folder (spec 32)."""
    lowered = str(path).lower()
    return any(marker in lowered for marker in CLOUD_MARKERS)


class BackupService:
    name = MODULE

    def __init__(
        self,
        db: Database,
        *,
        backups_dir: Path,
        repository: BackupRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._dir = Path(backups_dir)
        self._repository = repository or BackupRepository(db)
        self._clock = clock or SystemClock()

    # --- creating -----------------------------------------------------------
    def create(self, *, kind: str = "manual", reason: str = "") -> BackupRecord:
        """Take a consistent copy with SQLite's own backup API, then verify it."""
        self._dir.mkdir(parents=True, exist_ok=True)
        now = self._clock.now()
        backup_id = ids.new_id(BACKUP)
        target = self._dir / f"{backup_id}.db"

        source = self._db.connect()
        destination = sqlite3.connect(target)
        try:
            # Safe against a live writer; a plain file copy is not.
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()

        integrity, schema = verify_file(target)
        restored = integrity == "ok" and restore_test(target)
        size = target.stat().st_size if target.exists() else 0

        record = BackupRecord(
            backup_id=backup_id,
            kind=kind,
            path=target,
            size_bytes=size,
            schema_version=schema,
            integrity=integrity,
            restore_tested=restored,
            reason=reason,
            created_at=now,
        )
        self._record(record)

        if not record.usable:
            logger.error(
                "backup failed verification id=%s integrity=%s restore_tested=%s",
                backup_id,
                integrity,
                restored,
            )
        else:
            logger.info("backup created id=%s bytes=%d", backup_id, size)
        return record

    def before_operation(self, reason: str) -> BackupRecord:
        """The pre-operation snapshot a destructive change requires (spec 30)."""
        record = self.create(kind="pre_operation", reason=reason)
        if not record.usable:
            raise BackupError(
                f"pre-operation backup could not be verified ({record.integrity}); "
                "the operation must not proceed (spec 30, 32)"
            )
        return record

    # --- verification -------------------------------------------------------
    def verify(self, backup_id: str) -> bool:
        record = self.get(backup_id)
        if record is None:
            return False
        integrity, _ = verify_file(record.path)
        return integrity == "ok" and restore_test(record.path)

    # --- records ------------------------------------------------------------
    def _record(self, record: BackupRecord) -> None:
        self._repository.record(
            backup_id=record.backup_id,
            kind=record.kind,
            path=record.path,
            size_bytes=record.size_bytes,
            schema_version=record.schema_version,
            integrity=record.integrity,
            restore_tested=record.restore_tested,
            reason=record.reason,
            now=record.created_at,
        )

    def get(self, backup_id: str) -> BackupRecord | None:
        row = self._repository.get(backup_id)
        return None if row is None else _to_record(row)

    def recent(self, *, limit: int = 20) -> list[BackupRecord]:
        return [_to_record(row) for row in self._repository.recent(limit=limit)]

    def latest_usable(self) -> BackupRecord | None:
        for record in self.recent(limit=50):
            if record.usable:
                return record
        return None

    def count(self) -> int:
        return self._repository.count()


def _to_record(row) -> BackupRecord:
    return BackupRecord(
        backup_id=row["backup_id"],
        kind=row["kind"],
        path=Path(row["path"]),
        size_bytes=int(row["size_bytes"]),
        schema_version=int(row["schema_version"]),
        integrity=row["integrity"],
        restore_tested=bool(row["restore_tested"]),
        reason=row["reason"],
        created_at=from_iso(row["created_at"]),
    )
