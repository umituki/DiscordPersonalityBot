"""Event persistence.

Append-only. There is no update or delete method, and the schema has triggers
that abort both (spec 8.3).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from app.clock import from_iso, to_iso
from app.events.model import Event, UnknownPayload, build_payload
from app.storage.database import Database

_COLUMNS = """
    event_id, event_type, category, schema_version, payload_schema_version,
    occurred_at, recorded_at, actor_type, actor_id, target_type, target_id,
    root_event_id, parent_event_id, source_type, source_id, objective,
    priority, origin, payload_json
"""


class EventRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- writes ------------------------------------------------------------
    def insert(self, event: Event) -> bool:
        """Append an event. Returns ``False`` if it was already stored."""
        payload = event.payload
        payload_data = (
            payload.raw if isinstance(payload, UnknownPayload) else payload.model_dump(mode="json")
        )
        cursor = self._db.execute(
            f"INSERT OR IGNORE INTO events ({_COLUMNS}) VALUES ("
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.event_type,
                event.category,
                event.schema_version,
                event.payload_schema_version,
                to_iso(event.occurred_at),
                to_iso(event.recorded_at),
                event.actor_type,
                event.actor_id,
                event.target_type,
                event.target_id,
                event.root_event_id,
                event.parent_event_id,
                event.source_type,
                event.source_id,
                1 if event.objective else 0,
                event.priority,
                event.origin,
                json.dumps(payload_data, ensure_ascii=False, sort_keys=True),
            ),
        )
        return cursor.rowcount == 1

    # --- reads -------------------------------------------------------------
    def get(self, event_id: str) -> Event | None:
        row = self._db.query_one(
            f"SELECT {_COLUMNS} FROM events WHERE event_id = ?", (event_id,)
        )
        return None if row is None else self._to_event(row)

    def exists(self, event_id: str) -> bool:
        return self._db.query_one("SELECT 1 FROM events WHERE event_id = ?", (event_id,)) is not None

    def list_by_root(self, root_event_id: str) -> list[Event]:
        rows = self._db.query_all(
            f"SELECT {_COLUMNS} FROM events WHERE root_event_id = ? "
            "ORDER BY occurred_at, event_id",
            (root_event_id,),
        )
        return [self._to_event(row) for row in rows]

    def list_recent(self, *, limit: int = 50, origin: str | None = None) -> list[Event]:
        if origin is None:
            rows = self._db.query_all(
                f"SELECT {_COLUMNS} FROM events ORDER BY recorded_at DESC, event_id DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self._db.query_all(
                f"SELECT {_COLUMNS} FROM events WHERE origin = ? "
                "ORDER BY recorded_at DESC, event_id DESC LIMIT ?",
                (origin, limit),
            )
        return [self._to_event(row) for row in rows]

    def list_recorded_since(self, moment: datetime, *, limit: int = 500) -> list[Event]:
        rows = self._db.query_all(
            f"SELECT {_COLUMNS} FROM events WHERE recorded_at >= ? "
            "ORDER BY recorded_at, event_id LIMIT ?",
            (to_iso(moment), limit),
        )
        return [self._to_event(row) for row in rows]

    def list_by_types(self, event_types: Sequence[str], *, limit: int = 5000) -> list[Event]:
        if not event_types:
            return []
        placeholders = ", ".join("?" for _ in event_types)
        rows = self._db.query_all(
            f"SELECT {_COLUMNS} FROM events WHERE event_type IN ({placeholders}) "
            "ORDER BY occurred_at, event_id LIMIT ?",
            (*event_types, limit),
        )
        return [self._to_event(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM events") or 0)

    # --- mapping -----------------------------------------------------------
    @staticmethod
    def _to_event(row: sqlite3.Row) -> Event:
        payload = build_payload(
            row["event_type"], int(row["payload_schema_version"]), json.loads(row["payload_json"])
        )
        return Event(
            event_id=row["event_id"],
            event_type=row["event_type"],
            category=row["category"],
            schema_version=int(row["schema_version"]),
            payload_schema_version=int(row["payload_schema_version"]),
            occurred_at=from_iso(row["occurred_at"]),
            recorded_at=from_iso(row["recorded_at"]),
            actor_type=row["actor_type"],
            actor_id=row["actor_id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            root_event_id=row["root_event_id"],
            parent_event_id=row["parent_event_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            objective=bool(row["objective"]),
            priority=row["priority"],
            origin=row["origin"],
            payload=payload,
        )
