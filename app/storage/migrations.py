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


_0002_LLM_CALLS = Migration(
    version=2,
    name="llm_call_traces",
    statements=(
        # Spec 29: important LLM calls must stay attributable to a model and
        # prompt version, so a behaviour change can be told apart from growth.
        """
        CREATE TABLE llm_calls (
            call_id             TEXT PRIMARY KEY,
            run_id              TEXT,
            event_id            TEXT,
            manifest_id         TEXT REFERENCES runtime_manifests (manifest_id),
            purpose             TEXT NOT NULL,
            priority            TEXT NOT NULL,
            model               TEXT NOT NULL,
            prompt_id           TEXT,
            prompt_version      TEXT,
            structured          INTEGER NOT NULL DEFAULT 0,
            request_fingerprint TEXT NOT NULL,
            request_transcript  TEXT,
            status              TEXT NOT NULL,
            attempts            INTEGER NOT NULL DEFAULT 1,
            rejection_stage     TEXT,
            reason_code         TEXT,
            started_at          TEXT NOT NULL,
            finished_at         TEXT,
            latency_ms          INTEGER,
            prompt_tokens       INTEGER,
            completion_tokens   INTEGER,
            response_text       TEXT,
            error_type          TEXT,
            error_detail        TEXT
        )
        """,
        "CREATE INDEX idx_llm_calls_started ON llm_calls (started_at)",
        "CREATE INDEX idx_llm_calls_purpose ON llm_calls (purpose, started_at)",
        "CREATE INDEX idx_llm_calls_run ON llm_calls (run_id)",
    ),
)


_0003_CONVERSATIONS = Migration(
    version=3,
    name="conversation_projection",
    statements=(
        # Spec 31.4. These tables are a *projection* of the event stream kept
        # for cheap recent-history reads. Events stay authoritative (spec 2.9)
        # and the projection is rebuildable from them.
        """
        CREATE TABLE conversations (
            conversation_id  TEXT PRIMARY KEY,
            channel_id       TEXT NOT NULL UNIQUE,
            channel_type     TEXT NOT NULL,
            started_at       TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            turn_count       INTEGER NOT NULL DEFAULT 0,
            status           TEXT NOT NULL DEFAULT 'active'
        )
        """,
        """
        CREATE TABLE conversation_turns (
            turn_id         TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations (conversation_id),
            event_id        TEXT NOT NULL UNIQUE REFERENCES events (event_id),
            speaker         TEXT NOT NULL,
            author_id       TEXT,
            content         TEXT NOT NULL,
            occurred_at     TEXT NOT NULL,
            message_ref     TEXT
        )
        """,
        "CREATE INDEX idx_turns_conversation ON conversation_turns "
        "(conversation_id, occurred_at)",
    ),
)


