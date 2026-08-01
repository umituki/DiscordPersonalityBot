"""Migrations and the database boundary (spec 31)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.clock import FixedClock
from app.storage.database import Database, DatabaseError
from app.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    applied_versions,
    migrate,
    schema_version,
    verify_schema,
)

PHASE1_TABLES = {
    "schema_meta",
    "migrations",
    "runtime_manifests",
    "events",
    "event_deliveries",
    "processing_runs",
    "state_snapshots",
    "state_changes",
    "state_values",
    "failures",
}


def _tables(db: Database) -> set[str]:
    return {row["name"] for row in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}


def test_migration_creates_phase1_tables(db: Database) -> None:
    assert PHASE1_TABLES <= _tables(db)
    assert schema_version(db) == LATEST_VERSION


def test_migration_is_idempotent(db: Database, clock: FixedClock) -> None:
    result = migrate(db, clock=clock)
    assert result.applied == ()
    assert result.schema_version == LATEST_VERSION


def test_editing_an_applied_migration_is_rejected(db: Database, clock: FixedClock) -> None:
    tampered = (
        Migration(
            version=MIGRATIONS[0].version,
            name=MIGRATIONS[0].name,
            statements=MIGRATIONS[0].statements + ("CREATE TABLE sneaky (id TEXT)",),
        ),
    )
    with pytest.raises(MigrationError, match="different definition"):
        migrate(db, migrations=tampered, clock=clock)
    assert "sneaky" not in _tables(db)


def test_verify_schema_guards_startup(tmp_path: Path) -> None:
    with Database(tmp_path / "empty.db") as fresh:
        with pytest.raises(MigrationError):
            verify_schema(fresh)


def test_applied_versions_records_checksums(db: Database) -> None:
    recorded = applied_versions(db)
    assert recorded[1] == MIGRATIONS[0].checksum


def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO schema_meta (key, value, updated_at) VALUES ('x', 'y', 'z')"
            )
            raise RuntimeError("boom")
    assert db.query_one("SELECT 1 FROM schema_meta WHERE key = 'x'") is None


def test_nested_transactions_commit_once(db: Database) -> None:
    with db.transaction():
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO schema_meta (key, value, updated_at) VALUES ('a', 'b', 'c')"
            )
        assert db.in_transaction
    assert db.query_one("SELECT 1 FROM schema_meta WHERE key = 'a'") is not None


def test_nested_failure_rolls_back_everything(db: Database) -> None:
    with pytest.raises(RuntimeError):
        with db.transaction() as outer:
            outer.execute("INSERT INTO schema_meta (key, value, updated_at) VALUES ('p','q','r')")
            with db.transaction() as inner:
                inner.execute(
                    "INSERT INTO schema_meta (key, value, updated_at) VALUES ('s','t','u')"
                )
                raise RuntimeError("inner boom")
    assert db.query_one("SELECT 1 FROM schema_meta WHERE key IN ('p','s')") is None


def test_cannot_close_inside_transaction(db: Database) -> None:
    with pytest.raises(DatabaseError):
        with db.transaction():
            db.close()


def test_integrity_check_passes(db: Database) -> None:
    assert db.integrity_check() == "ok"
