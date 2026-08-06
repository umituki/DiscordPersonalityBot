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


# --- 0016: resolved candidates are history (patch spec 14 fallout) ----------
def _candidate(candidates, clock):
    return candidates.create(
        domain="personality",
        key="openness",
        direction=1,
        source_adaptation=None,
        now=clock.now(),
    )


def test_two_expired_candidates_for_one_target_can_coexist(db, clock) -> None:
    """The UNIQUE constraint covered resolved statuses and should not have.

    Consolidation crashed on the second expiry for the same trait. Patch spec
    14 made consolidation run many times during a Genesis, which turned a
    latent conflict into a reliable one.
    """
    from app.storage.repositories.growth import CandidateRepository

    candidates = CandidateRepository(db)
    first = _candidate(candidates, clock)
    candidates.resolve(first.candidate_id, "expired", now=clock.now())
    second = _candidate(candidates, clock)
    candidates.resolve(second.candidate_id, "expired", now=clock.now())

    assert first.candidate_id != second.candidate_id
    assert candidates.get(first.candidate_id).status == "expired"
    assert candidates.get(second.candidate_id).status == "expired"


def test_only_one_candidate_is_open_per_target_and_direction(db, clock) -> None:
    """The part of the constraint that was right is kept."""
    import sqlite3

    import pytest

    from app.storage.repositories.growth import CandidateRepository

    candidates = CandidateRepository(db)
    _candidate(candidates, clock)

    with pytest.raises(sqlite3.IntegrityError):
        _candidate(candidates, clock)

    assert len(candidates.accumulating()) == 1


# --- 0035: a semantic memory's identity includes what it is about -----------
#
# Migration 34 added the `subject` column and the repository began treating
# (statement, origin, subject) as the identity. The unique index still said
# (statement, origin), so storing the same proposition about her and about the
# world raised IntegrityError — the code's notion of identity and the
# constraint enforcing it disagreed.


def _unique_index_columns(db: Database, name: str) -> list[str]:
    return [row["name"] for row in db.query_all(f"PRAGMA index_info({name})")]


def _semantic_indexes(db: Database) -> dict[str, int]:
    """``{index name: is_unique}`` for the semantic memory table."""
    return {
        row["name"]: row["unique"]
        for row in db.query_all("PRAGMA index_list(semantic_memories)")
    }


def _at_version(tmp_path: Path, clock: FixedClock, version: int, name: str) -> Database:
    database = Database(tmp_path / name, synchronous="OFF")
    database.connect()
    migrate(database, clock=clock, target_version=version)
    assert schema_version(database) == version
    return database


def _insert_semantic(db: Database, statement: str, *, columns: str, values: tuple):
    db.execute(
        f"INSERT INTO semantic_memories "
        f"(semantic_id, statement, topics_json, origin, {columns} "
        f" confidence, stability, support_count, contradiction_count, "
        f" first_learned_at, updated_at, status, source_memory_ids_json) "
        f"VALUES (?, ?, '[]', 'virtual_life', {'?, ' * len(values)}"
        f" 0.5, 'CHANGEABLE', 1, 0, '2026-01-01T00:00:00+00:00', "
        f" '2026-01-01T00:00:00+00:00', 'active', '[]')",
        (f"sem_{statement}", statement, *values),
    )


def test_a_fresh_database_keys_semantic_memory_by_subject(db: Database) -> None:
    """A. The unique index on a database built from scratch."""
    assert schema_version(db) == LATEST_VERSION
    assert _semantic_indexes(db)["idx_semantic_statement"] == 1, "the index is not unique"
    assert _unique_index_columns(db, "idx_semantic_statement") == [
        "statement",
        "origin",
        "subject",
    ]


def test_the_same_proposition_about_two_subjects_coexists(db: Database) -> None:
    """The behaviour the index exists for, at the SQL boundary."""
    _insert_semantic(db, "読書は落ち着く_yui", columns="subject,", values=("yui",))
    db.execute(
        "UPDATE semantic_memories SET statement = '読書は落ち着く' "
        "WHERE semantic_id = 'sem_読書は落ち着く_yui'"
    )
    _insert_semantic(db, "読書は落ち着く_world", columns="subject,", values=("world",))
    db.execute(
        "UPDATE semantic_memories SET statement = '読書は落ち着く' "
        "WHERE semantic_id = 'sem_読書は落ち着く_world'"
    )

    rows = db.query_all(
        "SELECT subject FROM semantic_memories WHERE statement = '読書は落ち着く' "
        "ORDER BY subject"
    )
    assert [row["subject"] for row in rows] == ["world", "yui"]


