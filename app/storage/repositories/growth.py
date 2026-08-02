"""Growth persistence (spec 12, 23, 31.7)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Sequence

from app import ids
from app.clock import from_iso, to_iso
from app.consolidation.models import (
    CharacteristicAdaptation,
    ConsolidationRunRecord,
    DeepUpdateCandidate,
    DriftObservation,
    NarrativeTheme,
    PersonalityTrait,
    ValuePriority,
)
from app.storage.database import Database

ADAPTATION = "adp"
TRAIT = "trt"
VALUE = "val"
CANDIDATE = "dcd"
THEME = "thm"
DRIFT = "drf"
CONSOLIDATION = "cns"
HISTORY = "hst"


def _json_list(raw: str | None) -> tuple[str, ...]:
    return tuple(json.loads(raw)) if raw else ()


def _dump(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


class AdaptationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def ensure(self, name: str, *, initial: float, now: datetime) -> CharacteristicAdaptation:
        self._db.execute(
            """
            INSERT OR IGNORE INTO characteristic_adaptations
                (adaptation_id, name, value, baseline, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ids.new_id(ADAPTATION), name, initial, initial, to_iso(now), to_iso(now)),
        )
        return self.by_name(name)  # type: ignore[return-value]

    def update(
        self,
        *,
        name: str,
        value: float,
        pending_evidence: float,
        supporting_count: int,
        contradicting_count: int,
        contexts: Sequence[str],
        evidence_ids: Sequence[str],
        now: datetime,
        first_evidence_at: datetime | None = None,
    ) -> CharacteristicAdaptation:
        self._db.execute(
            """
            UPDATE characteristic_adaptations
               SET value = ?, pending_evidence = ?, supporting_count = ?,
                   contradicting_count = ?, contexts_json = ?, evidence_ids_json = ?,
                   last_evidence_at = ?,
                   first_evidence_at = COALESCE(first_evidence_at, ?), updated_at = ?
             WHERE name = ?
            """,
            (
                value, pending_evidence, supporting_count, contradicting_count,
                _dump(contexts), _dump(evidence_ids), to_iso(now),
                to_iso(first_evidence_at or now), to_iso(now), name,
            ),
        )
        return self.by_name(name)  # type: ignore[return-value]

    def by_name(self, name: str) -> CharacteristicAdaptation | None:
        row = self._db.query_one(
            "SELECT * FROM characteristic_adaptations WHERE name = ?", (name,)
        )
        return None if row is None else _to_adaptation(row)

    def all(self) -> list[CharacteristicAdaptation]:
        rows = self._db.query_all("SELECT * FROM characteristic_adaptations ORDER BY name")
        return [_to_adaptation(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM characteristic_adaptations") or 0)


class PersonalityRepository:
    """Trait baselines plus the history of every deep change."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def seed(self, temperament: dict[str, float], *, now: datetime) -> list[PersonalityTrait]:
        for name, baseline in temperament.items():
            self._db.execute(
                """
                INSERT OR IGNORE INTO personality_traits
                    (trait_id, name, baseline, initial_baseline, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ids.new_id(TRAIT), name, baseline, baseline, to_iso(now), to_iso(now)),
            )
        return self.all()

    def set_baseline(self, name: str, *, baseline: float, now: datetime) -> PersonalityTrait:
        self._db.execute(
            "UPDATE personality_traits SET baseline = ?, updated_at = ? WHERE name = ?",
            (baseline, to_iso(now), name),
        )
        return self.by_name(name)  # type: ignore[return-value]

    def by_name(self, name: str) -> PersonalityTrait | None:
        row = self._db.query_one("SELECT * FROM personality_traits WHERE name = ?", (name,))
        return None if row is None else _to_trait(row)

    def all(self) -> list[PersonalityTrait]:
        rows = self._db.query_all("SELECT * FROM personality_traits ORDER BY name")
        return [_to_trait(row) for row in rows]

    def record_change(
        self,
        *,
        trait: str,
        previous_value: float | None,
        new_value: float,
        baseline_after: float,
        reason_code: str,
        candidate_id: str | None,
        run_id: str | None,
        now: datetime,
    ) -> str:
        entry_id = ids.new_id(HISTORY)
        self._db.execute(
            """
            INSERT INTO personality_history
                (entry_id, trait, previous_value, new_value, baseline_after, reason_code,
                 candidate_id, run_id, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id, trait, previous_value, new_value, baseline_after, reason_code,
                candidate_id, run_id, to_iso(now),
            ),
        )
        return entry_id

    def history(self, trait: str | None = None, *, limit: int = 50) -> list[sqlite3.Row]:
        if trait is None:
            return self._db.query_all(
                "SELECT * FROM personality_history ORDER BY recorded_at DESC LIMIT ?", (limit,)
            )
        return self._db.query_all(
            "SELECT * FROM personality_history WHERE trait = ? "
            "ORDER BY recorded_at DESC LIMIT ?",
            (trait, limit),
        )


class ValueRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def seed(self, priorities: dict[str, float], *, now: datetime) -> list[ValuePriority]:
        for name, priority in priorities.items():
            self._db.execute(
                """
                INSERT OR IGNORE INTO value_priorities
                    (value_id, name, priority, initial_priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ids.new_id(VALUE), name, priority, priority, to_iso(now), to_iso(now)),
            )
        return self.all()

    def set_priority(self, name: str, *, priority: float, now: datetime) -> ValuePriority:
        self._db.execute(
            "UPDATE value_priorities SET priority = ?, updated_at = ? WHERE name = ?",
            (priority, to_iso(now), name),
        )
        return self.by_name(name)  # type: ignore[return-value]

    def by_name(self, name: str) -> ValuePriority | None:
        row = self._db.query_one("SELECT * FROM value_priorities WHERE name = ?", (name,))
        return None if row is None else _to_value(row)

    def all(self) -> list[ValuePriority]:
        rows = self._db.query_all("SELECT * FROM value_priorities ORDER BY name")
        return [_to_value(row) for row in rows]

    def record_change(
        self,
        *,
        value_name: str,
        previous_priority: float | None,
        new_priority: float,
        reason_code: str,
        candidate_id: str | None,
        run_id: str | None,
        now: datetime,
    ) -> str:
        entry_id = ids.new_id(HISTORY)
        self._db.execute(
            """
            INSERT INTO value_history
                (entry_id, value_name, previous_priority, new_priority, reason_code,
                 candidate_id, run_id, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id, value_name, previous_priority, new_priority, reason_code,
                candidate_id, run_id, to_iso(now),
            ),
        )
        return entry_id

    def history(self, *, limit: int = 50) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM value_history ORDER BY recorded_at DESC LIMIT ?", (limit,)
        )


class CandidateRepository:
    """The waiting room in front of Layer 4 (spec 12.3)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def open_candidate(
        self, *, domain: str, key: str, direction: int
    ) -> DeepUpdateCandidate | None:
        row = self._db.query_one(
            "SELECT * FROM deep_update_candidates WHERE target_domain = ? AND target_key = ? "
            "AND direction = ? AND status = 'accumulating'",
            (domain, key, direction),
        )
        return None if row is None else _to_candidate(row)

    def create(
        self,
        *,
        domain: str,
        key: str,
        direction: int,
        source_adaptation: str | None,
        now: datetime,
    ) -> DeepUpdateCandidate:
        candidate_id = ids.new_id(CANDIDATE)
        self._db.execute(
            """
            INSERT INTO deep_update_candidates
                (candidate_id, target_domain, target_key, direction, source_adaptation,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (candidate_id, domain, key, direction, source_adaptation, to_iso(now), to_iso(now)),
        )
        return self.get(candidate_id)  # type: ignore[return-value]

    def accumulate(
        self,
        candidate_id: str,
        *,
        pattern_count: int,
        contexts: Sequence[str],
        evidence_ids: Sequence[str],
        outcome_weight: float,
        mood_independent_count: int,
        magnitude: float,
        now: datetime,
        blocked_reason: str = "",
    ) -> DeepUpdateCandidate:
        self._db.execute(
            """
            UPDATE deep_update_candidates
               SET pattern_count = ?, contexts_json = ?, evidence_ids_json = ?,
                   outcome_weight = ?, mood_independent_count = ?, magnitude = ?,
                   last_seen_at = ?, blocked_reason = ?
             WHERE candidate_id = ?
            """,
            (
                pattern_count, _dump(contexts), _dump(evidence_ids), outcome_weight,
                mood_independent_count, magnitude, to_iso(now), blocked_reason, candidate_id,
            ),
        )
        return self.get(candidate_id)  # type: ignore[return-value]

    def resolve(self, candidate_id: str, status: str, *, now: datetime) -> DeepUpdateCandidate:
        self._db.execute(
            "UPDATE deep_update_candidates SET status = ?, resolved_at = ? WHERE candidate_id = ?",
            (status, to_iso(now), candidate_id),
        )
        return self.get(candidate_id)  # type: ignore[return-value]

    def get(self, candidate_id: str) -> DeepUpdateCandidate | None:
        row = self._db.query_one(
            "SELECT * FROM deep_update_candidates WHERE candidate_id = ?", (candidate_id,)
        )
        return None if row is None else _to_candidate(row)

    def accumulating(self, *, limit: int = 100) -> list[DeepUpdateCandidate]:
        rows = self._db.query_all(
            "SELECT * FROM deep_update_candidates WHERE status = 'accumulating' "
            "ORDER BY first_seen_at LIMIT ?",
            (limit,),
        )
        return [_to_candidate(row) for row in rows]

    def by_status(self, status: str, *, limit: int = 100) -> list[DeepUpdateCandidate]:
        rows = self._db.query_all(
            "SELECT * FROM deep_update_candidates WHERE status = ? "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (status, limit),
        )
        return [_to_candidate(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM deep_update_candidates") or 0)


class NarrativeRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def ensure(self, theme: str, *, statement: str, now: datetime) -> NarrativeTheme:
        self._db.execute(
            """
            INSERT OR IGNORE INTO narrative_identity
                (theme_id, theme, statement, first_seen_at, last_updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ids.new_id(THEME), theme, statement, to_iso(now), to_iso(now)),
        )
        return self.by_theme(theme)  # type: ignore[return-value]

    def update(
        self,
        theme: str,
        *,
        strength: float,
        supporting_memory_ids: Sequence[str],
        status: str,
        now: datetime,
        statement: str | None = None,
    ) -> NarrativeTheme:
        self._db.execute(
            """
            UPDATE narrative_identity
               SET strength = ?, supporting_memory_count = ?, supporting_memory_ids_json = ?,
                   status = ?, last_updated_at = ?,
                   statement = COALESCE(?, statement)
             WHERE theme = ?
            """,
            (
                strength, len(supporting_memory_ids), _dump(supporting_memory_ids),
                status, to_iso(now), statement, theme,
            ),
        )
        return self.by_theme(theme)  # type: ignore[return-value]

    def by_theme(self, theme: str) -> NarrativeTheme | None:
        row = self._db.query_one("SELECT * FROM narrative_identity WHERE theme = ?", (theme,))
        return None if row is None else _to_theme(row)

    def all(self, *, limit: int = 50) -> list[NarrativeTheme]:
        rows = self._db.query_all(
            "SELECT * FROM narrative_identity ORDER BY strength DESC LIMIT ?", (limit,)
        )
        return [_to_theme(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM narrative_identity") or 0)


class DriftRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        metric: str,
        window_start: datetime,
        window_end: datetime,
        value: float,
        expected_max: float,
        classification: str,
        detail: dict[str, Any],
        now: datetime,
    ) -> DriftObservation:
        observation_id = ids.new_id(DRIFT)
        self._db.execute(
            """
            INSERT INTO drift_observations
                (observation_id, metric, window_start, window_end, value, expected_max,
                 classification, detail_json, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id, metric, to_iso(window_start), to_iso(window_end), value,
                expected_max, classification,
                json.dumps(detail, ensure_ascii=False, default=str), to_iso(now),
            ),
        )
        return self.get(observation_id)  # type: ignore[return-value]

    def get(self, observation_id: str) -> DriftObservation | None:
        row = self._db.query_one(
            "SELECT * FROM drift_observations WHERE observation_id = ?", (observation_id,)
        )
        return None if row is None else _to_drift(row)

    def recent(self, *, limit: int = 50) -> list[DriftObservation]:
        rows = self._db.query_all(
            "SELECT * FROM drift_observations ORDER BY recorded_at DESC LIMIT ?", (limit,)
        )
        return [_to_drift(row) for row in rows]

    def by_classification(self, classification: str, *, limit: int = 50) -> list[DriftObservation]:
        rows = self._db.query_all(
            "SELECT * FROM drift_observations WHERE classification = ? "
            "ORDER BY recorded_at DESC LIMIT ?",
            (classification, limit),
        )
        return [_to_drift(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM drift_observations") or 0)


class ConsolidationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(self, *, kind: str, now: datetime) -> ConsolidationRunRecord:
        consolidation_id = ids.new_id(CONSOLIDATION)
        self._db.execute(
            "INSERT INTO consolidation_runs (consolidation_id, started_at, kind) VALUES (?, ?, ?)",
            (consolidation_id, to_iso(now), kind),
        )
        return self.get(consolidation_id)  # type: ignore[return-value]

    def finish(
        self,
        consolidation_id: str,
        *,
        status: str,
        changes_read: int,
        adaptations_moved: int,
        candidates_raised: int,
        deep_updates: int,
        semantic_facts: int,
        detail: dict[str, Any],
        now: datetime,
    ) -> ConsolidationRunRecord:
        self._db.execute(
            """
            UPDATE consolidation_runs
               SET ended_at = ?, status = ?, changes_read = ?, adaptations_moved = ?,
                   candidates_raised = ?, deep_updates = ?, semantic_facts = ?, detail_json = ?
             WHERE consolidation_id = ?
            """,
            (
                to_iso(now), status, changes_read, adaptations_moved, candidates_raised,
                deep_updates, semantic_facts,
                json.dumps(detail, ensure_ascii=False, default=str), consolidation_id,
            ),
        )
        return self.get(consolidation_id)  # type: ignore[return-value]

    def get(self, consolidation_id: str) -> ConsolidationRunRecord | None:
        row = self._db.query_one(
            "SELECT * FROM consolidation_runs WHERE consolidation_id = ?", (consolidation_id,)
        )
        return None if row is None else _to_consolidation(row)

    def last_completed_at(self) -> datetime | None:
        value = self._db.scalar(
            "SELECT MAX(ended_at) FROM consolidation_runs WHERE status = 'completed'"
        )
        return None if value is None else from_iso(value)

    def recent(self, *, limit: int = 20) -> list[ConsolidationRunRecord]:
        rows = self._db.query_all(
            "SELECT * FROM consolidation_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [_to_consolidation(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM consolidation_runs") or 0)


# --- row mapping -----------------------------------------------------------
def _optional_dt(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


def _to_adaptation(row: sqlite3.Row) -> CharacteristicAdaptation:
    return CharacteristicAdaptation(
        adaptation_id=row["adaptation_id"],
        name=row["name"],
        value=row["value"],
        baseline=row["baseline"],
        pending_evidence=row["pending_evidence"],
        supporting_count=int(row["supporting_count"]),
        contradicting_count=int(row["contradicting_count"]),
        contexts=_json_list(row["contexts_json"]),
        evidence_ids=_json_list(row["evidence_ids_json"]),
        first_evidence_at=_optional_dt(row["first_evidence_at"]),
        last_evidence_at=_optional_dt(row["last_evidence_at"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _to_trait(row: sqlite3.Row) -> PersonalityTrait:
    return PersonalityTrait(
        trait_id=row["trait_id"],
        name=row["name"],
        baseline=row["baseline"],
        initial_baseline=row["initial_baseline"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _to_value(row: sqlite3.Row) -> ValuePriority:
    return ValuePriority(
        value_id=row["value_id"],
        name=row["name"],
        priority=row["priority"],
        initial_priority=row["initial_priority"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _to_candidate(row: sqlite3.Row) -> DeepUpdateCandidate:
    return DeepUpdateCandidate(
        candidate_id=row["candidate_id"],
        target_domain=row["target_domain"],
        target_key=row["target_key"],
        direction=int(row["direction"]),
        pattern_count=int(row["pattern_count"]),
        contexts=_json_list(row["contexts_json"]),
        evidence_ids=_json_list(row["evidence_ids_json"]),
        outcome_weight=row["outcome_weight"],
        mood_independent_count=int(row["mood_independent_count"]),
        magnitude=row["magnitude"],
        source_adaptation=row["source_adaptation"],
        first_seen_at=from_iso(row["first_seen_at"]),
        last_seen_at=from_iso(row["last_seen_at"]),
        status=row["status"],
        resolved_at=_optional_dt(row["resolved_at"]),
        blocked_reason=row["blocked_reason"],
    )


def _to_theme(row: sqlite3.Row) -> NarrativeTheme:
    return NarrativeTheme(
        theme_id=row["theme_id"],
        theme=row["theme"],
        statement=row["statement"],
        strength=row["strength"],
        supporting_memory_count=int(row["supporting_memory_count"]),
        supporting_memory_ids=_json_list(row["supporting_memory_ids_json"]),
        first_seen_at=from_iso(row["first_seen_at"]),
        last_updated_at=from_iso(row["last_updated_at"]),
        status=row["status"],
    )


def _to_drift(row: sqlite3.Row) -> DriftObservation:
    return DriftObservation(
        observation_id=row["observation_id"],
        metric=row["metric"],
        window_start=from_iso(row["window_start"]),
        window_end=from_iso(row["window_end"]),
        value=row["value"],
        expected_max=row["expected_max"],
        classification=row["classification"],
        detail=json.loads(row["detail_json"] or "{}"),
        recorded_at=from_iso(row["recorded_at"]),
    )


def _to_consolidation(row: sqlite3.Row) -> ConsolidationRunRecord:
    return ConsolidationRunRecord(
        consolidation_id=row["consolidation_id"],
        started_at=from_iso(row["started_at"]),
        ended_at=_optional_dt(row["ended_at"]),
        kind=row["kind"],
        changes_read=int(row["changes_read"]),
        adaptations_moved=int(row["adaptations_moved"]),
        candidates_raised=int(row["candidates_raised"]),
        deep_updates=int(row["deep_updates"]),
        semantic_facts=int(row["semantic_facts"]),
        status=row["status"],
    )
