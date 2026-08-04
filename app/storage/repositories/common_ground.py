"""Common Ground claims (rebuild spec 11.1)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.clock import from_iso, to_iso
from app.conversation.common_ground import CommonGroundClaim, LIVE_STATUSES
from app.ids import new_id
from app.storage.database import Database


class CommonGroundRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        conversation_id: str,
        event_id: str | None,
        kind: str,
        statement: str,
        status: str,
        source: str,
        confidence: str,
        now: datetime,
        evidence: tuple[str, ...] = (),
        semantic: dict | None = None,
        subject: str = "",
        modality: str = "",
    ) -> CommonGroundClaim:
        """Record one claim, with the semantic representation that was verified.

        Audit finding 3: ``semantic`` is the reviewed claim object from *before*
        the send. Storing it is what lets a later correction reason about the
        same claim rather than re-deriving a different one from the sentence.
        """
        claim_id = new_id("cgc")
        self._db.execute(
            "INSERT INTO common_ground_claims ("
            "claim_id, conversation_id, event_id, kind, statement, status, source, "
            "confidence, evidence_json, semantic_json, subject, modality, "
            "asserted_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim_id,
                conversation_id,
                event_id,
                kind,
                statement,
                status,
                source,
                confidence,
                json.dumps(list(evidence), ensure_ascii=False),
                # Empty string, not "{}", when there is no verified claim: an
                # empty JSON object parses into a default claim, and a default
                # claim asserts something with no evidence. A legacy row must
                # read as "no stored representation", not as "an empty one".
                (
                    json.dumps(semantic, ensure_ascii=False, sort_keys=True)
                    if semantic
                    else ""
                ),
                subject,
                modality,
                to_iso(now),
                to_iso(now),
            ),
        )
        return self.get(claim_id)  # type: ignore[return-value]

    def set_status(
        self, claim_id: str, *, status: str, reason: str, now: datetime
    ) -> CommonGroundClaim:
        self._db.execute(
            "UPDATE common_ground_claims SET status = ?, resolved_reason = ?, "
            "updated_at = ? WHERE claim_id = ?",
            (status, reason, to_iso(now), claim_id),
        )
        return self.get(claim_id)  # type: ignore[return-value]

    def get(self, claim_id: str) -> CommonGroundClaim | None:
        row = self._db.query_one(
            "SELECT * FROM common_ground_claims WHERE claim_id = ?", (claim_id,)
        )
        return None if row is None else _to_claim(row)

    def live(self, conversation_id: str, *, limit: int = 10) -> list[CommonGroundClaim]:
        """Newest first. A retracted claim is not returned, ever (CORR-003)."""
        placeholders = ", ".join("?" for _ in LIVE_STATUSES)
        rows = self._db.query_all(
            "SELECT * FROM common_ground_claims WHERE conversation_id = ? "
            f"AND status IN ({placeholders}) ORDER BY asserted_at DESC, rowid DESC LIMIT ?",
            (conversation_id, *LIVE_STATUSES, limit),
        )
        return [_to_claim(row) for row in rows]

    def all_for(
        self, conversation_id: str, *, limit: int = 100
    ) -> list[CommonGroundClaim]:
        """Every claim including retracted ones — for the debug path, not the
        conversation. What she took back is history, not common ground."""
        rows = self._db.query_all(
            "SELECT * FROM common_ground_claims WHERE conversation_id = ? "
            "ORDER BY asserted_at DESC, rowid DESC LIMIT ?",
            (conversation_id, limit),
        )
        return [_to_claim(row) for row in rows]

    def count(self, *, status: str | None = None) -> int:
        if status is None:
            return int(self._db.scalar("SELECT COUNT(*) FROM common_ground_claims") or 0)
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM common_ground_claims WHERE status = ?", (status,)
            )
            or 0
        )


def _to_claim(row: sqlite3.Row) -> CommonGroundClaim:
    return CommonGroundClaim(
        claim_id=row["claim_id"],
        conversation_id=row["conversation_id"],
        event_id=row["event_id"],
        kind=row["kind"],
        statement=row["statement"],
        status=row["status"],
        source=row["source"],
        confidence=row["confidence"],
        evidence=tuple(json.loads(row["evidence_json"] or "[]")),
        asserted_at=from_iso(row["asserted_at"]),
        updated_at=from_iso(row["updated_at"]),
        resolved_reason=row["resolved_reason"],
        semantic_json=_column(row, "semantic_json"),
        subject=_column(row, "subject"),
        modality=_column(row, "modality"),
    )


def _column(row: sqlite3.Row, name: str) -> str:
    """Read a column that older rows may not have."""
    try:
        return row[name] or ""
    except (IndexError, KeyError):
        return ""


__all__ = ["CommonGroundRepository"]
