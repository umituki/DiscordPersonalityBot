"""Goals, plans, habits and decisions (spec 15, 31.6)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Sequence

from app import ids
from app.agency.models import ActionCandidate, Goal, Habit, Plan
from app.clock import from_iso, to_iso
from app.storage.database import Database

GOAL = "goal"
PLAN = "plan"
HABIT = "hab"
DECISION = "dec"


class GoalRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(
        self,
        *,
        description: str,
        source: str,
        reason: str,
        importance: float,
        autonomy: float,
        obligation: float,
        expected_reward: float,
        identity_relevance: float,
        value_alignment: float,
        origin: str,
        now: datetime,
    ) -> Goal:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT goal_id FROM goals WHERE description = ? AND source = ?",
                (description, source),
            ).fetchone()
            if row is None:
                goal_id = ids.new_id(GOAL)
                connection.execute(
                    """
                    INSERT INTO goals
                        (goal_id, description, source, reason, importance, autonomy,
                         obligation, expected_reward, identity_relevance, value_alignment,
                         created_at, updated_at, origin)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        goal_id, description, source, reason, importance, autonomy,
                        obligation, expected_reward, identity_relevance, value_alignment,
                        to_iso(now), to_iso(now), origin,
                    ),
                )
            else:
                goal_id = row["goal_id"]
                connection.execute(
                    "UPDATE goals SET importance = ?, expected_reward = ?, updated_at = ? "
                    "WHERE goal_id = ?",
                    (importance, expected_reward, to_iso(now), goal_id),
                )
            result = connection.execute(
                "SELECT * FROM goals WHERE goal_id = ?", (goal_id,)
            ).fetchone()
        return _to_goal(result)

    def update_progress(
        self, *, goal_id: str, progress: float, status: str, now: datetime
    ) -> Goal:
        self._db.execute(
            "UPDATE goals SET progress = ?, status = ?, updated_at = ?, last_pursued_at = ? "
            "WHERE goal_id = ?",
            (progress, status, to_iso(now), to_iso(now), goal_id),
        )
        return self.get(goal_id)  # type: ignore[return-value]

    def set_status(self, goal_id: str, status: str, *, now: datetime) -> None:
        self._db.execute(
            "UPDATE goals SET status = ?, updated_at = ? WHERE goal_id = ?",
            (status, to_iso(now), goal_id),
        )

    def get(self, goal_id: str) -> Goal | None:
        row = self._db.query_one("SELECT * FROM goals WHERE goal_id = ?", (goal_id,))
        return None if row is None else _to_goal(row)

    def find(self, description: str, source: str) -> Goal | None:
        row = self._db.query_one(
            "SELECT * FROM goals WHERE description = ? AND source = ?", (description, source)
        )
        return None if row is None else _to_goal(row)

    def active(self, *, limit: int = 20) -> list[Goal]:
        rows = self._db.query_all(
            "SELECT * FROM goals WHERE status = 'active' ORDER BY importance DESC LIMIT ?",
            (limit,),
        )
        return [_to_goal(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM goals") or 0)


class PlanRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        *,
        goal_id: str | None,
        description: str,
        planned_for: datetime | None,
        now: datetime,
    ) -> Plan:
        plan_id = ids.new_id(PLAN)
        self._db.execute(
            """
            INSERT INTO plans
                (plan_id, goal_id, description, status, planned_for, created_at, updated_at)
            VALUES (?, ?, ?, 'planned', ?, ?, ?)
            """,
            (
                plan_id, goal_id, description,
                None if planned_for is None else to_iso(planned_for),
                to_iso(now), to_iso(now),
            ),
        )
        return self.get(plan_id)  # type: ignore[return-value]

    def transition(
        self,
        *,
        plan_id: str,
        status: str,
        now: datetime,
        outcome: str | None = None,
    ) -> Plan:
        """Move a plan's status. Only ``completed`` records a real happening."""
        started = to_iso(now) if status == "in_progress" else None
        completed = to_iso(now) if status == "completed" else None
        self._db.execute(
            """
            UPDATE plans
               SET status = ?, updated_at = ?,
                   started_at = COALESCE(?, started_at),
                   completed_at = COALESCE(?, completed_at),
                   outcome = COALESCE(?, outcome)
             WHERE plan_id = ?
            """,
            (status, to_iso(now), started, completed, outcome, plan_id),
        )
        return self.get(plan_id)  # type: ignore[return-value]

    def get(self, plan_id: str) -> Plan | None:
        row = self._db.query_one("SELECT * FROM plans WHERE plan_id = ?", (plan_id,))
        return None if row is None else _to_plan(row)

    def with_status(self, status: str, *, limit: int = 50) -> list[Plan]:
        rows = self._db.query_all(
            "SELECT * FROM plans WHERE status = ? ORDER BY created_at LIMIT ?", (status, limit)
        )
        return [_to_plan(row) for row in rows]

    def completed(self, *, limit: int = 50) -> list[Plan]:
        """Only these describe something that actually happened (spec 2.15)."""
        return self.with_status("completed", limit=limit)

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM plans") or 0)


class HabitRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def ensure(self, *, name: str, cue: str, action: str, now: datetime) -> Habit:
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO habits "
                "(habit_id, name, cue, action, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ids.new_id(HABIT), name, cue, action, to_iso(now), to_iso(now)),
            )
            row = connection.execute(
                "SELECT * FROM habits WHERE name = ? AND cue = ?", (name, cue)
            ).fetchone()
        return _to_habit(row)

    def update(
        self,
        *,
        habit_id: str,
        automaticity: float,
        repetitions: int,
        cue_encounters: int,
        context_available: bool,
        last_performed_at: datetime | None,
        last_cue_at: datetime | None,
        status: str,
        now: datetime,
    ) -> Habit:
        self._db.execute(
            """
            UPDATE habits
               SET automaticity = ?, repetitions = ?, cue_encounters = ?,
                   context_available = ?, last_performed_at = ?, last_cue_at = ?,
                   status = ?, updated_at = ?
             WHERE habit_id = ?
            """,
            (
                automaticity, repetitions, cue_encounters, 1 if context_available else 0,
                None if last_performed_at is None else to_iso(last_performed_at),
                None if last_cue_at is None else to_iso(last_cue_at),
                status, to_iso(now), habit_id,
            ),
        )
        return self.get(habit_id)  # type: ignore[return-value]

    def get(self, habit_id: str) -> Habit | None:
        row = self._db.query_one("SELECT * FROM habits WHERE habit_id = ?", (habit_id,))
        return None if row is None else _to_habit(row)

    def find(self, name: str, cue: str) -> Habit | None:
        row = self._db.query_one(
            "SELECT * FROM habits WHERE name = ? AND cue = ?", (name, cue)
        )
        return None if row is None else _to_habit(row)

    def by_cue(self, cue: str) -> list[Habit]:
        rows = self._db.query_all(
            "SELECT * FROM habits WHERE cue = ? ORDER BY automaticity DESC", (cue,)
        )
        return [_to_habit(row) for row in rows]

    def established(self, threshold: float) -> list[Habit]:
        rows = self._db.query_all(
            "SELECT * FROM habits WHERE automaticity >= ? ORDER BY automaticity DESC",
            (threshold,),
        )
        return [_to_habit(row) for row in rows]

    def all(self, *, limit: int = 500) -> list[Habit]:
        rows = self._db.query_all("SELECT * FROM habits ORDER BY created_at LIMIT ?", (limit,))
        return [_to_habit(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM habits") or 0)


class DecisionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        decision_id: str,
        run_id: str | None,
        event_id: str | None,
        chosen: ActionCandidate,
        candidates: Sequence[ActionCandidate],
        now: datetime,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO decisions
                (decision_id, run_id, event_id, chosen_action, route, expected_value,
                 candidates_json, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id, run_id, event_id, chosen.action, chosen.route,
                chosen.expected_value,
                json.dumps(
                    [candidate.model_dump() for candidate in candidates],
                    ensure_ascii=False,
                ),
                to_iso(now),
            ),
        )

    def resolve(
        self, *, decision_id: str, outcome_value: float, prediction_error: float, now: datetime
    ) -> None:
        self._db.execute(
            "UPDATE decisions SET outcome_value = ?, prediction_error = ?, resolved_at = ? "
            "WHERE decision_id = ?",
            (outcome_value, prediction_error, to_iso(now), decision_id),
        )

    def get(self, decision_id: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        )

    def recent(self, *, limit: int = 50) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM decisions ORDER BY decided_at DESC LIMIT ?", (limit,)
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM decisions") or 0)


def _optional_dt(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


def _to_goal(row: sqlite3.Row) -> Goal:
    return Goal(
        goal_id=row["goal_id"],
        description=row["description"],
        source=row["source"],
        reason=row["reason"],
        importance=row["importance"],
        autonomy=row["autonomy"],
        obligation=row["obligation"],
        expected_reward=row["expected_reward"],
        identity_relevance=row["identity_relevance"],
        value_alignment=row["value_alignment"],
        progress=row["progress"],
        status=row["status"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        last_pursued_at=_optional_dt(row["last_pursued_at"]),
        origin=row["origin"],
    )


def _to_plan(row: sqlite3.Row) -> Plan:
    return Plan(
        plan_id=row["plan_id"],
        goal_id=row["goal_id"],
        description=row["description"],
        status=row["status"],
        planned_for=_optional_dt(row["planned_for"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        started_at=_optional_dt(row["started_at"]),
        completed_at=_optional_dt(row["completed_at"]),
        outcome=row["outcome"],
    )


def _to_habit(row: sqlite3.Row) -> Habit:
    return Habit(
        habit_id=row["habit_id"],
        name=row["name"],
        cue=row["cue"],
        action=row["action"],
        automaticity=row["automaticity"],
        repetitions=int(row["repetitions"]),
        cue_encounters=int(row["cue_encounters"]),
        context_available=bool(row["context_available"]),
        last_performed_at=_optional_dt(row["last_performed_at"]),
        last_cue_at=_optional_dt(row["last_cue_at"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        status=row["status"],
    )
