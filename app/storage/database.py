"""SQLite connection and transaction authority.

Spec 31.1: production SQLite is the single authority for dynamic state.
Spec 32: exactly one writer process.

Design notes
------------
* One connection, guarded by a re-entrant lock. ``transaction()`` is the only
  way to write; nested calls join the outer transaction so a commit is
  all-or-nothing across domains (spec storage rule: "write multi-domain state
  atomically").
* Calls are synchronous. Async callers hand the whole unit of work to
  :meth:`run` which executes it on a worker thread, so a transaction never
  spans an ``await``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

SQLParams = Sequence[Any] | Mapping[str, Any]

MEMORY = ":memory:"


class DatabaseError(RuntimeError):
    """Raised for database lifecycle misuse."""


class Database:
    """Owns the process' SQLite connection."""

    def __init__(
        self,
        path: Path | str,
        *,
        journal_mode: str = "WAL",
        synchronous: str = "FULL",
        busy_timeout_ms: int = 5000,
        foreign_keys: bool = True,
    ) -> None:
        self._path = MEMORY if str(path) == MEMORY else Path(path)
        self._journal_mode = journal_mode
        self._synchronous = synchronous
        self._busy_timeout_ms = busy_timeout_ms
        self._foreign_keys = foreign_keys
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._depth = 0

    # --- lifecycle ---------------------------------------------------------
    @property
    def path(self) -> Path | str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is not None:
                return self._connection
            if isinstance(self._path, Path):
                self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self._path if isinstance(self._path, str) else str(self._path),
                timeout=self._busy_timeout_ms / 1000,
                isolation_level=None,  # explicit transaction control
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
            connection.execute(f"PRAGMA foreign_keys = {'ON' if self._foreign_keys else 'OFF'}")
            if self._path != MEMORY:
                connection.execute(f"PRAGMA journal_mode = {self._journal_mode}")
            connection.execute(f"PRAGMA synchronous = {self._synchronous}")
            self._connection = connection
            logger.info("database opened path=%s journal=%s", self._path, self._journal_mode)
            return connection

    def close(self) -> None:
        with self._lock:
            if self._depth:
                raise DatabaseError("cannot close the database inside a transaction")
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                logger.info("database closed path=%s", self._path)

    def __enter__(self) -> Database:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- transactions ------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit of work. Nested uses join the outermost transaction."""
        with self._lock:
            connection = self.connect()
            outermost = self._depth == 0
            if outermost:
                connection.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield connection
            except BaseException:
                self._depth -= 1
                if outermost:
                    connection.execute("ROLLBACK")
                    logger.warning("transaction rolled back path=%s", self._path)
                raise
            else:
                self._depth -= 1
                if outermost:
                    connection.execute("COMMIT")

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    # --- reads / writes ----------------------------------------------------
    def execute(self, sql: str, params: SQLParams = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.connect().execute(sql, params)

    def execute_many(self, sql: str, params: Iterable[SQLParams]) -> sqlite3.Cursor:
        with self._lock:
            return self.connect().executemany(sql, params)

    def query_all(self, sql: str, params: SQLParams = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connect().execute(sql, params).fetchall())

    def query_one(self, sql: str, params: SQLParams = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.connect().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: SQLParams = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # --- async bridge ------------------------------------------------------
    async def run(self, work: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Execute a synchronous unit of work off the event loop."""
        return await asyncio.to_thread(work, *args, **kwargs)

    # --- integrity ---------------------------------------------------------
    def integrity_check(self) -> str:
        """Return ``ok`` or the first reported integrity problem (spec 32)."""
        row = self.query_one("PRAGMA integrity_check")
        return "ok" if row is None else str(row[0])


# --- backup file inspection (spec 32) --------------------------------------
#: Tables a restore test reads. If a copy cannot answer these, it is not a
#: database anybody could come back to.
RESTORE_TEST_TABLES: tuple[str, ...] = ("events", "state_values", "migrations")


def verify_file(path: Path) -> tuple[str, int]:
    """Integrity-check a database *file* and read its schema version.

    Used on backup copies, which are not the live connection and therefore
    cannot go through :class:`Database` (spec 32).
    """
    connection = sqlite3.connect(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return integrity, int(row[0]) if row else 0
    except sqlite3.Error as exc:
        return f"error: {exc}", 0
    finally:
        connection.close()


def restore_test(path: Path) -> bool:
    """Open a backup and read from it. An untested backup is a hope (spec 32)."""
    connection = sqlite3.connect(path)
    try:
        for table in RESTORE_TEST_TABLES:
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.Error as exc:
        logger.error("restore test failed for %s: %r", path, exc)
        return False
    finally:
        connection.close()
    return True
