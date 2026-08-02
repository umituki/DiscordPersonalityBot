"""Conversation projection (spec 31.4).

This is a read model. The authoritative record of what was said is the event
store; these rows exist so recent history can be read without replaying events,
and they can be rebuilt from events at any time.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app import ids
from app.clock import from_iso, to_iso
from app.conversation.models import Conversation, ConversationTurn
from app.storage.database import Database

class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- conversations -----------------------------------------------------
    def ensure_conversation(
        self, *, channel_id: str, channel_type: str, now: datetime
    ) -> Conversation:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if row is None:
                conversation_id = ids.new_id(ids.CONVERSATION)
                connection.execute(
                    """
                    INSERT INTO conversations
                        (conversation_id, channel_id, channel_type, started_at,
                         last_activity_at, turn_count, status)
                    VALUES (?, ?, ?, ?, ?, 0, 'active')
                    """,
                    (conversation_id, channel_id, channel_type, to_iso(now), to_iso(now)),
                )
                row = connection.execute(
                    "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)
                ).fetchone()
        return _to_conversation(row)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = self._db.query_one(
            "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)
        )
        return None if row is None else _to_conversation(row)

    def by_channel(self, channel_id: str) -> Conversation | None:
        row = self._db.query_one(
            "SELECT * FROM conversations WHERE channel_id = ?", (channel_id,)
        )
        return None if row is None else _to_conversation(row)

    # --- turns -------------------------------------------------------------
    def record_turn(
        self,
        *,
        conversation_id: str,
        event_id: str,
        speaker: str,
        author_id: str | None,
        content: str,
        occurred_at: datetime,
        message_ref: str | None = None,
    ) -> bool:
        """Project one utterance. Re-projecting the same event is a no-op."""
        with self._db.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO conversation_turns
                    (turn_id, conversation_id, event_id, speaker, author_id, content,
                     occurred_at, message_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ids.new_id(ids.TURN),
                    conversation_id,
                    event_id,
                    speaker,
                    author_id,
                    content,
                    to_iso(occurred_at),
                    message_ref,
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "UPDATE conversations SET turn_count = turn_count + 1, last_activity_at = ? "
                "WHERE conversation_id = ?",
                (to_iso(occurred_at), conversation_id),
            )
        return True

    def recent_turns(
        self, conversation_id: str, *, limit: int = 12, exclude_event_id: str | None = None
    ) -> list[ConversationTurn]:
        """Most recent turns in chronological order."""
        if exclude_event_id is None:
            rows = self._db.query_all(
                "SELECT * FROM conversation_turns WHERE conversation_id = ? "
                "ORDER BY occurred_at DESC, turn_id DESC LIMIT ?",
                (conversation_id, limit),
            )
        else:
            rows = self._db.query_all(
                "SELECT * FROM conversation_turns WHERE conversation_id = ? AND event_id != ? "
                "ORDER BY occurred_at DESC, turn_id DESC LIMIT ?",
                (conversation_id, exclude_event_id, limit),
            )
        return [_to_turn(row) for row in reversed(rows)]

    def turn_for_event(self, event_id: str) -> ConversationTurn | None:
        row = self._db.query_one(
            "SELECT * FROM conversation_turns WHERE event_id = ?", (event_id,)
        )
        return None if row is None else _to_turn(row)

    def turn_count(self, conversation_id: str) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM conversation_turns WHERE conversation_id = ?",
                (conversation_id,),
            )
            or 0
        )

    def clear_projection(self) -> None:
        """Drop the projection so it can be rebuilt from events."""
        with self._db.transaction() as connection:
            connection.execute("DELETE FROM conversation_turns")
            connection.execute("UPDATE conversations SET turn_count = 0")


def _to_conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        conversation_id=row["conversation_id"],
        channel_id=row["channel_id"],
        channel_type=row["channel_type"],
        started_at=from_iso(row["started_at"]),
        last_activity_at=from_iso(row["last_activity_at"]),
        turn_count=int(row["turn_count"]),
        status=row["status"],
    )


def _to_turn(row: sqlite3.Row) -> ConversationTurn:
    return ConversationTurn(
        turn_id=row["turn_id"],
        conversation_id=row["conversation_id"],
        event_id=row["event_id"],
        speaker=row["speaker"],
        author_id=row["author_id"],
        content=row["content"],
        occurred_at=from_iso(row["occurred_at"]),
        message_ref=row["message_ref"],
    )
