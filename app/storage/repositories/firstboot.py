"""FIRST BOOT state and lease persistence (rebuild spec 34.20 — Phase 13).

Two things live here and nowhere else: what the FIRST BOOT state machine
currently says, and who is allowed to move it. Both are in the database rather
than in a process, because the case they exist for is the process going away.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.clock import from_iso, to_iso
from app.storage.database import Database

#: The lease is single-row by construction.
LOCK_ID = "first_boot"

#: How long a lease survives without a heartbeat. Past this the holder is
#: presumed dead — a crashed process cannot release its own lock, so the only
#: alternative is a lock nobody can ever take again.
LEASE_MINUTES = 15.0


class FirstBootRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- state ---------------------------------------------------------------
    def ensure(self, epoch_id: str, *, schema_version: int = 0) -> sqlite3.Row:
        """The row for this rebuild epoch, created if this is the first look.

        One row per epoch (point 52): FIRST BOOT happens once per rebuild, not
        once per installation, so a reset gives her a new epoch and a new
        chance at being born.
        """
        existing = self.get(epoch_id)
        if existing is not None:
            return existing
        self._db.execute(
            "INSERT INTO first_boot_state (epoch_id, status, schema_version) "
            "VALUES (?, 'PENDING', ?)",
            (epoch_id, schema_version),
        )
        return self.get(epoch_id)  # type: ignore[return-value]

    def get(self, epoch_id: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM first_boot_state WHERE epoch_id = ?", (epoch_id,)
        )

    def current(self) -> sqlite3.Row | None:
        """The newest epoch's state. What startup reads."""
        return self._db.query_one(
            "SELECT * FROM first_boot_state ORDER BY rowid DESC LIMIT 1"
        )

    def authoritative_genesis_run_id(self) -> str | None:
        """The run whose life she is actually living, or ``None``.

        The single source for "which Genesis life is authoritative". World
        provenance used to answer this with the most recently started run,
        which is a different question: a later run can exist without FIRST BOOT
        ever having pointed at it, and then a stale life passes because a newer
        one is sitting beside it in the table.

        ``None`` means FIRST BOOT has not attached a run — a life that has not
        begun. That is not a licence to go looking for one.
        """
        row = self.current()
        if row is None:
            return None
        return row["genesis_run_id"] or None

    def transition(
        self,
        epoch_id: str,
        *,
        expected: tuple[str, ...],
        to: str,
        now: datetime,
        **fields: Any,
    ) -> bool:
        """Compare-and-set. Returns False if the state was not what we thought.

        The atomicity that stops two processes both deciding they may start.
        A plain read-then-write would let both read `PENDING`, both decide yes,
        and both begin generating a life.
        """
        placeholders = ", ".join("?" for _ in expected)
        assignments = ["status = ?"]
        params: list[Any] = [to]
        for column, value in fields.items():
            assignments.append(f"{column} = ?")
            params.append(
                to_iso(value) if isinstance(value, datetime) else value
            )
        params.extend([epoch_id, *expected])
        cursor = self._db.execute(
            f"UPDATE first_boot_state SET {', '.join(assignments)} "
            f"WHERE epoch_id = ? AND status IN ({placeholders})",
            tuple(params),
        )
        return cursor.rowcount == 1

    def touch(
        self,
        epoch_id: str,
        *,
        now: datetime,
        stage: str = "",
        target_id: str = "",
        checkpoint: str = "",
    ) -> None:
        """A heartbeat with meaning (point 20).

        Written at the end of a meaningful unit rather than on a timer, so the
        row answers "what is it doing" and not merely "is it alive".
        """
        self._db.execute(
            "UPDATE first_boot_state SET last_progress_at = ?, current_stage = ?, "
            "current_target_id = ?, last_checkpoint = CASE WHEN ? != '' THEN ? "
            "ELSE last_checkpoint END WHERE epoch_id = ?",
            (to_iso(now), stage, target_id, checkpoint, checkpoint, epoch_id),
        )

    def attach_run(self, epoch_id: str, genesis_run_id: str) -> None:
        self._db.execute(
            "UPDATE first_boot_state SET genesis_run_id = ? WHERE epoch_id = ?",
            (genesis_run_id, epoch_id),
        )

    def save_report(self, epoch_id: str, report_json: str) -> None:
        self._db.execute(
            "UPDATE first_boot_state SET report_json = ? WHERE epoch_id = ?",
            (report_json, epoch_id),
        )

    def bump_attempt(self, epoch_id: str) -> None:
        self._db.execute(
            "UPDATE first_boot_state SET attempt_count = attempt_count + 1 "
            "WHERE epoch_id = ?",
            (epoch_id,),
        )

    def set_fingerprint(self, epoch_id: str, fingerprint: str) -> None:
        self._db.execute(
            "UPDATE first_boot_state SET anchors_fingerprint = ? WHERE epoch_id = ?",
            (fingerprint, epoch_id),
        )

    # --- the lease (point 27, 28) --------------------------------------------
    def acquire(self, epoch_id: str, *, now: datetime, holder: str | None = None) -> bool:
        """Take the lease, or fail because somebody else holds a live one."""
        who = holder or f"{os.getpid()}"
        existing = self._db.query_one(
            "SELECT * FROM first_boot_lock WHERE lock_id = ?", (LOCK_ID,)
        )
        if existing is not None:
            beat = from_iso(existing["heartbeat_at"])
            if now - beat < timedelta(minutes=LEASE_MINUTES):
                return existing["holder"] == who  # re-entrant for the same process
            # Expired: the holder is gone and cannot release it itself.
            self._db.execute("DELETE FROM first_boot_lock WHERE lock_id = ?", (LOCK_ID,))
        try:
            self._db.execute(
                "INSERT INTO first_boot_lock (lock_id, epoch_id, holder, acquired_at, "
                "heartbeat_at) VALUES (?, ?, ?, ?, ?)",
                (LOCK_ID, epoch_id, who, to_iso(now), to_iso(now)),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def beat(self, *, now: datetime, holder: str | None = None) -> None:
        self._db.execute(
            "UPDATE first_boot_lock SET heartbeat_at = ? WHERE lock_id = ? AND holder = ?",
            (to_iso(now), LOCK_ID, holder or f"{os.getpid()}"),
        )

    def release(self, *, holder: str | None = None) -> None:
        self._db.execute(
            "DELETE FROM first_boot_lock WHERE lock_id = ? AND holder = ?",
            (LOCK_ID, holder or f"{os.getpid()}"),
        )

    def lock_holder(self) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM first_boot_lock WHERE lock_id = ?", (LOCK_ID,)
        )

    def lease_is_live(self, *, now: datetime) -> bool:
        row = self.lock_holder()
        if row is None:
            return False
        return now - from_iso(row["heartbeat_at"]) < timedelta(minutes=LEASE_MINUTES)


__all__ = ["LEASE_MINUTES", "LOCK_ID", "FirstBootRepository"]