def test_the_same_statement_origin_and_subject_still_collides(db: Database) -> None:
    """Narrowed, not removed. Identity is three columns, and it is still an
    identity — the same fact about the same subject is one row."""
    import sqlite3

    _insert_semantic(db, "同じ命題", columns="subject,", values=("yui",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO semantic_memories "
            "(semantic_id, statement, topics_json, origin, subject, confidence, "
            " stability, support_count, contradiction_count, first_learned_at, "
            " updated_at, status, source_memory_ids_json) "
            "VALUES ('sem_dup', '同じ命題', '[]', 'virtual_life', 'yui', 0.5, "
            " 'CHANGEABLE', 1, 0, '2026-01-01T00:00:00+00:00', "
            " '2026-01-01T00:00:00+00:00', 'active', '[]')"
        )


def test_a_schema_34_database_upgrades_without_losing_rows(
    tmp_path: Path, clock: FixedClock
) -> None:
    """B. 34 → 35."""
    database = _at_version(tmp_path, clock, 34, "at34.db")
    try:
        assert _unique_index_columns(database, "idx_semantic_statement") == [
            "statement",
            "origin",
        ], "the fixture is not actually at the old index"
        _insert_semantic(database, "既存の知識", columns="subject,", values=("yui",))

        result = migrate(database, clock=clock)

        assert result.applied == (35, 36, 37, 38, 39)
        assert schema_version(database) == LATEST_VERSION
        assert _unique_index_columns(database, "idx_semantic_statement") == [
            "statement",
            "origin",
            "subject",
        ]
        row = database.query_one(
            "SELECT statement, subject FROM semantic_memories WHERE statement = '既存の知識'"
        )
        assert row is not None, "an existing semantic memory was lost"
        assert row["subject"] == "yui"
    finally:
        database.close()


def test_a_schema_33_database_applies_both_migrations(
    tmp_path: Path, clock: FixedClock
) -> None:
    """C. 33 → 34 → 35, with a row written before either existed."""
    database = _at_version(tmp_path, clock, 33, "at33.db")
    try:
        columns = {
            row["name"]
            for row in database.query_all("PRAGMA table_info(semantic_memories)")
        }
        assert "subject" not in columns, "the fixture is not actually at 33"
        _insert_semantic(database, "移行前の知識", columns="", values=())

        result = migrate(database, clock=clock)

        assert result.applied == (34, 35, 36, 37, 38, 39)
        assert schema_version(database) == LATEST_VERSION
        row = database.query_one(
            "SELECT subject FROM semantic_memories WHERE statement = '移行前の知識'"
        )
        assert row is not None, "a pre-migration semantic memory was lost"
        assert row["subject"] == "unknown", (
            "a row written before anything recorded provenance was given one"
        )
    finally:
        database.close()


def test_no_migration_before_the_latest_was_edited() -> None:
    """D. Checksums are how drift is detected, so fixing 34's consequences by
    rewriting 34 would turn every database that already ran it into an error.

    Pinned literally rather than recomputed, so an edit to any earlier
    migration fails here rather than silently agreeing with itself.
    """
    checksums = {migration.version: migration.checksum for migration in MIGRATIONS}

    assert checksums[4].startswith("8e50185cc5e1"), "migration 4 was edited"
    assert checksums[33].startswith("be97b2c35e5a"), "migration 33 was edited"
    assert checksums[34].startswith("af83c6ad2b13"), "migration 34 was edited"
    assert checksums[35].startswith("56eef5bd5488"), "migration 35 was edited"
    assert checksums[36].startswith("390124beb257"), "migration 36 was edited"
    assert checksums[37].startswith("2ef139ec9562"), "migration 37 was edited"
    assert checksums[38].startswith("59de309fbc45"), "migration 38 was edited"
    assert LATEST_VERSION == 39
    assert MIGRATIONS[-1].version == 39
    assert MIGRATIONS[-1].name == "annual_synthesis_provenance"
