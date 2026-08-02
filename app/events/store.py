"""Append-only event store (spec 8.3).

The store has no update or delete operation, by design. A mistaken event is
retracted by appending ``EVENT_INVALIDATED``; a changed understanding is
recorded by appending ``REINTERPRETATION_CREATED``. The original event stays.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from app.clock import Clock, SystemClock
from app.events.model import (
    Event,
    EventInvalidatedPayload,
    ReinterpretationCreatedPayload,
)
from app.storage.database import Database
from app.storage.repositories.events import EventRepository

logger = logging.getLogger(__name__)


class ImmutableEventError(RuntimeError):
    """Raised on any attempt to mutate stored history."""


class EventStore:
    def __init__(
        self,
        db: Database,
        repository: EventRepository | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._events = repository or EventRepository(db)
        self._clock = clock or SystemClock()

    # --- append ------------------------------------------------------------
    def append(self, event: Event) -> bool:
        """Append one event. Re-appending the same id is a no-op (idempotent)."""
        inserted = self._events.insert(event)
        if not inserted:
            logger.debug("event already stored event_id=%s", event.event_id)
        return inserted

    def append_all(self, events: Iterable[Event]) -> tuple[int, int]:
        """Append a batch atomically. Returns ``(inserted, duplicates)``."""
        inserted = 0
        duplicates = 0
        with self._db.transaction():
            for event in events:
                if self._events.insert(event):
                    inserted += 1
                else:
                    duplicates += 1
        return inserted, duplicates

    # --- corrections (spec 8.3) --------------------------------------------
    def invalidate(
        self,
        event: Event,
        *,
        reason_code: str,
        detail: str | None = None,
        actor_type: str = "system",
    ) -> Event:
        """Append an invalidation. The invalidated event is never deleted."""
        correction = event.child(
            event_type="EVENT_INVALIDATED",
            category="system",
            actor_type=actor_type,  # type: ignore[arg-type]
            source_type="event_store",
            payload=EventInvalidatedPayload(
                invalidated_event_id=event.event_id, reason_code=reason_code, detail=detail
            ),
            clock=self._clock,
            origin="system",
            priority="P2",
        )
        self.append(correction)
        return correction

    def reinterpret(
        self,
        event: Event,
        *,
        reason_code: str,
        detail: str | None = None,
    ) -> Event:
        """Append a later reading of an event without touching the original."""
        correction = event.child(
            event_type="REINTERPRETATION_CREATED",
            category="internal",
            actor_type="yui",
            source_type="event_store",
            payload=ReinterpretationCreatedPayload(
                reinterpreted_event_id=event.event_id, reason_code=reason_code, detail=detail
            ),
            clock=self._clock,
            priority="P5",
        )
        self.append(correction)
        return correction

    # --- reads -------------------------------------------------------------
    def get(self, event_id: str) -> Event | None:
        return self._events.get(event_id)

    def exists(self, event_id: str) -> bool:
        return self._events.exists(event_id)

    def chain(self, root_event_id: str) -> list[Event]:
        return self._events.list_by_root(root_event_id)

    def recent(self, *, limit: int = 50, origin: str | None = None) -> list[Event]:
        return self._events.list_recent(limit=limit, origin=origin)

    def recorded_since(self, moment: datetime, *, limit: int = 500) -> list[Event]:
        return self._events.list_recorded_since(moment, limit=limit)

    def by_types(self, event_types: Iterable[str], *, limit: int = 5000) -> list[Event]:
        """Events of the given types, oldest first."""
        return self._events.list_by_types(tuple(event_types), limit=limit)

    def count(self) -> int:
        return self._events.count()

    def invalidated_ids(self, root_event_id: str) -> set[str]:
        """Ids retracted within a chain, so readers can skip them."""
        return {
            event.payload.invalidated_event_id
            for event in self.chain(root_event_id)
            if isinstance(event.payload, EventInvalidatedPayload)
        }
