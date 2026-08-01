"""Schema migrations.

Spec 31.9: ``テーブル追加は Migration 経由``. The production database is never
hand-edited, and every applied migration is recorded with a checksum so schema
drift is detectable at startup.

Phase 1 creates only the core tables of spec 31.3 plus the two tables the
commit path needs to exist at all: ``state_values`` (the current value of a
dynamic state key) and ``failures`` (rejected proposals must be recorded,
never clamped).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from app.clock import Clock, SystemClock, to_iso
from app.storage.database import Database

logger = logging.getLogger(__name__)

SCHEMA_VERSION_KEY = "schema_version"


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        digest = hashlib.sha256()
        for statement in self.statements:
            digest.update(" ".join(statement.split()).encode("utf-8"))
            digest.update(b";")
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[int, ...]
    schema_version: int


class MigrationError(RuntimeError):
    """Raised when the database schema cannot be brought to the target state."""


_0001_CORE = Migration(
    version=1,
    name="core_event_state",
    statements=(
        # --- meta ----------------------------------------------------------
        """
        CREATE TABLE schema_meta (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            checksum    TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
        """,
        # --- versioning / reproducibility (spec 29) -------------------------
        """
        CREATE TABLE runtime_manifests (
            manifest_id         TEXT PRIMARY KEY,
            fingerprint         TEXT NOT NULL UNIQUE,
            created_at          TEXT NOT NULL,
            code_commit_hash    TEXT,
            config_version      INTEGER NOT NULL,
            event_schema_version INTEGER NOT NULL,
            components_json     TEXT NOT NULL
        )
        """,
        # --- events (spec 8) ------------------------------------------------
        """
        CREATE TABLE events (
            event_id                TEXT PRIMARY KEY,
            event_type              TEXT NOT NULL,
            category                TEXT NOT NULL,
            schema_version          INTEGER NOT NULL,
            payload_schema_version  INTEGER NOT NULL,
            occurred_at             TEXT NOT NULL,
            recorded_at             TEXT NOT NULL,
            actor_type              TEXT NOT NULL,
            actor_id                TEXT,
            target_type             TEXT NOT NULL,
            target_id               TEXT,
            root_event_id           TEXT NOT NULL,
            parent_event_id         TEXT,
            source_type             TEXT NOT NULL,
            source_id               TEXT,
            objective               INTEGER NOT NULL,
            priority                TEXT NOT NULL,
            origin                  TEXT NOT NULL,
            payload_json            TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_events_root ON events (root_event_id, occurred_at)",
        "CREATE INDEX idx_events_parent ON events (parent_event_id)",
        "CREATE INDEX idx_events_type_time ON events (event_type, occurred_at)",
        "CREATE INDEX idx_events_category_time ON events (category, occurred_at)",
        "CREATE INDEX idx_events_recorded ON events (recorded_at)",
        # Spec 8.3: events are immutable. Corrections are new events
        # (EVENT_INVALIDATED / REINTERPRETATION_CREATED), never an UPDATE.
        # Enforced by the database itself, not only by application code.
        """
        CREATE TRIGGER events_are_immutable_update
        BEFORE UPDATE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are immutable: emit a correction event');
        END
        """,
        """
        CREATE TRIGGER events_are_immutable_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events are immutable: emit an invalidation event');
        END
        """,
        # --- delivery (spec 28.4, idempotency) ------------------------------
        """
        CREATE TABLE event_deliveries (
            delivery_id     TEXT PRIMARY KEY,
            event_id        TEXT NOT NULL REFERENCES events (event_id),
            subscriber      TEXT NOT NULL,
            status          TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            first_attempt_at TEXT,
            last_attempt_at TEXT,
            completed_at    TEXT,
            error           TEXT,
            UNIQUE (event_id, subscriber)
        )
        """,
        "CREATE INDEX idx_deliveries_status ON event_deliveries (status, subscriber)",
        # --- processing runs (spec 7.2) -------------------------------------
        """
        CREATE TABLE processing_runs (
            run_id              TEXT PRIMARY KEY,
            root_event_id       TEXT NOT NULL REFERENCES events (event_id),
            state_snapshot_id   TEXT,
            runtime_manifest_id TEXT REFERENCES runtime_manifests (manifest_id),
            mode                TEXT NOT NULL,
            priority            TEXT NOT NULL,
            status              TEXT NOT NULL,
            started_at          TEXT NOT NULL,
            finished_at         TEXT,
            proposals_received  INTEGER NOT NULL DEFAULT 0,
            proposals_accepted  INTEGER NOT NULL DEFAULT 0,
            proposals_rejected  INTEGER NOT NULL DEFAULT 0,
            error               TEXT
        )
        """,
        "CREATE INDEX idx_runs_root ON processing_runs (root_event_id)",
        "CREATE INDEX idx_runs_status ON processing_runs (status, started_at)",
        # --- state (spec 9) --------------------------------------------------
        """
        CREATE TABLE state_snapshots (
            snapshot_id     TEXT PRIMARY KEY,
            run_id          TEXT,
            root_event_id   TEXT,
            created_at      TEXT NOT NULL,
            domain_count    INTEGER NOT NULL,
            key_count       INTEGER NOT NULL,
            payload_json    TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_snapshots_run ON state_snapshots (run_id)",
        """
        CREATE TABLE state_values (
            domain              TEXT NOT NULL,
            key                 TEXT NOT NULL,
            value_type          TEXT NOT NULL,
            numeric_value       REAL,
            text_value          TEXT,
            json_value          TEXT,
            confidence          REAL,
            version             INTEGER NOT NULL DEFAULT 1,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            updated_by_run_id   TEXT,
            updated_by_event_id TEXT,
            PRIMARY KEY (domain, key)
        )
        """,
        """
        CREATE TABLE state_changes (
            change_id           TEXT PRIMARY KEY,
            run_id              TEXT NOT NULL REFERENCES processing_runs (run_id),
            source_event_id     TEXT NOT NULL REFERENCES events (event_id),
            proposal_id         TEXT NOT NULL,
            source_module       TEXT NOT NULL,
            domain              TEXT NOT NULL,
            key                 TEXT NOT NULL,
            operation           TEXT NOT NULL,
            value_type          TEXT NOT NULL,
            previous_value_json TEXT,
            new_value_json      TEXT,
            delta               REAL,
            confidence          REAL,
            version_after       INTEGER NOT NULL,
            reason_codes_json   TEXT NOT NULL,
            evidence_ids_json   TEXT NOT NULL,
            committed_at        TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_changes_run ON state_changes (run_id)",
        "CREATE INDEX idx_changes_domain_key ON state_changes (domain, key, committed_at)",
        "CREATE UNIQUE INDEX idx_changes_proposal ON state_changes (proposal_id)",
        # --- failures (spec 28, 31.9) ---------------------------------------
        """
        CREATE TABLE failures (
            failure_id      TEXT PRIMARY KEY,
            occurred_at     TEXT NOT NULL,
            failure_type    TEXT NOT NULL,
            severity        TEXT NOT NULL,
            component       TEXT NOT NULL,
            reason_code     TEXT NOT NULL,
            run_id          TEXT,
            event_id        TEXT,
            reference_id    TEXT,
            detail_json     TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_failures_time ON failures (occurred_at)",
        "CREATE INDEX idx_failures_type ON failures (failure_type, reason_code)",
    ),
)


MIGRATIONS: tuple[Migration, ...] = (_0001_CORE,)

LATEST_VERSION = max(migration.version for migration in MIGRATIONS)


def _validate_sequence(migrations: tuple[Migration, ...]) -> None:
    expected = list(range(1, len(migrations) + 1))
    actual = [migration.version for migration in migrations]
    if actual != expected:
        raise MigrationError(f"migration versions must be contiguous from 1: {actual}")


def applied_versions(db: Database) -> dict[int, str]:
    """Return ``{version: checksum}`` for migrations already applied."""
    table = db.query_one(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migrations'"
    )
    if table is None:
        return {}
    return {int(row["version"]): str(row["checksum"]) for row in db.query_all(
        "SELECT version, checksum FROM migrations"
    )}


def schema_version(db: Database) -> int:
    """Current schema version, ``0`` for an empty database."""
    return max(applied_versions(db), default=0)


def migrate(
    db: Database,
    *,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    clock: Clock | None = None,
    target_version: int | None = None,
) -> MigrationResult:
    """Apply pending migrations. Each migration is one transaction."""
    _validate_sequence(migrations)
    now: datetime = (clock or SystemClock()).now()
    already = applied_versions(db)

    for migration in migrations:
        recorded = already.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) was applied with a "
                f"different definition (recorded {recorded[:12]}, current "
                f"{migration.checksum[:12]}); create a new migration instead of editing it"
            )

    ceiling = LATEST_VERSION if target_version is None else target_version
    applied: list[int] = []
    for migration in migrations:
        if migration.version in already or migration.version > ceiling:
            continue
        with db.transaction() as connection:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, to_iso(now)),
            )
            connection.execute(
                """
                INSERT INTO schema_meta (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                                updated_at = excluded.updated_at
                """,
                (SCHEMA_VERSION_KEY, str(migration.version), to_iso(now)),
            )
        applied.append(migration.version)
        logger.info("migration applied version=%s name=%s", migration.version, migration.name)

    return MigrationResult(applied=tuple(applied), schema_version=schema_version(db))


def verify_schema(db: Database, *, expected_version: int = LATEST_VERSION) -> None:
    """Startup guard (spec 32): refuse to run against an unexpected schema."""
    current = schema_version(db)
    if current != expected_version:
        raise MigrationError(
            f"database schema version {current} does not match expected {expected_version}; "
            "run migrations before starting"
        )
