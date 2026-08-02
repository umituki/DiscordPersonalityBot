"""Starting over as a fresh person (rebuild spec 3).

The OWNER has allowed the old history to be discarded, so this rebuild does not
carry anything forward — no events, no memories, no relationship, no personality,
no Genesis. What it does carry is the *fact* that a reset happened, and one
verified forensic copy of what was there before.

Three things are MUST in spec 3 and all three are structural here:

**3.1** a verified SQLite-API backup is taken first, into
``backups/pre_full_rebuild/``. If it does not verify, the reset stops and the
live database is untouched.

**3.2 / 3.3** the new runtime opens a *new* database built from migrations, not
a migrated copy of the old one. Nothing is imported. The old file is archived
beside the backup rather than deleted, so "where did the old YUI go" always has
an answer.

**3.4** this never runs as part of a normal start. It is its own command and it
requires the confirmation string; a reset that can happen by accident is not a
reset, it is data loss with a nice name.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.clock import Clock, SystemClock, to_iso
from app.storage.backup import BackupRecord, BackupService
from app.storage.database import Database
from app.storage.migrations import LATEST_VERSION, migrate
from app.storage.repositories.rebuild import RebuildEpochRepository

logger = logging.getLogger(__name__)

MODULE = "rebuild_service"

#: Rebuild spec 3.4. The OWNER has to type this, exactly.
CONFIRMATION = "ERASE_YUI_STATE"

#: Rebuild spec 3.1. A forensic archive: never read by the new runtime.
BACKUP_SUBDIR = "pre_full_rebuild"

#: The spec this rebuild was performed against, recorded with the epoch.
SPEC_VERSION = "YUI_FULL_REBUILD_SPEC"

#: Tables that must be empty in a freshly rebuilt database (spec 3.3). If any
#: of these has a row, something was imported that should not have been.
MUST_BE_EMPTY: tuple[str, ...] = (
    "events",
    "conversations",
    "conversation_turns",
    "episodes",
    "episodic_memories",
    "semantic_memories",
    "beliefs",
    "self_schemas",
    "state_values",
    "state_changes",
    "personality_traits",
    "personality_history",
    "value_priorities",
    "characteristic_adaptations",
    "deep_update_candidates",
    "narrative_identity",
    "npcs",
    "npc_models",
    "npc_relationships",
    "npc_interactions",
    "proactive_contacts",
    "activities",
    "goals",
    "habits",
    "simulation_runs",
    "life_scaffolds",
    "temperament_seeds",
    "knowledge_acquisitions",
    "diary_entries",
)


class RebuildRefused(RuntimeError):
    """Raised when a reset was asked for without meeting its preconditions."""


@dataclass
class RebuildResult:
    """What the reset did, in the order spec 3.4 lists it."""

    confirmed: bool = False
    backup: BackupRecord | None = None
    archived_db_path: Path | None = None
    fresh_db_path: Path | None = None
    schema_version: int = 0
    epoch_id: str | None = None
    genesis_status: str = "pending"
    imported_rows: dict[str, int] = field(default_factory=dict)
    refusal: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.confirmed
            and not self.refusal
            and self.epoch_id is not None
            and not self.imported_rows
        )

    def as_detail(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "epoch_id": self.epoch_id,
            "schema_version": self.schema_version,
            "backup": None if self.backup is None else str(self.backup.path),
            "archived_db": None if self.archived_db_path is None else str(
                self.archived_db_path
            ),
            "fresh_db": None if self.fresh_db_path is None else str(self.fresh_db_path),
            "genesis_status": self.genesis_status,
            "unexpected_rows": self.imported_rows,
            "refusal": self.refusal,
        }


class RebuildService:
    """Archives the old database and creates a fresh one (rebuild spec 3)."""

    name = MODULE

    def __init__(
        self,
        *,
        db: Database,
        backups: BackupService,
        backups_dir: Path,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._backups = backups
        self._backups_dir = Path(backups_dir)
        self._clock = clock or SystemClock()

    # --- reads ---------------------------------------------------------------
    def current_epoch(self):
        """The epoch this database is in, or ``None`` if it predates rebuilds."""
        return RebuildEpochRepository(self._db).current()

    # --- the reset (spec 3.4) ------------------------------------------------
    def reset(
        self, *, confirmation: str, reason: str = "full rebuild"
    ) -> RebuildResult:
        """Archive the current database and start a fresh one.

        Order is the spec's::

            validate confirmation
            → verified backup
            → close DB
            → archive active DB
            → create fresh schema
            → record rebuild epoch
            → Genesis pending

        Nothing is deleted at any point: the old database is moved, and the
        backup is a second, independently verified copy of it.
        """
        result = RebuildResult()
        if confirmation != CONFIRMATION:
            result.refusal = (
                f"a rebuild reset requires the confirmation {CONFIRMATION!r}"
            )
            raise RebuildRefused(result.refusal)
        result.confirmed = True

        production = self._production_path()
        previous = self._previous_epoch_id()

        # 3.1 — a verified backup, or nothing happens at all.
        result.backup = self._backups.create(
            kind="pre_full_rebuild", reason=reason
        )
        if not result.backup.usable:
            result.refusal = "the pre-rebuild backup did not verify; nothing was changed"
            logger.error(result.refusal)
            return result
        result.backup = self._file_into_rebuild_dir(result.backup)

        # Close before the file moves, and take the WAL/SHM siblings with it or
        # the archive is most of a database rather than one.
        self._db.close()
        result.archived_db_path = self._archive(production)

        # 3.2 — a new schema built from migrations, never a migrated old file.
        migration = migrate(self._db, clock=self._clock)
        result.fresh_db_path = production
        result.schema_version = migration.schema_version

        # 3.3 — prove nothing came across.
        result.imported_rows = self._non_empty_tables()
        if result.imported_rows:
            result.refusal = (
                "the fresh database is not empty: "
                + ", ".join(f"{name}={count}" for name, count in result.imported_rows.items())
            )
            logger.error(result.refusal)
            return result

        epochs = RebuildEpochRepository(self._db)
        result.epoch_id = epochs.record(
            started_at=self._clock.now(),
            reason=reason,
            spec_version=SPEC_VERSION,
            schema_version=result.schema_version,
            backup_path=str(result.backup.path),
            archived_db_path=str(result.archived_db_path),
            previous_epoch_id=previous,
            genesis_status="pending",
            detail={"latest_migration": LATEST_VERSION},
        )
        logger.warning(
            "rebuild epoch %s started: archived=%s backup=%s",
            result.epoch_id,
            result.archived_db_path,
            result.backup.path,
        )
        return result

    # --- helpers -------------------------------------------------------------
    def _production_path(self) -> Path:
        path = self._db.path
        if not isinstance(path, Path):
            raise RebuildRefused("an in-memory database has nothing to rebuild")
        return path

    def _previous_epoch_id(self) -> str | None:
        try:
            row = RebuildEpochRepository(self._db).current()
        except Exception:  # noqa: BLE001 - the table may predate this feature
            return None
        return None if row is None else str(row["epoch_id"])

    def _rebuild_dir(self) -> Path:
        directory = self._backups_dir / BACKUP_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _file_into_rebuild_dir(self, record: BackupRecord) -> BackupRecord:
        """Move the verified backup into ``backups/pre_full_rebuild/`` (3.1)."""
        target = self._rebuild_dir() / f"{self._stamp()}.db"
        if record.path.exists():
            shutil.move(str(record.path), str(target))
        return BackupRecord(
            backup_id=record.backup_id,
            kind=record.kind,
            path=target,
            size_bytes=record.size_bytes,
            schema_version=record.schema_version,
            integrity=record.integrity,
            restore_tested=record.restore_tested,
            reason=record.reason,
            created_at=record.created_at,
        )

    def _archive(self, production: Path) -> Path | None:
        """Move the live database aside. Never delete it (spec 3.4)."""
        if not production.exists():
            return None
        target = self._rebuild_dir() / f"{self._stamp()}.previous.db"
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(production) + suffix)
            if source.exists():
                shutil.move(str(source), str(Path(str(target) + suffix)))
        return target

    def _non_empty_tables(self) -> dict[str, int]:
        """Repositories are the only SQL boundary (architecture rules)."""
        return RebuildEpochRepository(self._db).non_empty_tables(MUST_BE_EMPTY)

    def _stamp(self) -> str:
        return (
            to_iso(self._clock.now())
            .replace(":", "")
            .replace("-", "")
            .replace("+0000", "Z")
        )


__all__ = [
    "BACKUP_SUBDIR",
    "CONFIRMATION",
    "MODULE",
    "MUST_BE_EMPTY",
    "RebuildRefused",
    "RebuildResult",
    "RebuildService",
    "SPEC_VERSION",
]
