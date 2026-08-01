"""Failure records (spec 28, 31.9).

Rejections are first-class data. A proposal that violates policy is recorded
here and dropped — never clamped into an acceptable range.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app import ids
from app.clock import to_iso
from app.storage.database import Database

FailureType = Literal[
    "transport",
    "generation",
    "parse",
    "schema",
    "semantic",
    "consistency",
    "tool",
    "context",
    "behavior",
    "state",
    "infrastructure",
]
Severity = Literal["info", "warning", "error", "critical"]


@dataclass(frozen=True, slots=True)
class FailureRecord:
    failure_type: FailureType
    component: str
    reason_code: str
    severity: Severity = "warning"
    run_id: str | None = None
    event_id: str | None = None
    reference_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class FailureRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, failure: FailureRecord, *, now: datetime) -> str:
        failure_id = ids.new_id(ids.FAILURE)
        self._db.execute(
            """
            INSERT INTO failures
                (failure_id, occurred_at, failure_type, severity, component, reason_code,
                 run_id, event_id, reference_id, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                failure_id,
                to_iso(now),
                failure.failure_type,
                failure.severity,
                failure.component,
                failure.reason_code,
                failure.run_id,
                failure.event_id,
                failure.reference_id,
                json.dumps(failure.detail, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )
        return failure_id

    def for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM failures WHERE run_id = ? ORDER BY occurred_at", (run_id,)
        )

    def recent(self, *, limit: int = 50) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM failures ORDER BY occurred_at DESC, failure_id DESC LIMIT ?", (limit,)
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM failures") or 0)
