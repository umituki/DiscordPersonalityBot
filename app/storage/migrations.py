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


_0008_VIRTUAL_LIFE = Migration(
    version=8,
    name="world_sleep_scheduler",
    statements=(
        # Spec 18.2: routine, plan, current activity and completed event are
        # four different things. An activity row is a *fact about now* that
        # becomes a completed fact only when it ends.
        """
        CREATE TABLE activities (
            activity_id  TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            kind         TEXT NOT NULL,
            location     TEXT,
            plan_id      TEXT,
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            status       TEXT NOT NULL DEFAULT 'ongoing',
            outcome      TEXT,
            origin       TEXT NOT NULL DEFAULT 'virtual_life'
        )
        """,
        "CREATE INDEX idx_activities_status ON activities (status, started_at)",
        # Spec 18.1: the objective world record, compressed to meaningful
        # transitions rather than every tick (spec 8.3).
        """
        CREATE TABLE world_state_history (
            entry_id     TEXT PRIMARY KEY,
            recorded_at  TEXT NOT NULL,
            transition   TEXT NOT NULL,
            awake        INTEGER NOT NULL,
            location     TEXT,
            activity     TEXT,
            detail_json  TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX idx_world_history_time ON world_state_history (recorded_at)",
        # Spec 18.3: a functional two-process record, not a physiology sim.
        """
        CREATE TABLE sleep_episodes (
            sleep_id     TEXT PRIMARY KEY,
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            planned_wake_at TEXT,
            sleep_pressure_at_onset REAL NOT NULL,
            circadian_at_onset REAL NOT NULL,
            quality      REAL,
            interrupted  INTEGER NOT NULL DEFAULT 0,
            reason       TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX idx_sleep_started ON sleep_episodes (started_at)",
        # Spec 19: the scheduler produces opportunities, never actions.
        """
        CREATE TABLE scheduled_jobs (
            job_id        TEXT PRIMARY KEY,
            job_type      TEXT NOT NULL,
            job_class     TEXT NOT NULL,
            due_at        TEXT,
            window_end    TEXT,
            payload_json  TEXT NOT NULL DEFAULT '{}',
            priority      TEXT NOT NULL DEFAULT 'P4',
            status        TEXT NOT NULL DEFAULT 'pending',
            misfire_policy TEXT NOT NULL DEFAULT 'skip',
            expires_at    TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            last_fired_at TEXT,
            attempts      INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX idx_jobs_due ON scheduled_jobs (status, due_at)",
        # Proactive contact history, so frequency can be governed by what
        # actually happened rather than by hope (spec 19).
        """
        CREATE TABLE proactive_contacts (
            contact_id   TEXT PRIMARY KEY,
            opportunity  TEXT NOT NULL,
            sent_at      TEXT NOT NULL,
            answered_at  TEXT,
            event_id     TEXT
        )
        """,
        "CREATE INDEX idx_proactive_sent ON proactive_contacts (sent_at)",
    ),
)


_0009_GROWTH = Migration(
    version=9,
    name="growth_consolidation",
    statements=(
        # Spec 12.2 / 31.7: adaptations sit between traits and expression and
        # are allowed to move faster than traits. ``baseline`` keeps the
        # separation demanded by spec 23.2 (baseline / adaptation / expression).
        """
        CREATE TABLE characteristic_adaptations (
            adaptation_id     TEXT PRIMARY KEY,
            name              TEXT NOT NULL UNIQUE,
            value             REAL NOT NULL,
            baseline          REAL NOT NULL,
            pending_evidence  REAL NOT NULL DEFAULT 0.0,
            supporting_count  INTEGER NOT NULL DEFAULT 0,
            contradicting_count INTEGER NOT NULL DEFAULT 0,
            contexts_json     TEXT NOT NULL DEFAULT '[]',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            first_evidence_at TEXT,
            last_evidence_at  TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
        """,
        # Spec 12.1 / 23.2: the trait baseline is a separate, very slow thing
        # from the currently expressed value, and it is not a permanent
        # constant either — it follows, far behind, what keeps being true.
        """
        CREATE TABLE personality_traits (
            trait_id         TEXT PRIMARY KEY,
            name             TEXT NOT NULL UNIQUE,
            baseline         REAL NOT NULL,
            initial_baseline REAL NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
        """,
        # Spec 31.7: every deep change keeps why it happened.
        """
        CREATE TABLE personality_history (
            entry_id      TEXT PRIMARY KEY,
            trait         TEXT NOT NULL,
            previous_value REAL,
            new_value     REAL NOT NULL,
            baseline_after REAL NOT NULL,
            reason_code   TEXT NOT NULL,
            candidate_id  TEXT,
            run_id        TEXT,
            recorded_at   TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_personality_history ON personality_history (trait, recorded_at)",
        # Spec 12.5: values are a *relative* priority ordering, so the table
        # holds priorities that are renormalised together, never independent
        # scores that can all rise at once. (``values`` is SQL syntax, hence
        # ``value_priorities``.)
        """
        CREATE TABLE value_priorities (
            value_id     TEXT PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            priority     REAL NOT NULL,
            initial_priority REAL NOT NULL,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE value_history (
            entry_id      TEXT PRIMARY KEY,
            value_name    TEXT NOT NULL,
            previous_priority REAL,
            new_priority  REAL NOT NULL,
            reason_code   TEXT NOT NULL,
            candidate_id  TEXT,
            run_id        TEXT,
            recorded_at   TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_value_history ON value_history (value_name, recorded_at)",
        # Spec 12.3: a deep update is a *candidate* until repetition, temporal
        # persistence, cross-context evidence, a meaningful outcome and
        # mood-independence have all accumulated. This table is that waiting
        # room; nothing bypasses it.
        """
        CREATE TABLE deep_update_candidates (
            candidate_id   TEXT PRIMARY KEY,
            target_domain  TEXT NOT NULL,
            target_key     TEXT NOT NULL,
            direction      INTEGER NOT NULL,
            pattern_count  INTEGER NOT NULL DEFAULT 0,
            contexts_json  TEXT NOT NULL DEFAULT '[]',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            outcome_weight REAL NOT NULL DEFAULT 0.0,
            mood_independent_count INTEGER NOT NULL DEFAULT 0,
            magnitude      REAL NOT NULL DEFAULT 0.0,
            source_adaptation TEXT,
            first_seen_at  TEXT NOT NULL,
            last_seen_at   TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'accumulating',
            resolved_at    TEXT,
            blocked_reason TEXT NOT NULL DEFAULT '',
            UNIQUE (target_domain, target_key, direction, status)
        )
        """,
        "CREATE INDEX idx_candidates_status ON deep_update_candidates (status, last_seen_at)",
        # Spec 12.4 / 31.7: recurring themes YUI tells about herself. Slower
        # than behaviour and independent of whether they are accurate.
        """
        CREATE TABLE narrative_identity (
            theme_id      TEXT PRIMARY KEY,
            theme         TEXT NOT NULL UNIQUE,
            statement     TEXT NOT NULL DEFAULT '',
            strength      REAL NOT NULL DEFAULT 0.0,
            supporting_memory_count INTEGER NOT NULL DEFAULT 0,
            supporting_memory_ids_json TEXT NOT NULL DEFAULT '[]',
            first_seen_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'emerging'
        )
        """,
        # Spec 23.3: the monitor observes and classifies. Only INVALID is a
        # rollback candidate, and nothing here clamps ordinary life change.
        """
        CREATE TABLE drift_observations (
            observation_id TEXT PRIMARY KEY,
            metric         TEXT NOT NULL,
            window_start   TEXT NOT NULL,
            window_end     TEXT NOT NULL,
            value          REAL NOT NULL,
            expected_max   REAL NOT NULL,
            classification TEXT NOT NULL,
            detail_json    TEXT NOT NULL DEFAULT '{}',
            recorded_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_drift_metric ON drift_observations (metric, recorded_at)",
        # Spec 9.5: deep state is processed by a consolidation job, separately
        # from the per-event run. This is that job's own ledger.
        """
        CREATE TABLE consolidation_runs (
            consolidation_id TEXT PRIMARY KEY,
            started_at     TEXT NOT NULL,
            ended_at       TEXT,
            kind           TEXT NOT NULL DEFAULT 'routine',
            changes_read   INTEGER NOT NULL DEFAULT 0,
            adaptations_moved INTEGER NOT NULL DEFAULT 0,
            candidates_raised INTEGER NOT NULL DEFAULT 0,
            deep_updates   INTEGER NOT NULL DEFAULT 0,
            semantic_facts INTEGER NOT NULL DEFAULT 0,
            status         TEXT NOT NULL DEFAULT 'running',
            detail_json    TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX idx_consolidation_started ON consolidation_runs (started_at)",
    ),
)


_0010_SOCIETY = Migration(
    version=10,
    name="npc_society",
    statements=(
        # Spec 20.1: three tiers, because not every person in a life is a full
        # agent. Tier 0 is a name and a face; only tier 2 earns a model.
        # Spec 20.2: this table is the NPC's *objective* profile. What YUI
        # believes about them lives in ``npc_models`` and may be wrong.
        """
        CREATE TABLE npcs (
            npc_id       TEXT PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            tier         INTEGER NOT NULL DEFAULT 0,
            role         TEXT NOT NULL DEFAULT '',
            traits_json  TEXT NOT NULL DEFAULT '{}',
            availability REAL NOT NULL DEFAULT 0.5,
            warmth       REAL NOT NULL DEFAULT 0.5,
            reliability  REAL NOT NULL DEFAULT 0.5,
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            origin       TEXT NOT NULL DEFAULT 'virtual_life'
        )
        """,
        "CREATE INDEX idx_npcs_tier ON npcs (tier, status)",
        # Spec 20.2: YUI's model of an NPC. Separate table, separate writer,
        # allowed to disagree with the profile above.
        """
        CREATE TABLE npc_models (
            model_id     TEXT PRIMARY KEY,
            npc_id       TEXT NOT NULL REFERENCES npcs (npc_id),
            perceived_warmth REAL NOT NULL DEFAULT 0.5,
            perceived_reliability REAL NOT NULL DEFAULT 0.5,
            perceived_availability REAL NOT NULL DEFAULT 0.5,
            observation_count INTEGER NOT NULL DEFAULT 0,
            confidence   REAL NOT NULL DEFAULT 0.2,
            updated_at   TEXT NOT NULL,
            UNIQUE (npc_id)
        )
        """,
        # Spec 20.4: the lifecycle is a named stage, and losing contact is not
        # the same thing as falling out.
        """
        CREATE TABLE npc_relationships (
            relationship_id TEXT PRIMARY KEY,
            npc_id       TEXT NOT NULL REFERENCES npcs (npc_id),
            stage        TEXT NOT NULL DEFAULT 'unmet',
            previous_stage TEXT NOT NULL DEFAULT '',
            familiarity  REAL NOT NULL DEFAULT 0.0,
            closeness    REAL NOT NULL DEFAULT 0.0,
            conflict     REAL NOT NULL DEFAULT 0.0,
            interaction_count INTEGER NOT NULL DEFAULT 0,
            first_met_at TEXT,
            last_contact_at TEXT,
            ended_at     TEXT,
            ended_reason TEXT NOT NULL DEFAULT '',
            updated_at   TEXT NOT NULL,
            UNIQUE (npc_id)
        )
        """,
        "CREATE INDEX idx_npc_rel_stage ON npc_relationships (stage, last_contact_at)",
        # Spec 20.3: groups have their own character, and belonging to one is
        # a source of relatedness that is not the USER.
        """
        CREATE TABLE npc_groups (
            group_id     TEXT PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            activity_type TEXT NOT NULL DEFAULT '',
            norms_json   TEXT NOT NULL DEFAULT '[]',
            social_density REAL NOT NULL DEFAULT 0.5,
            competitiveness REAL NOT NULL DEFAULT 0.5,
            warmth       REAL NOT NULL DEFAULT 0.5,
            stability    REAL NOT NULL DEFAULT 0.5,
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE group_memberships (
            membership_id TEXT PRIMARY KEY,
            group_id     TEXT NOT NULL REFERENCES npc_groups (group_id),
            member_type  TEXT NOT NULL DEFAULT 'npc',
            npc_id       TEXT REFERENCES npcs (npc_id),
            role         TEXT NOT NULL DEFAULT 'member',
            joined_at    TEXT NOT NULL,
            left_at      TEXT,
            status       TEXT NOT NULL DEFAULT 'active',
            UNIQUE (group_id, member_type, npc_id)
        )
        """,
        "CREATE INDEX idx_memberships_group ON group_memberships (group_id, status)",
        # Spec 20: NPCs know each other independently of YUI. The society is
        # not a star with YUI at the centre.
        """
        CREATE TABLE npc_social_links (
            link_id      TEXT PRIMARY KEY,
            from_npc_id  TEXT NOT NULL REFERENCES npcs (npc_id),
            to_npc_id    TEXT NOT NULL REFERENCES npcs (npc_id),
            kind         TEXT NOT NULL DEFAULT 'acquaintance',
            strength     REAL NOT NULL DEFAULT 0.3,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            UNIQUE (from_npc_id, to_npc_id)
        )
        """,
        # An interaction with an NPC is a fact about virtual life. It is never
        # mixed with real Discord history (spec 2.10, 34.2-4).
        """
        CREATE TABLE npc_interactions (
            interaction_id TEXT PRIMARY KEY,
            npc_id       TEXT NOT NULL REFERENCES npcs (npc_id),
            group_id     TEXT REFERENCES npc_groups (group_id),
            kind         TEXT NOT NULL DEFAULT 'conversation',
            valence      REAL NOT NULL DEFAULT 0.0,
            summary      TEXT NOT NULL DEFAULT '',
            occurred_at  TEXT NOT NULL,
            event_id     TEXT,
            origin       TEXT NOT NULL DEFAULT 'virtual_life'
        )
        """,
        "CREATE INDEX idx_npc_interactions ON npc_interactions (npc_id, occurred_at)",
    ),
)


_0011_KNOWLEDGE = Migration(
    version=11,
    name="historical_knowledge",
    statements=(
        # Spec 21.1: where a claim came from is part of the claim.
        """
        CREATE TABLE knowledge_sources (
            source_id    TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            kind         TEXT NOT NULL DEFAULT 'reference',
            url          TEXT,
            published_at TEXT,
            reliability  REAL NOT NULL DEFAULT 0.5,
            created_at   TEXT NOT NULL
        )
        """,
        # Spec 21.3: a knowledge candidate without its temporal fields cannot
        # be checked against the past, so the columns are not optional extras.
        """
        CREATE TABLE external_knowledge (
            knowledge_id  TEXT PRIMARY KEY,
            statement     TEXT NOT NULL,
            coverage_class TEXT NOT NULL,
            topic         TEXT NOT NULL DEFAULT '',
            geography     TEXT NOT NULL DEFAULT 'global',
            language      TEXT NOT NULL DEFAULT 'ja',
            available_from TEXT NOT NULL,
            available_until TEXT,
            valid_from    TEXT,
            valid_until   TEXT,
            source_published_at TEXT,
            stability     TEXT NOT NULL DEFAULT 'CHANGEABLE',
            truth_confidence REAL NOT NULL DEFAULT 0.5,
            complexity    REAL NOT NULL DEFAULT 0.5,
            salience      REAL NOT NULL DEFAULT 0.3,
            source_id     TEXT REFERENCES knowledge_sources (source_id),
            version       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            UNIQUE (statement, available_from)
        )
        """,
        "CREATE INDEX idx_knowledge_available ON external_knowledge (available_from)",
        "CREATE INDEX idx_knowledge_class ON external_knowledge (coverage_class, topic)",
        # Spec 21.3 / 31.8: what was believed true changes over time without
        # rewriting what was believed before.
        """
        CREATE TABLE knowledge_versions (
            version_id   TEXT PRIMARY KEY,
            knowledge_id TEXT NOT NULL REFERENCES external_knowledge (knowledge_id),
            version      INTEGER NOT NULL,
            statement    TEXT NOT NULL,
            valid_from   TEXT,
            valid_until  TEXT,
            truth_confidence REAL NOT NULL DEFAULT 0.5,
            reason       TEXT NOT NULL DEFAULT '',
            recorded_at  TEXT NOT NULL,
            UNIQUE (knowledge_id, version)
        )
        """,
        # Spec 21.5: existing in the world is only the first step. An
        # opportunity is not knowledge, and being famous is not knowing.
        """
        CREATE TABLE knowledge_exposure_opportunities (
            opportunity_id TEXT PRIMARY KEY,
            knowledge_id  TEXT NOT NULL REFERENCES external_knowledge (knowledge_id),
            occurred_at   TEXT NOT NULL,
            channel       TEXT NOT NULL DEFAULT 'ambient',
            salience      REAL NOT NULL DEFAULT 0.3,
            reach         REAL NOT NULL DEFAULT 0.5,
            stage_reached TEXT NOT NULL DEFAULT 'existed',
            acquired      INTEGER NOT NULL DEFAULT 0,
            reason        TEXT NOT NULL DEFAULT '',
            simulation_block_id TEXT,
            created_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_exposure_time ON knowledge_exposure_opportunities (occurred_at)",
        # Spec 21.1: the ONLY evidence that YUI knows something. A model that
        # happens to have read the internet is not evidence about YUI.
        """
        CREATE TABLE knowledge_acquisitions (
            acquisition_id TEXT PRIMARY KEY,
            knowledge_id  TEXT NOT NULL REFERENCES external_knowledge (knowledge_id),
            opportunity_id TEXT REFERENCES knowledge_exposure_opportunities (opportunity_id),
            acquired_at   TEXT NOT NULL,
            comprehension REAL NOT NULL DEFAULT 0.5,
            retention     REAL NOT NULL DEFAULT 0.5,
            status        TEXT NOT NULL DEFAULT 'retained',
            semantic_memory_id TEXT,
            origin        TEXT NOT NULL DEFAULT 'simulated_past',
            UNIQUE (knowledge_id, acquired_at)
        )
        """,
        "CREATE INDEX idx_acquisitions_knowledge ON knowledge_acquisitions (knowledge_id)",
        # Spec 21.2: coverage is a job with a period and a class, so gaps are
        # visible rather than silently absent.
        """
        CREATE TABLE historical_coverage_jobs (
            job_id       TEXT PRIMARY KEY,
            coverage_class TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            completed_at TEXT,
            produced_count INTEGER NOT NULL DEFAULT 0,
            detail_json  TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX idx_coverage_status ON historical_coverage_jobs (status, coverage_class)",
    ),
)


_0012_SIMULATION = Migration(
    version=12,
    name="past_simulation",
    statements=(
        # Spec 22.2: the questionnaire produces a temperamental seed and
        # nothing else. There is deliberately no column here for a finished
        # personality, a value ranking, or a set of interests.
        """
        CREATE TABLE temperament_seeds (
            seed_id      TEXT PRIMARY KEY,
            answers_json TEXT NOT NULL DEFAULT '{}',
            temperament_json TEXT NOT NULL DEFAULT '{}',
            avoid_json   TEXT NOT NULL DEFAULT '[]',
            interests_json TEXT NOT NULL DEFAULT '[]',
            created_at   TEXT NOT NULL
        )
        """,
        # Spec 22.3: what may be decided in advance. Hobbies, values, habits
        # and the current personality are results, not scaffold.
        """
        CREATE TABLE life_scaffolds (
            scaffold_id  TEXT PRIMARY KEY,
            seed_id      TEXT NOT NULL REFERENCES temperament_seeds (seed_id),
            period_start TEXT NOT NULL,
            period_end   TEXT NOT NULL,
            environment  TEXT NOT NULL DEFAULT '',
            education_context TEXT NOT NULL DEFAULT '',
            social_density REAL NOT NULL DEFAULT 0.5,
            technology_availability REAL NOT NULL DEFAULT 0.5,
            life_stage   TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE simulation_runs (
            simulation_id TEXT PRIMARY KEY,
            seed_id      TEXT NOT NULL REFERENCES temperament_seeds (seed_id),
            scaffold_id  TEXT NOT NULL REFERENCES life_scaffolds (scaffold_id),
            status       TEXT NOT NULL DEFAULT 'running',
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            simulated_from TEXT NOT NULL,
            simulated_to TEXT NOT NULL,
            blocks_run   INTEGER NOT NULL DEFAULT 0,
            experiences  INTEGER NOT NULL DEFAULT 0,
            first_boot_at TEXT,
            detail_json  TEXT NOT NULL DEFAULT '{}'
        )
        """,
        # Spec 22.5: stable stretches are compressed into blocks; only periods
        # with something in them are expanded.
        """
        CREATE TABLE life_phases (
            phase_id     TEXT PRIMARY KEY,
            simulation_id TEXT NOT NULL REFERENCES simulation_runs (simulation_id),
            name         TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            ended_at     TEXT NOT NULL,
            summary      TEXT NOT NULL DEFAULT '',
            ordinal      INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE simulation_blocks (
            block_id     TEXT PRIMARY KEY,
            simulation_id TEXT NOT NULL REFERENCES simulation_runs (simulation_id),
            phase_id     TEXT REFERENCES life_phases (phase_id),
            started_at   TEXT NOT NULL,
            ended_at     TEXT NOT NULL,
            detail_level TEXT NOT NULL DEFAULT 'compressed',
            experience_class TEXT NOT NULL DEFAULT 'routine',
            summary      TEXT NOT NULL DEFAULT '',
            event_count  INTEGER NOT NULL DEFAULT 0,
            ordinal      INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX idx_blocks_simulation ON simulation_blocks (simulation_id, ordinal)",
        # Spec 22.7: FIRST BOOT is gated on audits that are recorded, so a
        # failed audit is visible rather than a thing somebody remembers.
        """
        CREATE TABLE genesis_audits (
            audit_id     TEXT PRIMARY KEY,
            simulation_id TEXT NOT NULL REFERENCES simulation_runs (simulation_id),
            kind         TEXT NOT NULL,
            passed       INTEGER NOT NULL DEFAULT 0,
            detail_json  TEXT NOT NULL DEFAULT '{}',
            recorded_at  TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_audits_simulation ON genesis_audits (simulation_id, kind)",
    ),
)


_0013_OPERATIONS = Migration(
    version=13,
    name="admin_and_backup",
    statements=(
        # Spec 30 / 31.9: every admin operation is recorded, including the ones
        # that were previewed and never carried out. "We looked first" has to
        # be a row, not a habit.
        """
        CREATE TABLE admin_actions (
            action_id    TEXT PRIMARY KEY,
            operation    TEXT NOT NULL,
            risk_class   TEXT NOT NULL,
            target_type  TEXT NOT NULL,
            target_id    TEXT,
            stage        TEXT NOT NULL DEFAULT 'requested',
            dry_run      INTEGER NOT NULL DEFAULT 1,
            confirmed_by TEXT,
            snapshot_path TEXT,
            impact_json  TEXT NOT NULL DEFAULT '{}',
            result_json  TEXT NOT NULL DEFAULT '{}',
            reason       TEXT NOT NULL DEFAULT '',
            requested_at TEXT NOT NULL,
            completed_at TEXT
        )
        """,
        "CREATE INDEX idx_admin_actions_time ON admin_actions (requested_at)",
        "CREATE INDEX idx_admin_actions_target ON admin_actions (target_type, target_id)",
        # Spec 32: a backup is only a backup once its integrity has been
        # verified, so the verification result lives with the record.
        """
        CREATE TABLE backups (
            backup_id    TEXT PRIMARY KEY,
            kind         TEXT NOT NULL DEFAULT 'manual',
            path         TEXT NOT NULL,
            size_bytes   INTEGER NOT NULL DEFAULT 0,
            schema_version INTEGER NOT NULL DEFAULT 0,
            integrity    TEXT NOT NULL DEFAULT 'unknown',
            restore_tested INTEGER NOT NULL DEFAULT 0,
            reason       TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_backups_created ON backups (created_at)",
    ),
)


_0014_LLM_TELEMETRY = Migration(
    version=14,
    name="llm_call_telemetry",
    statements=(
        # Patch spec 19.1. Without these, "the reply took 4 minutes" cannot be
        # attributed to queueing, model loading, prompt evaluation, generation
        # or reasoning — which is exactly the position the 2026-08-02 run was
        # in. Queue wait and inference are deliberately separate columns.
        "ALTER TABLE llm_calls ADD COLUMN logical_call_id TEXT",
        "ALTER TABLE llm_calls ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE llm_calls ADD COLUMN queue_wait_ms INTEGER",
        "ALTER TABLE llm_calls ADD COLUMN transport_latency_ms INTEGER",
        "ALTER TABLE llm_calls ADD COLUMN model_total_duration_ms INTEGER",
        "ALTER TABLE llm_calls ADD COLUMN load_duration_ms INTEGER",
        "ALTER TABLE llm_calls ADD COLUMN prompt_eval_duration_ms INTEGER",
        "ALTER TABLE llm_calls ADD COLUMN eval_duration_ms INTEGER",
        # Patch spec 3.3: metadata about reasoning only. The reasoning text
        # itself is never stored in a production trace.
        "ALTER TABLE llm_calls ADD COLUMN thinking_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE llm_calls ADD COLUMN thinking_present INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE llm_calls ADD COLUMN thinking_char_count INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX idx_llm_calls_logical ON llm_calls (logical_call_id)",
    ),
)


_0015_RUN_CONFLICTS = Migration(
    version=15,
    name="processing_run_conflicts",
    statements=(
        # Patch spec 7.5. The 2026-08-02 run failed with
        # ConcurrentStateWriteError and left no way to see that a retry had
        # happened, or against which snapshot the first attempt was built.
        "ALTER TABLE processing_runs ADD COLUMN retry_of_run_id TEXT",
        "ALTER TABLE processing_runs ADD COLUMN conflict_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE processing_runs ADD COLUMN commit_attempt INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE processing_runs ADD COLUMN snapshot_fingerprint TEXT",
        "CREATE INDEX idx_runs_retry ON processing_runs (retry_of_run_id)",
    ),
)


_0016_CANDIDATE_HISTORY = Migration(
    version=16,
    name="deep_update_candidate_history",
    statements=(
        # The table-level UNIQUE covered (domain, key, direction, status),
        # which is right for `accumulating` — one open candidate per target and
        # direction — and wrong for everything else. Two candidates for the
        # same trait that both eventually expired collided, and consolidation
        # crashed on the second one. Patch spec 14 made consolidation run many
        # times during a Genesis, which is how a latent conflict became a
        # reliable one.
        #
        # Resolved candidates are history and history accumulates, so the
        # constraint becomes a partial unique index over the open state only.
        # Every existing row is carried across; nothing is dropped.
        """
        CREATE TABLE deep_update_candidates_new (
            candidate_id   TEXT PRIMARY KEY,
            target_domain  TEXT NOT NULL,
            target_key     TEXT NOT NULL,
            direction      INTEGER NOT NULL,
            pattern_count  INTEGER NOT NULL DEFAULT 0,
            contexts_json  TEXT NOT NULL DEFAULT '[]',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            outcome_weight REAL NOT NULL DEFAULT 0.0,
            mood_independent_count INTEGER NOT NULL DEFAULT 0,
            magnitude      REAL NOT NULL DEFAULT 0.0,
            source_adaptation TEXT,
            first_seen_at  TEXT NOT NULL,
            last_seen_at   TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'accumulating',
            resolved_at    TEXT,
            blocked_reason TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        INSERT INTO deep_update_candidates_new
        SELECT candidate_id, target_domain, target_key, direction, pattern_count,
               contexts_json, evidence_ids_json, outcome_weight,
               mood_independent_count, magnitude, source_adaptation,
               first_seen_at, last_seen_at, status, resolved_at, blocked_reason
          FROM deep_update_candidates
        """,
        "DROP TABLE deep_update_candidates",
        "ALTER TABLE deep_update_candidates_new RENAME TO deep_update_candidates",
        "CREATE INDEX idx_candidates_status ON deep_update_candidates (status, last_seen_at)",
        """
        CREATE UNIQUE INDEX idx_candidates_open
            ON deep_update_candidates (target_domain, target_key, direction)
         WHERE status = 'accumulating'
        """,
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
    _0008_VIRTUAL_LIFE,
    _0009_GROWTH,
    _0010_SOCIETY,
    _0011_KNOWLEDGE,
    _0012_SIMULATION,
    _0013_OPERATIONS,
    _0014_LLM_TELEMETRY,
    _0015_RUN_CONFLICTS,
    _0016_CANDIDATE_HISTORY,
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
