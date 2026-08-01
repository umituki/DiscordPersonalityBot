"""Runtime manifest persistence (spec 29)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.clock import to_iso
from app.storage.database import Database


class ManifestRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def find_by_fingerprint(self, fingerprint: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM runtime_manifests WHERE fingerprint = ?", (fingerprint,)
        )

    def insert(
        self,
        *,
        manifest_id: str,
        fingerprint: str,
        created_at: datetime,
        code_commit_hash: str | None,
        config_version: int,
        event_schema_version: int,
        components: dict[str, str],
    ) -> None:
        self._db.execute(
            """
            INSERT INTO runtime_manifests
                (manifest_id, fingerprint, created_at, code_commit_hash, config_version,
                 event_schema_version, components_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest_id,
                fingerprint,
                to_iso(created_at),
                code_commit_hash,
                config_version,
                event_schema_version,
                json.dumps(components, ensure_ascii=False, sort_keys=True),
            ),
        )

    def get(self, manifest_id: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM runtime_manifests WHERE manifest_id = ?", (manifest_id,)
        )

    def latest(self) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM runtime_manifests ORDER BY created_at DESC, manifest_id DESC LIMIT 1"
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM runtime_manifests") or 0)
