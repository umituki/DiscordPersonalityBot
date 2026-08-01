"""Current dynamic state and its change history (spec 9, 31.3)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.clock import from_iso, to_iso
from app.state.value import JSONValue, StateValue, value_type_of
from app.storage.database import Database

_VALUE_COLUMNS = """
    domain, key, value_type, numeric_value, text_value, json_value, confidence,
    version, created_at, updated_at, updated_by_run_id, updated_by_event_id
"""


class StateRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- reads -------------------------------------------------------------
    def get(self, domain: str, key: str) -> StateValue | None:
        row = self._db.query_one(
            f"SELECT {_VALUE_COLUMNS} FROM state_values WHERE domain = ? AND key = ?",
            (domain, key),
        )
        return None if row is None else self._to_value(row)

    def list_domain(self, domain: str) -> list[StateValue]:
        rows = self._db.query_all(
            f"SELECT {_VALUE_COLUMNS} FROM state_values WHERE domain = ? ORDER BY key", (domain,)
        )
        return [self._to_value(row) for row in rows]

    def list_all(self) -> list[StateValue]:
        rows = self._db.query_all(
            f"SELECT {_VALUE_COLUMNS} FROM state_values ORDER BY domain, key"
        )
        return [self._to_value(row) for row in rows]

    def domains(self) -> list[str]:
        return [row["domain"] for row in self._db.query_all(
            "SELECT DISTINCT domain FROM state_values ORDER BY domain"
        )]

    # --- writes ------------------------------------------------------------
    def write_value(
        self,
        *,
        domain: str,
        key: str,
        value: JSONValue,
        confidence: float | None,
        now: datetime,
        run_id: str | None,
        event_id: str | None,
        expected_version: int | None,
    ) -> int:
        """Insert or update one key, returning the new version.

        ``expected_version`` implements optimistic concurrency: ``None`` means
        the caller believes the key does not exist yet.
        """
        timestamp = to_iso(now)
        value_type = value_type_of(value)
        numeric = float(value) if value_type == "number" else None
        text = value if value_type == "text" else None
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True) if value_type in (
            "json",
            "boolean",
        ) else None

        connection = self._db.connect()
        if expected_version is None:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO state_values ({_VALUE_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (
                    domain, key, value_type, numeric, text, encoded, confidence,
                    timestamp, timestamp, run_id, event_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentStateWriteError(
                    f"{domain}.{key} already exists; snapshot was stale"
                )
            return 1

        cursor = connection.execute(
            """
            UPDATE state_values
               SET value_type = ?, numeric_value = ?, text_value = ?, json_value = ?,
                   confidence = ?, version = version + 1, updated_at = ?,
                   updated_by_run_id = ?, updated_by_event_id = ?
             WHERE domain = ? AND key = ? AND version = ?
            """,
            (
                value_type, numeric, text, encoded, confidence, timestamp, run_id, event_id,
                domain, key, expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentStateWriteError(
                f"{domain}.{key} changed since version {expected_version}"
            )
        return expected_version + 1

    def record_change(
        self,
        *,
        change_id: str,
        run_id: str,
        source_event_id: str,
        proposal_id: str,
        source_module: str,
        domain: str,
        key: str,
        operation: str,
        value_type: str,
        previous_value: JSONValue,
        new_value: JSONValue,
        delta: float | None,
        confidence: float | None,
        version_after: int,
        reason_codes: tuple[str, ...],
        evidence_ids: tuple[str, ...],
        now: datetime,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO state_changes
                (change_id, run_id, source_event_id, proposal_id, source_module, domain, key,
                 operation, value_type, previous_value_json, new_value_json, delta, confidence,
                 version_after, reason_codes_json, evidence_ids_json, committed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change_id,
                run_id,
                source_event_id,
                proposal_id,
                source_module,
                domain,
                key,
                operation,
                value_type,
                json.dumps(previous_value, ensure_ascii=False, sort_keys=True),
                json.dumps(new_value, ensure_ascii=False, sort_keys=True),
                delta,
                confidence,
                version_after,
                json.dumps(list(reason_codes), ensure_ascii=False),
                json.dumps(list(evidence_ids), ensure_ascii=False),
                to_iso(now),
            ),
        )

    # --- history -----------------------------------------------------------
    def changes_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM state_changes WHERE run_id = ? ORDER BY committed_at, change_id",
            (run_id,),
        )

    def change_history(self, domain: str, key: str, *, limit: int = 50) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM state_changes WHERE domain = ? AND key = ? "
            "ORDER BY committed_at DESC, change_id DESC LIMIT ?",
            (domain, key, limit),
        )

    def change_count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM state_changes") or 0)

    # --- mapping -----------------------------------------------------------
    @staticmethod
    def _to_value(row: sqlite3.Row) -> StateValue:
        value_type = row["value_type"]
        if value_type == "number":
            value: JSONValue = row["numeric_value"]
        elif value_type == "text":
            value = row["text_value"]
        elif value_type in ("json", "boolean"):
            value = json.loads(row["json_value"])
        else:
            value = None
        return StateValue(
            domain=row["domain"],
            key=row["key"],
            value=value,
            confidence=row["confidence"],
            version=int(row["version"]),
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
            updated_by_run_id=row["updated_by_run_id"],
            updated_by_event_id=row["updated_by_event_id"],
        )


class ConcurrentStateWriteError(RuntimeError):
    """Raised when a commit would overwrite a newer value than it read."""
