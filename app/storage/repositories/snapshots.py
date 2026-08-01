"""State snapshot persistence (spec 9.2)."""

from __future__ import annotations

import json
from datetime import datetime

from app.clock import to_iso
from app.storage.database import Database


class SnapshotRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def save(
        self,
        *,
        snapshot_id: str,
        run_id: str | None,
        root_event_id: str | None,
        created_at: datetime,
        payload: dict[str, dict[str, object]],
    ) -> None:
        key_count = sum(len(keys) for keys in payload.values())
        self._db.execute(
            """
            INSERT INTO state_snapshots
                (snapshot_id, run_id, root_event_id, created_at, domain_count, key_count,
                 payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                run_id,
                root_event_id,
                to_iso(created_at),
                len(payload),
                key_count,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )

    def load_payload(self, snapshot_id: str) -> dict[str, dict[str, object]] | None:
        row = self._db.query_one(
            "SELECT payload_json FROM state_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        )
        return None if row is None else json.loads(row["payload_json"])

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM state_snapshots") or 0)
