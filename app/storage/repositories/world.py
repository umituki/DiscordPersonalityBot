"""Virtual life persistence (spec 18, 19, 31.6)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app import ids
from app.clock import from_iso, to_iso
from app.storage.database import Database
from app.world.models import Activity, ScheduledJob, SleepEpisode

ACTIVITY = "act"
WORLD_ENTRY = "wsh"
SLEEP = "slp"
JOB = "job"
CONTACT = "pct"


class ActivityRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(
        self,
        *,
        name: str,
        kind: str,
        location: str | None,
        plan_id: str | None,
        now: datetime,
        origin: str = "virtual_life",
    ) -> Activity:
        activity_id = ids.new_id(ACTIVITY)
        self._db.execute(
            """
            INSERT INTO activities
                (activity_id, name, kind, location, plan_id, started_at, status, origin)
            VALUES (?, ?, ?, ?, ?, ?, 'ongoing', ?)
            """,
            (activity_id, name, kind, location, plan_id, to_iso(now), origin),
        )
        return self.get(activity_id)  # type: ignore[return-value]

    def finish(self, activity_id: str, *, now: datetime, outcome: str | None = None) -> Activity:
        """An ongoing fact becomes an occurred fact only here (spec 18.2)."""
        self._db.execute(
            "UPDATE activities SET status = 'completed', ended_at = ?, outcome = ? "
            "WHERE activity_id = ? AND status = 'ongoing'",
            (to_iso(now), outcome, activity_id),
        )
        return self.get(activity_id)  # type: ignore[return-value]

    def abandon(self, activity_id: str, *, now: datetime, reason: str = "") -> Activity:
        self._db.execute(
            "UPDATE activities SET status = 'abandoned', ended_at = ?, outcome = ? "
            "WHERE activity_id = ? AND status = 'ongoing'",
            (to_iso(now), reason or None, activity_id),
        )
        return self.get(activity_id)  # type: ignore[return-value]

    def ongoing(self) -> Activity | None:
        row = self._db.query_one(
            "SELECT * FROM activities WHERE status = 'ongoing' ORDER BY started_at DESC LIMIT 1"
        )
        return None if row is None else _to_activity(row)

    def completed(self, *, limit: int = 50) -> list[Activity]:
        rows = self._db.query_all(
            "SELECT * FROM activities WHERE status = 'completed' "
            "ORDER BY ended_at DESC LIMIT ?",
            (limit,),
        )
        return [_to_activity(row) for row in rows]

    def get(self, activity_id: str) -> Activity | None:
        row = self._db.query_one(
            "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
        )
        return None if row is None else _to_activity(row)

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM activities") or 0)


class WorldHistoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        transition: str,
        awake: bool,
        location: str | None,
        activity: str | None,
        detail: dict[str, Any],
        now: datetime,
    ) -> str:
        entry_id = ids.new_id(WORLD_ENTRY)
        self._db.execute(
            """
            INSERT INTO world_state_history
                (entry_id, recorded_at, transition, awake, location, activity, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id, to_iso(now), transition, 1 if awake else 0, location, activity,
                json.dumps(detail, ensure_ascii=False, default=str),
            ),
        )
        return entry_id

    def recent(self, *, limit: int = 50) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM world_state_history ORDER BY recorded_at DESC LIMIT ?", (limit,)
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM world_state_history") or 0)


class SleepRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def begin(
        self,
        *,
        now: datetime,
        sleep_pressure: float,
        circadian: float,
        planned_wake_at: datetime | None,
        reason: str,
    ) -> SleepEpisode:
        sleep_id = ids.new_id(SLEEP)
        self._db.execute(
            """
            INSERT INTO sleep_episodes
                (sleep_id, started_at, planned_wake_at, sleep_pressure_at_onset,
                 circadian_at_onset, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sleep_id, to_iso(now),
                None if planned_wake_at is None else to_iso(planned_wake_at),
                sleep_pressure, circadian, reason,
            ),
        )
        return self.get(sleep_id)  # type: ignore[return-value]

    def end(
        self, sleep_id: str, *, now: datetime, quality: float, interrupted: bool
    ) -> SleepEpisode:
        self._db.execute(
            "UPDATE sleep_episodes SET ended_at = ?, quality = ?, interrupted = ? "
            "WHERE sleep_id = ?",
            (to_iso(now), quality, 1 if interrupted else 0, sleep_id),
        )
        return self.get(sleep_id)  # type: ignore[return-value]

    def current(self) -> SleepEpisode | None:
        row = self._db.query_one(
            "SELECT * FROM sleep_episodes WHERE ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"
        )
        return None if row is None else _to_sleep(row)

    def get(self, sleep_id: str) -> SleepEpisode | None:
        row = self._db.query_one(
            "SELECT * FROM sleep_episodes WHERE sleep_id = ?", (sleep_id,)
        )
        return None if row is None else _to_sleep(row)

    def recent(self, *, limit: int = 20) -> list[SleepEpisode]:
        rows = self._db.query_all(
            "SELECT * FROM sleep_episodes ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [_to_sleep(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM sleep_episodes") or 0)


class JobRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def schedule(
        self,
        *,
        job_type: str,
        job_class: str,
        due_at: datetime | None,
        window_end: datetime | None,
        payload: dict[str, Any],
        priority: str,
        misfire_policy: str,
        expires_at: datetime | None,
        now: datetime,
    ) -> ScheduledJob:
        job_id = ids.new_id(JOB)
        self._db.execute(
            """
            INSERT INTO scheduled_jobs
                (job_id, job_type, job_class, due_at, window_end, payload_json, priority,
                 misfire_policy, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, job_type, job_class,
                None if due_at is None else to_iso(due_at),
                None if window_end is None else to_iso(window_end),
                json.dumps(payload, ensure_ascii=False, default=str),
                priority, misfire_policy,
                None if expires_at is None else to_iso(expires_at),
                to_iso(now), to_iso(now),
            ),
        )
        return self.get(job_id)  # type: ignore[return-value]

    def due(self, *, now: datetime, limit: int = 20) -> list[ScheduledJob]:
        rows = self._db.query_all(
            "SELECT * FROM scheduled_jobs WHERE status = 'pending' AND due_at IS NOT NULL "
            "AND due_at <= ? ORDER BY due_at LIMIT ?",
            (to_iso(now), limit),
        )
        return [_to_job(row) for row in rows]

    def mark(self, job_id: str, status: str, *, now: datetime) -> ScheduledJob:
        self._db.execute(
            "UPDATE scheduled_jobs SET status = ?, updated_at = ?, last_fired_at = ?, "
            "attempts = attempts + 1 WHERE job_id = ?",
            (status, to_iso(now), to_iso(now), job_id),
        )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> ScheduledJob | None:
        row = self._db.query_one("SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,))
        return None if row is None else _to_job(row)

    def pending(self, *, limit: int = 100) -> list[ScheduledJob]:
        rows = self._db.query_all(
            "SELECT * FROM scheduled_jobs WHERE status = 'pending' ORDER BY due_at LIMIT ?",
            (limit,),
        )
        return [_to_job(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM scheduled_jobs") or 0)


class ProactiveRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record_contact(
        self, *, opportunity: str, now: datetime, event_id: str | None = None
    ) -> str:
        contact_id = ids.new_id(CONTACT)
        self._db.execute(
            "INSERT INTO proactive_contacts (contact_id, opportunity, sent_at, event_id) "
            "VALUES (?, ?, ?, ?)",
            (contact_id, opportunity, to_iso(now), event_id),
        )
        return contact_id

    def mark_answered(self, *, now: datetime) -> int:
        """The USER replied: every outstanding contact counts as answered."""
        cursor = self._db.execute(
            "UPDATE proactive_contacts SET answered_at = ? WHERE answered_at IS NULL",
            (to_iso(now),),
        )
        return cursor.rowcount

    def unanswered_count(self) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM proactive_contacts WHERE answered_at IS NULL"
            )
            or 0
        )

    def last_contact_at(self) -> datetime | None:
        value = self._db.scalar("SELECT MAX(sent_at) FROM proactive_contacts")
        return None if value is None else from_iso(value)

    def recent(self, *, limit: int = 20) -> list[sqlite3.Row]:
        """The contact history, newest first. A read, for inspection only."""
        return self._db.query_all(
            "SELECT contact_id, opportunity, sent_at, answered_at, event_id "
            "FROM proactive_contacts ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM proactive_contacts") or 0)


def _optional_dt(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


def _to_activity(row: sqlite3.Row) -> Activity:
    return Activity(
        activity_id=row["activity_id"],
        name=row["name"],
        kind=row["kind"],
        location=row["location"],
        plan_id=row["plan_id"],
        started_at=from_iso(row["started_at"]),
        ended_at=_optional_dt(row["ended_at"]),
        status=row["status"],
        outcome=row["outcome"],
        origin=row["origin"],
    )


def _to_sleep(row: sqlite3.Row) -> SleepEpisode:
    return SleepEpisode(
        sleep_id=row["sleep_id"],
        started_at=from_iso(row["started_at"]),
        ended_at=_optional_dt(row["ended_at"]),
        planned_wake_at=_optional_dt(row["planned_wake_at"]),
        sleep_pressure_at_onset=row["sleep_pressure_at_onset"],
        circadian_at_onset=row["circadian_at_onset"],
        quality=row["quality"],
        interrupted=bool(row["interrupted"]),
        reason=row["reason"],
    )


def _to_job(row: sqlite3.Row) -> ScheduledJob:
    return ScheduledJob(
        job_id=row["job_id"],
        job_type=row["job_type"],
        job_class=row["job_class"],
        due_at=_optional_dt(row["due_at"]),
        window_end=_optional_dt(row["window_end"]),
        payload=json.loads(row["payload_json"]),
        priority=row["priority"],
        status=row["status"],
        misfire_policy=row["misfire_policy"],
        expires_at=_optional_dt(row["expires_at"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        last_fired_at=_optional_dt(row["last_fired_at"]),
        attempts=int(row["attempts"]),
    )
