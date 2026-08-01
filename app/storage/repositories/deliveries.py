"""Event delivery bookkeeping.

Spec storage rule: ``Delivery processing must be idempotent``. The unique
``(event_id, subscriber)`` row is the authority on whether a subscriber has
already consumed an event, so redelivery after a crash cannot double-apply
psychology.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app import ids
from app.clock import to_iso
from app.storage.database import Database

DeliveryState = Literal["pending", "completed", "failed_retryable", "failed_permanent", "skipped"]

#: Attempts beyond this move the delivery to a permanent failure (spec 28.4).
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    """Result of asking whether a subscriber should process an event."""

    delivery_id: str
    should_process: bool
    status: DeliveryState
    attempts: int


class DeliveryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def begin_attempt(
        self,
        event_id: str,
        subscriber: str,
        *,
        now: datetime,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> DeliveryDecision:
        """Reserve an attempt, or report that the event was already handled."""
        timestamp = to_iso(now)
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT delivery_id, status, attempts FROM event_deliveries "
                "WHERE event_id = ? AND subscriber = ?",
                (event_id, subscriber),
            ).fetchone()

            if row is None:
                delivery_id = ids.new_id(ids.DELIVERY)
                connection.execute(
                    """
                    INSERT INTO event_deliveries
                        (delivery_id, event_id, subscriber, status, attempts,
                         first_attempt_at, last_attempt_at)
                    VALUES (?, ?, ?, 'pending', 1, ?, ?)
                    """,
                    (delivery_id, event_id, subscriber, timestamp, timestamp),
                )
                return DeliveryDecision(delivery_id, True, "pending", 1)

            status: DeliveryState = row["status"]
            attempts = int(row["attempts"])
            if status in ("completed", "skipped", "failed_permanent"):
                return DeliveryDecision(row["delivery_id"], False, status, attempts)

            attempts += 1
            if attempts > max_attempts:
                connection.execute(
                    "UPDATE event_deliveries SET status = 'failed_permanent', attempts = ?, "
                    "last_attempt_at = ? WHERE delivery_id = ?",
                    (attempts, timestamp, row["delivery_id"]),
                )
                return DeliveryDecision(row["delivery_id"], False, "failed_permanent", attempts)

            connection.execute(
                "UPDATE event_deliveries SET status = 'pending', attempts = ?, "
                "last_attempt_at = ? WHERE delivery_id = ?",
                (attempts, timestamp, row["delivery_id"]),
            )
            return DeliveryDecision(row["delivery_id"], True, "pending", attempts)

    def mark_completed(self, delivery_id: str, *, now: datetime) -> None:
        self._db.execute(
            "UPDATE event_deliveries SET status = 'completed', completed_at = ?, error = NULL "
            "WHERE delivery_id = ?",
            (to_iso(now), delivery_id),
        )

    def mark_skipped(self, delivery_id: str, *, now: datetime, reason: str) -> None:
        self._db.execute(
            "UPDATE event_deliveries SET status = 'skipped', completed_at = ?, error = ? "
            "WHERE delivery_id = ?",
            (to_iso(now), json.dumps({"reason": reason}), delivery_id),
        )

    def mark_failed(
        self,
        delivery_id: str,
        *,
        now: datetime,
        error: str,
        permanent: bool = False,
    ) -> None:
        self._db.execute(
            "UPDATE event_deliveries SET status = ?, last_attempt_at = ?, error = ? "
            "WHERE delivery_id = ?",
            (
                "failed_permanent" if permanent else "failed_retryable",
                to_iso(now),
                error[:2000],
                delivery_id,
            ),
        )

    def status_of(self, event_id: str, subscriber: str) -> DeliveryState | None:
        row = self._db.query_one(
            "SELECT status FROM event_deliveries WHERE event_id = ? AND subscriber = ?",
            (event_id, subscriber),
        )
        return None if row is None else row["status"]

    def attempts_of(self, event_id: str, subscriber: str) -> int:
        row = self._db.query_one(
            "SELECT attempts FROM event_deliveries WHERE event_id = ? AND subscriber = ?",
            (event_id, subscriber),
        )
        return 0 if row is None else int(row["attempts"])

    def incomplete(self, *, limit: int = 200) -> list[tuple[str, str, DeliveryState]]:
        """Deliveries that a restart must reconsider (spec 28.4, 32 recovery)."""
        rows = self._db.query_all(
            "SELECT event_id, subscriber, status FROM event_deliveries "
            "WHERE status IN ('pending', 'failed_retryable') "
            "ORDER BY first_attempt_at LIMIT ?",
            (limit,),
        )
        return [(row["event_id"], row["subscriber"], row["status"]) for row in rows]
