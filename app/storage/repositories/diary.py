"""Diary persistence (rebuild spec 26, 36 — Phase 10)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app import ids
from app.clock import from_iso, to_iso
from app.diary.models import DiaryEntry, DiaryReference, LifeDay
from app.storage.database import Database

LIFE_DAY = "day"
DIARY = "dia"


class LifeDayRepository:
    """The unit she actually lives in (26.2): waking to sleeping."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def open_day(
        self, *, now: datetime, wake_sleep_id: str | None = None
    ) -> LifeDay:
        """Begin a day, or return the one already open.

        Idempotent on purpose. Waking is the trigger, and a wake that is
        recorded twice — a retry, a restart mid-transition — must not produce
        two days and therefore two diaries for one evening.
        """
        current = self.current()
        if current is not None:
            return current
        ordinal = self.count() + 1
        life_day_id = ids.new_id(LIFE_DAY)
        self._db.execute(
            "INSERT INTO life_days (life_day_id, started_at, wake_sleep_id, ordinal) "
            "VALUES (?, ?, ?, ?)",
            (life_day_id, to_iso(now), wake_sleep_id, ordinal),
        )
        return self.get(life_day_id)  # type: ignore[return-value]

    def close_day(
        self, life_day_id: str, *, now: datetime, sleep_sleep_id: str | None = None
    ) -> LifeDay | None:
        self._db.execute(
            "UPDATE life_days SET ended_at = ?, sleep_sleep_id = ? "
            "WHERE life_day_id = ? AND ended_at IS NULL",
            (to_iso(now), sleep_sleep_id, life_day_id),
        )
        return self.get(life_day_id)

    def current(self) -> LifeDay | None:
        row = self._db.query_one(
            "SELECT * FROM life_days WHERE ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"
        )
        return None if row is None else _to_day(row)

    def get(self, life_day_id: str) -> LifeDay | None:
        row = self._db.query_one(
            "SELECT * FROM life_days WHERE life_day_id = ?", (life_day_id,)
        )
        return None if row is None else _to_day(row)

    def recent(self, *, limit: int = 20) -> list[LifeDay]:
        rows = self._db.query_all(
            "SELECT * FROM life_days ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [_to_day(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM life_days") or 0)


class DiaryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def intend(
        self,
        *,
        life_day_id: str,
        intended_at: datetime,
        sleep_episode_id: str | None = None,
    ) -> DiaryEntry:
        """Record that tonight's entry is owed. 26.3's ``pending``.

        Written before the model is asked, so that a generation which never
        returns still leaves something for the retry to find.
        """
        existing = self.for_day(life_day_id)
        if existing is not None:
            return existing
        diary_id = ids.new_id(DIARY)
        self._db.execute(
            "INSERT INTO diary_entries (diary_id, life_day_id, intended_at, "
            "sleep_episode_id, status) VALUES (?, ?, ?, ?, 'pending')",
            (diary_id, life_day_id, to_iso(intended_at), sleep_episode_id),
        )
        return self.get(diary_id)  # type: ignore[return-value]

    def write(
        self,
        diary_id: str,
        *,
        content: str,
        summary: str,
        importance: float,
        mood_valence: float,
        mood_arousal: float,
        prompt_version: str,
        model_version: str,
        now: datetime,
        late: bool,
    ) -> DiaryEntry:
        self._db.execute(
            """
            UPDATE diary_entries
               SET content = ?, summary = ?, importance = ?, mood_valence = ?,
                   mood_arousal = ?, prompt_version = ?, model_version = ?,
                   generated_at = ?, status = ?, attempts = attempts + 1
             WHERE diary_id = ?
            """,
            (
                content, summary, importance, mood_valence, mood_arousal,
                prompt_version, model_version, to_iso(now),
                "late_written" if late else "written", diary_id,
            ),
        )
        return self.get(diary_id)  # type: ignore[return-value]

    def record_attempt(self, diary_id: str, *, failed: bool = False) -> None:
        """A try that produced nothing. 26.3: sleep proceeds regardless."""
        self._db.execute(
            "UPDATE diary_entries SET attempts = attempts + 1, status = ? "
            "WHERE diary_id = ? AND status = 'pending'",
            ("failed" if failed else "pending", diary_id),
        )

    def add_references(
        self, diary_id: str, references: tuple[DiaryReference, ...]
    ) -> None:
        for reference in references:
            self._db.execute(
                "INSERT OR REPLACE INTO diary_references "
                "(diary_id, reference_type, reference_id, mentioned_in_text) "
                "VALUES (?, ?, ?, ?)",
                (
                    diary_id,
                    reference.reference_type,
                    reference.reference_id,
                    1 if reference.mentioned_in_text else 0,
                ),
            )

    def get(self, diary_id: str) -> DiaryEntry | None:
        row = self._db.query_one(
            "SELECT * FROM diary_entries WHERE diary_id = ?", (diary_id,)
        )
        return None if row is None else self._to_entry(row)

    def for_day(self, life_day_id: str) -> DiaryEntry | None:
        row = self._db.query_one(
            "SELECT * FROM diary_entries WHERE life_day_id = ?", (life_day_id,)
        )
        return None if row is None else self._to_entry(row)

    def pending(self, *, limit: int = 10) -> list[DiaryEntry]:
        """Entries that are owed. 26.3's retry queue."""
        rows = self._db.query_all(
            "SELECT * FROM diary_entries WHERE status = 'pending' "
            "ORDER BY intended_at LIMIT ?",
            (limit,),
        )
        return [self._to_entry(row) for row in rows]

    def recent(self, *, limit: int = 20) -> list[DiaryEntry]:
        rows = self._db.query_all(
            "SELECT * FROM diary_entries WHERE status IN ('written', 'late_written') "
            "ORDER BY intended_at DESC LIMIT ?",
            (limit,),
        )
        return [self._to_entry(row) for row in rows]

    def count(self, *, status: str | None = None) -> int:
        if status is None:
            return int(self._db.scalar("SELECT COUNT(*) FROM diary_entries") or 0)
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM diary_entries WHERE status = ?", (status,)
            )
            or 0
        )

    def references(self, diary_id: str) -> list[DiaryReference]:
        rows = self._db.query_all(
            "SELECT * FROM diary_references WHERE diary_id = ?", (diary_id,)
        )
        return [
            DiaryReference(
                reference_type=row["reference_type"],
                reference_id=row["reference_id"],
                mentioned_in_text=bool(row["mentioned_in_text"]),
            )
            for row in rows
        ]

    def _to_entry(self, row: sqlite3.Row) -> DiaryEntry:
        return DiaryEntry(
            diary_id=row["diary_id"],
            life_day_id=row["life_day_id"],
            intended_at=from_iso(row["intended_at"]),
            generated_at=(
                None if row["generated_at"] is None else from_iso(row["generated_at"])
            ),
            sleep_episode_id=row["sleep_episode_id"],
            content=row["content"],
            summary=row["summary"],
            importance=row["importance"],
            mood_valence=row["mood_valence"],
            mood_arousal=row["mood_arousal"],
            prompt_version=row["prompt_version"],
            model_version=row["model_version"],
            attempts=row["attempts"],
            status=row["status"],
            references=tuple(self.references(row["diary_id"])),
        )


def _to_day(row: sqlite3.Row) -> LifeDay:
    return LifeDay(
        life_day_id=row["life_day_id"],
        started_at=from_iso(row["started_at"]),
        ended_at=None if row["ended_at"] is None else from_iso(row["ended_at"]),
        wake_sleep_id=row["wake_sleep_id"],
        sleep_sleep_id=row["sleep_sleep_id"],
        ordinal=row["ordinal"],
    )


__all__ = ["DiaryRepository", "LifeDayRepository"]