_0004_MEMORY = Migration(
    version=4,
    name="subjective_memory",
    statements=(
        # Spec 10.1: this is *subjective* memory. It is deliberately separate
        # from the objective archive in ``events`` — normal recall reads these
        # tables and never the archive.
        """
        CREATE TABLE episodes (
            episode_id      TEXT PRIMARY KEY,
            conversation_id TEXT,
            origin          TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            ended_at        TEXT,
            event_count     INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'open',
            boundary_reason TEXT,
            event_ids_json  TEXT NOT NULL DEFAULT '[]'
        )
        """,
        "CREATE INDEX idx_episodes_status ON episodes (status, started_at)",
        "CREATE INDEX idx_episodes_conversation ON episodes (conversation_id, started_at)",
        # Spec 10.3 fields. accessibility and importance are separate on
        # purpose (spec 10.5): a memory can matter and still be hard to reach.
        """
        CREATE TABLE episodic_memories (
            memory_id           TEXT PRIMARY KEY,
            episode_id          TEXT NOT NULL UNIQUE REFERENCES episodes (episode_id),
            origin              TEXT NOT NULL,
            summary             TEXT NOT NULL,
            topics_json         TEXT NOT NULL DEFAULT '[]',
            importance          REAL NOT NULL,
            emotional_intensity REAL NOT NULL DEFAULT 0.0,
            accessibility       REAL NOT NULL,
            content_confidence  REAL NOT NULL DEFAULT 0.8,
            source_confidence   REAL NOT NULL DEFAULT 0.9,
            temporal_confidence REAL NOT NULL DEFAULT 0.8,
            novelty             REAL NOT NULL DEFAULT 0.0,
            prediction_error    REAL NOT NULL DEFAULT 0.0,
            recall_count        INTEGER NOT NULL DEFAULT 0,
            last_recalled_at    TEXT,
            last_decayed_at     TEXT,
            occurred_at         TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            revision_count      INTEGER NOT NULL DEFAULT 0,
            status              TEXT NOT NULL DEFAULT 'active',
            source_event_ids_json TEXT NOT NULL DEFAULT '[]'
        )
        """,
        "CREATE INDEX idx_memories_status ON episodic_memories (status, occurred_at)",
        "CREATE INDEX idx_memories_origin ON episodic_memories (origin, occurred_at)",
        "CREATE INDEX idx_memories_accessibility ON episodic_memories (accessibility)",
        # Trigram tokenizer: Japanese has no word breaks, so substring search
        # is what actually works here (spec 10.7 starts with FTS5).
        """
        CREATE VIRTUAL TABLE episodic_memories_fts USING fts5(
            memory_id UNINDEXED,
            summary,
            topics,
            tokenize='trigram'
        )
        """,
        """
        CREATE TABLE semantic_memories (
            semantic_id     TEXT PRIMARY KEY,
            statement       TEXT NOT NULL,
            topics_json     TEXT NOT NULL DEFAULT '[]',
            origin          TEXT NOT NULL,
            confidence      REAL NOT NULL DEFAULT 0.5,
            stability       TEXT NOT NULL DEFAULT 'CHANGEABLE',
            support_count   INTEGER NOT NULL DEFAULT 1,
            contradiction_count INTEGER NOT NULL DEFAULT 0,
            first_learned_at TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            source_memory_ids_json TEXT NOT NULL DEFAULT '[]'
        )
        """,
        "CREATE INDEX idx_semantic_status ON semantic_memories (status, updated_at)",
        "CREATE UNIQUE INDEX idx_semantic_statement ON semantic_memories (statement, origin)",
        """
        CREATE TABLE memory_links (
            link_id     TEXT PRIMARY KEY,
            from_memory_id TEXT NOT NULL,
            to_memory_id   TEXT NOT NULL,
            relation    TEXT NOT NULL,
            strength    REAL NOT NULL DEFAULT 0.5,
            created_at  TEXT NOT NULL,
            UNIQUE (from_memory_id, to_memory_id, relation)
        )
        """,
        # Retrieval history feeds retrieval practice (spec 10.5) and makes
        # "why did she remember that" answerable.
        """
        CREATE TABLE memory_retrievals (
            retrieval_id TEXT PRIMARY KEY,
            memory_id    TEXT NOT NULL REFERENCES episodic_memories (memory_id),
            run_id       TEXT,
            event_id     TEXT,
            query        TEXT NOT NULL,
            score        REAL NOT NULL,
            rank         INTEGER NOT NULL,
            used         INTEGER NOT NULL DEFAULT 0,
            retrieved_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_retrievals_memory ON memory_retrievals (memory_id, retrieved_at)",
        # Spec 10.6: a recall may reinterpret, but the history is kept and the
        # original event is never destroyed.
        """
        CREATE TABLE memory_revisions (
            revision_id      TEXT PRIMARY KEY,
            memory_id        TEXT NOT NULL REFERENCES episodic_memories (memory_id),
            revised_at       TEXT NOT NULL,
            reason_code      TEXT NOT NULL,
            previous_summary TEXT NOT NULL,
            new_summary      TEXT NOT NULL,
            confidence_after REAL,
            run_id           TEXT,
            event_id         TEXT
        )
        """,
        "CREATE INDEX idx_revisions_memory ON memory_revisions (memory_id, revised_at)",
    ),
)


_0005_BELIEFS_SELF = Migration(
    version=5,
    name="beliefs_and_self",
    statements=(
        # Spec 31.5. A belief carries content, so unlike a scalar it needs a
        # row of its own; its confidence is derived from evidence, never set.
        """
        CREATE TABLE beliefs (
            belief_id       TEXT PRIMARY KEY,
            statement       TEXT NOT NULL,
            subject         TEXT NOT NULL,
            origin          TEXT NOT NULL,
            confidence      REAL NOT NULL,
            support_weight  REAL NOT NULL DEFAULT 0.0,
            contradiction_weight REAL NOT NULL DEFAULT 0.0,
            status          TEXT NOT NULL DEFAULT 'held',
            first_formed_at TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            revision_count  INTEGER NOT NULL DEFAULT 0,
            UNIQUE (statement, subject, origin)
        )
        """,
        "CREATE INDEX idx_beliefs_subject ON beliefs (subject, status)",
        # Spec 25: evidence is kept for and against, and a repost of the same
        # primary source must not count twice. The unique index is that rule.
        """
        CREATE TABLE belief_evidence (
            evidence_id       TEXT PRIMARY KEY,
            belief_id         TEXT NOT NULL REFERENCES beliefs (belief_id),
            stance            TEXT NOT NULL,
            weight            REAL NOT NULL,
            source_type       TEXT NOT NULL,
            primary_source_id TEXT NOT NULL,
            event_id          TEXT,
            recorded_at       TEXT NOT NULL,
            UNIQUE (belief_id, primary_source_id, stance)
        )
        """,
        "CREATE INDEX idx_belief_evidence_belief ON belief_evidence (belief_id, stance)",
        # Spec 12.4: self schemas are separate from personality, and may be
        # wrong. behaviour_evidence lets the schema lag behind behaviour
        # (spec 23.2) instead of tracking it instantly.
        """
        CREATE TABLE self_schemas (
            schema_id        TEXT PRIMARY KEY,
            name             TEXT NOT NULL UNIQUE,
            statement        TEXT NOT NULL,
            strength         REAL NOT NULL,
            clarity          REAL NOT NULL DEFAULT 0.5,
            supporting_count INTEGER NOT NULL DEFAULT 0,
            contradicting_count INTEGER NOT NULL DEFAULT 0,
            pending_evidence REAL NOT NULL DEFAULT 0.0,
            last_behaviour_at TEXT,
            first_formed_at  TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'active'
        )
        """,
        """
        CREATE TABLE self_event_connections (
            connection_id TEXT PRIMARY KEY,
            schema_id     TEXT NOT NULL REFERENCES self_schemas (schema_id),
            event_id      TEXT NOT NULL,
            relation      TEXT NOT NULL,
            recorded_at   TEXT NOT NULL,
            UNIQUE (schema_id, event_id, relation)
        )
        """,
        "CREATE INDEX idx_self_connections_schema ON self_event_connections (schema_id)",
        # Spec 12.4: who YUI might become. Kept apart from who she thinks she is.
        """
        CREATE TABLE possible_selves (
            possible_self_id TEXT PRIMARY KEY,
            name          TEXT NOT NULL UNIQUE,
            statement     TEXT NOT NULL,
            valence       TEXT NOT NULL,
            salience      REAL NOT NULL DEFAULT 0.3,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'active'
        )
        """,
    ),
)


_0006_TOOLS = Migration(
    version=6,
    name="tool_calls",
    statements=(
        # Spec 26: the execution record is the only authority on whether a tool
        # call happened and whether it worked.
        """
        CREATE TABLE tool_calls (
            call_id      TEXT PRIMARY KEY,
            tool_name    TEXT NOT NULL,
            permission   TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            run_id       TEXT,
            event_id     TEXT,
            arguments_json TEXT NOT NULL,
            status       TEXT NOT NULL,
            success      INTEGER NOT NULL DEFAULT 0,
            source       TEXT,
            data_json    TEXT,
            error        TEXT,
            retryable    INTEGER NOT NULL DEFAULT 0,
            reason       TEXT,
            started_at   TEXT NOT NULL,
            finished_at  TEXT
        )
        """,
        "CREATE INDEX idx_tool_calls_started ON tool_calls (started_at)",
        "CREATE INDEX idx_tool_calls_run ON tool_calls (run_id)",
        "CREATE INDEX idx_tool_calls_tool ON tool_calls (tool_name, success)",
    ),
)


_0007_AGENCY = Migration(
    version=7,
    name="goals_and_habits",
    statements=(
        # Spec 15.2, 31.6. A goal carries why it exists, not just what it is.
        """
        CREATE TABLE goals (
            goal_id        TEXT PRIMARY KEY,
            description    TEXT NOT NULL,
            source         TEXT NOT NULL,
            reason         TEXT NOT NULL DEFAULT '',
            importance     REAL NOT NULL DEFAULT 0.5,
            autonomy       REAL NOT NULL DEFAULT 0.5,
            obligation     REAL NOT NULL DEFAULT 0.0,
            expected_reward REAL NOT NULL DEFAULT 0.5,
            identity_relevance REAL NOT NULL DEFAULT 0.3,
            value_alignment REAL NOT NULL DEFAULT 0.5,
            progress       REAL NOT NULL DEFAULT 0.0,
            status         TEXT NOT NULL DEFAULT 'active',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            last_pursued_at TEXT,
            origin         TEXT NOT NULL DEFAULT 'real_discord',
            UNIQUE (description, source)
        )
        """,
        "CREATE INDEX idx_goals_status ON goals (status, importance)",
        # Spec 18.2: a plan is an intended future and is never a completed
        # experience (spec 2.15). Status transitions are explicit.
        """
        CREATE TABLE plans (
            plan_id     TEXT PRIMARY KEY,
            goal_id     TEXT REFERENCES goals (goal_id),
            description TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'planned',
            planned_for TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            started_at  TEXT,
            completed_at TEXT,
            outcome     TEXT
        )
        """,
        "CREATE INDEX idx_plans_status ON plans (status, planned_for)",
        # Spec 15.3: a habit is cue-triggered automaticity, not a streak.
        """
        CREATE TABLE habits (
            habit_id      TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            cue           TEXT NOT NULL,
            action        TEXT NOT NULL,
            automaticity  REAL NOT NULL DEFAULT 0.0,
            repetitions   INTEGER NOT NULL DEFAULT 0,
            cue_encounters INTEGER NOT NULL DEFAULT 0,
            context_available INTEGER NOT NULL DEFAULT 1,
            last_performed_at TEXT,
            last_cue_at   TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'forming',
            UNIQUE (name, cue)
        )
        """,
        "CREATE INDEX idx_habits_cue ON habits (cue, status)",
        # Spec 15.4: what was chosen, what was expected, what happened.
        """
        CREATE TABLE decisions (
            decision_id  TEXT PRIMARY KEY,
            run_id       TEXT,
            event_id     TEXT,
            chosen_action TEXT NOT NULL,
            route        TEXT NOT NULL,
            expected_value REAL NOT NULL,
            candidates_json TEXT NOT NULL,
            outcome_value REAL,
            prediction_error REAL,
            decided_at   TEXT NOT NULL,
            resolved_at  TEXT
        )
        """,
        "CREATE INDEX idx_decisions_decided ON decisions (decided_at)",
    ),
)


MIGRATIONS: tuple[Migration, ...] = (
    _0001_CORE,
    _0002_LLM_CALLS,
    _0003_CONVERSATIONS,
    _0004_MEMORY,
    _0005_BELIEFS_SELF,
    _0006_TOOLS,
    _0007_AGENCY,
)

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
