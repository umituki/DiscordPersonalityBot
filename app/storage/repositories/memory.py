"""Subjective memory persistence (spec 10, 31.4).

Only the Memory Engine writes through this repository: ``subjective_memory`` is
its owned domain (spec 9.3). Forgetting never deletes a row — it lowers
``accessibility`` (spec 10.5).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Iterable, Sequence

from app import ids
from app.clock import from_iso, to_iso
from app.memory.models import (
    Episode,
    EpisodicMemory,
    MemoryLink,
    SemanticMemory,
)
from app.storage.database import Database

_MEMORY_COLUMNS = """
    memory_id, episode_id, origin, summary, topics_json, importance, emotional_intensity,
    accessibility, content_confidence, source_confidence, temporal_confidence, novelty,
    prediction_error, recall_count, last_recalled_at, last_decayed_at, occurred_at,
    created_at, updated_at, revision_count, status, source_event_ids_json
"""


class MemoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- episodes ----------------------------------------------------------
    def open_episode(
        self,
        *,
        conversation_id: str | None,
        origin: str,
        started_at: datetime,
        first_event_id: str | None = None,
    ) -> Episode:
        episode_id = ids.new_id(ids.EPISODE)
        event_ids = [first_event_id] if first_event_id else []
        self._db.execute(
            """
            INSERT INTO episodes
                (episode_id, conversation_id, origin, started_at, event_count,
                 status, event_ids_json)
            VALUES (?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                episode_id,
                conversation_id,
                origin,
                to_iso(started_at),
                len(event_ids),
                json.dumps(event_ids),
            ),
        )
        return self.get_episode(episode_id)  # type: ignore[return-value]

    def append_to_episode(self, episode_id: str, event_id: str) -> Episode:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT event_ids_json FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown episode: {episode_id}")
            event_ids = json.loads(row["event_ids_json"])
            if event_id not in event_ids:
                event_ids.append(event_id)
                connection.execute(
                    "UPDATE episodes SET event_ids_json = ?, event_count = ? "
                    "WHERE episode_id = ?",
                    (json.dumps(event_ids), len(event_ids), episode_id),
                )
        return self.get_episode(episode_id)  # type: ignore[return-value]

    def touch_episode(self, episode_id: str, moment: datetime) -> Episode:
        """Keep ``ended_at`` tracking the last activity of an open episode."""
        self._db.execute(
            "UPDATE episodes SET ended_at = ? WHERE episode_id = ?",
            (to_iso(moment), episode_id),
        )
        return self.get_episode(episode_id)  # type: ignore[return-value]

    def close_episode(
        self, episode_id: str, *, ended_at: datetime, reason: str, status: str = "closed"
    ) -> Episode:
        self._db.execute(
            "UPDATE episodes SET status = ?, ended_at = ?, boundary_reason = ? "
            "WHERE episode_id = ?",
            (status, to_iso(ended_at), reason, episode_id),
        )
        return self.get_episode(episode_id)  # type: ignore[return-value]

    def mark_episode(self, episode_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE episodes SET status = ? WHERE episode_id = ?", (status, episode_id)
        )

    def get_episode(self, episode_id: str) -> Episode | None:
        row = self._db.query_one("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,))
        return None if row is None else _to_episode(row)

    def open_episode_for(
        self, conversation_id: str | None, *, origin: str | None = None
    ) -> Episode | None:
        """The open episode of one stream.

        ``origin`` matters when ``conversation_id`` is NULL: a simulated life
        and a virtual-life day both have no conversation, and without the
        origin they would be appended to each other's episode (patch spec 15).
        """
        if origin is None:
            return self._first_open(
                "status = 'open' AND conversation_id IS ?", (conversation_id,)
            )
        return self._first_open(
            "status = 'open' AND conversation_id IS ? AND origin = ?",
            (conversation_id, origin),
        )

    def _first_open(self, where: str, params: tuple) -> Episode | None:
        row = self._db.query_one(
            f"SELECT * FROM episodes WHERE {where} ORDER BY started_at DESC LIMIT 1",
            params,
        )
        return None if row is None else _to_episode(row)

    def episodes_with_status(self, status: str, *, limit: int = 100) -> list[Episode]:
        rows = self._db.query_all(
            "SELECT * FROM episodes WHERE status = ? ORDER BY started_at LIMIT ?",
            (status, limit),
        )
        return [_to_episode(row) for row in rows]

    # --- episodic memories -------------------------------------------------
    def insert_memory(self, memory: EpisodicMemory) -> bool:
        """Store a memory. Encoding the same episode twice is a no-op."""
        with self._db.transaction() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO episodic_memories ({_MEMORY_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory.memory_id,
                    memory.episode_id,
                    memory.origin,
                    memory.summary,
                    json.dumps(list(memory.topics), ensure_ascii=False),
                    memory.importance,
                    memory.emotional_intensity,
                    memory.accessibility,
                    memory.content_confidence,
                    memory.source_confidence,
                    memory.temporal_confidence,
                    memory.novelty,
                    memory.prediction_error,
                    memory.recall_count,
                    _optional_iso(memory.last_recalled_at),
                    _optional_iso(memory.last_decayed_at),
                    to_iso(memory.occurred_at),
                    to_iso(memory.created_at),
                    to_iso(memory.updated_at),
                    memory.revision_count,
                    memory.status,
                    json.dumps(list(memory.source_event_ids)),
                ),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO episodic_memories_fts (memory_id, summary, topics) VALUES (?, ?, ?)",
                (memory.memory_id, memory.summary, " ".join(memory.topics)),
            )
        return True

    def get_memory(self, memory_id: str) -> EpisodicMemory | None:
        row = self._db.query_one(
            f"SELECT {_MEMORY_COLUMNS} FROM episodic_memories WHERE memory_id = ?", (memory_id,)
        )
        return None if row is None else _to_memory(row)

    def memory_for_episode(self, episode_id: str) -> EpisodicMemory | None:
        row = self._db.query_one(
            f"SELECT {_MEMORY_COLUMNS} FROM episodic_memories WHERE episode_id = ?", (episode_id,)
        )
        return None if row is None else _to_memory(row)

    def search(
        self,
        *,
        match_query: str | None,
        limit: int,
        origins: Sequence[str] | None = None,
        status: str = "active",
    ) -> list[EpisodicMemory]:
        """Candidate memories, optionally narrowed by full-text match."""
        params: list[object] = []
        where = ["m.status = ?"]
        params.append(status)
        if origins:
            where.append(f"m.origin IN ({', '.join('?' for _ in origins)})")
            params.extend(origins)

        if match_query:
            sql = (
                f"SELECT {_memory_columns('m')} FROM episodic_memories_fts f "
                "JOIN episodic_memories m ON m.memory_id = f.memory_id "
                f"WHERE episodic_memories_fts MATCH ? AND {' AND '.join(where)} "
                "ORDER BY bm25(episodic_memories_fts) LIMIT ?"
            )
            params = [match_query, *params, limit]
        else:
            sql = (
                f"SELECT {_memory_columns('m')} FROM episodic_memories m "
                f"WHERE {' AND '.join(where)} ORDER BY m.occurred_at DESC LIMIT ?"
            )
            params = [*params, limit]
        return [_to_memory(row) for row in self._db.query_all(sql, params)]

    def by_topics(
        self,
        topics: Sequence[str],
        *,
        limit: int,
        origins: Sequence[str] | None = None,
        status: str = "active",
    ) -> list[EpisodicMemory]:
        """Memories tagged with any of these topics (Phase 2 §2B, §2G).

        Topics are stored as a JSON array, so this is a LIKE over the encoded
        form. It is a candidate-generation read: imprecision here costs an
        extra candidate for Stage 2 to reject, never a false recall.
        """
        wanted = [topic.strip() for topic in topics if topic and topic.strip()]
        if not wanted:
            return []
        where = ["m.status = ?"]
        params: list[object] = [status]
        if origins:
            where.append(f"m.origin IN ({', '.join('?' for _ in origins)})")
            params.extend(origins)
        clauses = " OR ".join("m.topics_json LIKE ?" for _ in wanted)
        params.extend(f"%{topic}%" for topic in wanted)
        sql = (
            f"SELECT {_memory_columns('m')} FROM episodic_memories m "
            f"WHERE {' AND '.join(where)} AND ({clauses}) "
            "ORDER BY m.importance DESC, m.occurred_at DESC LIMIT ?"
        )
        params.append(limit)
        return [_to_memory(row) for row in self._db.query_all(sql, params)]

    def occurred_between(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int,
        origins: Sequence[str] | None = None,
        status: str = "active",
    ) -> list[EpisodicMemory]:
        """Memories from a stretch of time (Phase 2 §2B time hints)."""
        where = ["m.status = ?", "m.occurred_at >= ?", "m.occurred_at <= ?"]
        params: list[object] = [status, to_iso(start), to_iso(end)]
        if origins:
            where.append(f"m.origin IN ({', '.join('?' for _ in origins)})")
            params.extend(origins)
        sql = (
            f"SELECT {_memory_columns('m')} FROM episodic_memories m "
            f"WHERE {' AND '.join(where)} ORDER BY m.occurred_at DESC LIMIT ?"
        )
        params.append(limit)
        return [_to_memory(row) for row in self._db.query_all(sql, params)]

    def most_important(
        self,
        *,
        limit: int,
        origins: Sequence[str] | None = None,
        min_importance: float = 0.0,
        status: str = "active",
    ) -> list[EpisodicMemory]:
        """The memories that mattered most (Phase 2 §2B, reflective modes)."""
        where = ["m.status = ?", "m.importance >= ?"]
        params: list[object] = [status, min_importance]
        if origins:
            where.append(f"m.origin IN ({', '.join('?' for _ in origins)})")
            params.extend(origins)
        sql = (
            f"SELECT {_memory_columns('m')} FROM episodic_memories m "
            f"WHERE {' AND '.join(where)} ORDER BY m.importance DESC LIMIT ?"
        )
        params.append(limit)
        return [_to_memory(row) for row in self._db.query_all(sql, params)]

    def recent_memories(self, *, limit: int = 20, status: str = "active") -> list[EpisodicMemory]:
        rows = self._db.query_all(
            f"SELECT {_MEMORY_COLUMNS} FROM episodic_memories WHERE status = ? "
            "ORDER BY occurred_at DESC LIMIT ?",
            (status, limit),
        )
        return [_to_memory(row) for row in rows]

    def all_memories(self, *, limit: int = 1000) -> list[EpisodicMemory]:
        rows = self._db.query_all(
            f"SELECT {_MEMORY_COLUMNS} FROM episodic_memories ORDER BY occurred_at LIMIT ?",
            (limit,),
        )
        return [_to_memory(row) for row in rows]

    def update_accessibility(
        self,
        memory_id: str,
        *,
        accessibility: float,
        now: datetime,
        decayed: bool = False,
        recalled: bool = False,
    ) -> None:
        if recalled:
            self._db.execute(
                "UPDATE episodic_memories SET accessibility = ?, recall_count = recall_count + 1, "
                "last_recalled_at = ?, updated_at = ? WHERE memory_id = ?",
                (accessibility, to_iso(now), to_iso(now), memory_id),
            )
        elif decayed:
            self._db.execute(
                "UPDATE episodic_memories SET accessibility = ?, last_decayed_at = ?, "
                "updated_at = ? WHERE memory_id = ?",
                (accessibility, to_iso(now), to_iso(now), memory_id),
            )
        else:
            self._db.execute(
                "UPDATE episodic_memories SET accessibility = ?, updated_at = ? "
                "WHERE memory_id = ?",
                (accessibility, to_iso(now), memory_id),
            )

    def set_status(self, memory_id: str, status: str, *, now: datetime) -> None:
        """Admin-facing state change (spec 30). Never a delete."""
        self._db.execute(
            "UPDATE episodic_memories SET status = ?, updated_at = ? WHERE memory_id = ?",
            (status, to_iso(now), memory_id),
        )

    def revise(
        self,
        *,
        memory_id: str,
        new_summary: str,
        reason_code: str,
        now: datetime,
        content_confidence: float | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Rewrite the remembered summary, keeping the previous one (spec 10.6)."""
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT summary, content_confidence FROM episodic_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown memory: {memory_id}")
            previous = row["summary"]
            confidence = (
                row["content_confidence"] if content_confidence is None else content_confidence
            )
            revision_id = ids.new_id(ids.REVISION)
            connection.execute(
                """
                INSERT INTO memory_revisions
                    (revision_id, memory_id, revised_at, reason_code, previous_summary,
                     new_summary, confidence_after, run_id, event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id, memory_id, to_iso(now), reason_code, previous, new_summary,
                    confidence, run_id, event_id,
                ),
            )
            connection.execute(
                "UPDATE episodic_memories SET summary = ?, content_confidence = ?, "
                "revision_count = revision_count + 1, updated_at = ? WHERE memory_id = ?",
                (new_summary, confidence, to_iso(now), memory_id),
            )
            connection.execute(
                "UPDATE episodic_memories_fts SET summary = ? WHERE memory_id = ?",
                (new_summary, memory_id),
            )
        return revision_id

    def revisions(self, memory_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM memory_revisions WHERE memory_id = ? ORDER BY revised_at", (memory_id,)
        )

    # --- retrieval history -------------------------------------------------
    def record_retrieval(
        self,
        *,
        memory_id: str,
        query: str,
        score: float,
        rank: int,
        now: datetime,
        used: bool,
        run_id: str | None = None,
        event_id: str | None = None,
        group_id: str = "",
        mode: str = "CONVERSATIONAL",
        state: str = "candidate",
        relevance: str = "",
        relevance_source: str = "",
        relevance_reason: str = "",
        reject_stage: str = "",
        reject_reason: str = "",
        accessibility_at: float | None = None,
        availability: float | None = None,
        candidate_reasons: Sequence[str] = (),
        used_in_reply: bool = False,
        practice_applied: bool = False,
        llm_call_id: str | None = None,
    ) -> str:
        """One row per candidate per retrieval (Phase 2 §2J, observability).

        ``used`` is kept for the old readers and means what it now says
        everywhere: the memory reached the reply context. It is no longer set
        merely because a search returned the row.
        """
        retrieval_id = ids.new_id(ids.RETRIEVAL)
        self._db.execute(
            """
            INSERT INTO memory_retrievals
                (retrieval_id, memory_id, run_id, event_id, query, score, rank, used,
                 retrieved_at, group_id, mode, state, relevance, relevance_source,
                 relevance_reason, reject_stage, reject_reason, accessibility_at,
                 availability, candidate_reasons, used_in_reply, practice_applied,
                 llm_call_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                retrieval_id, memory_id, run_id, event_id, query[:500], score, rank,
                1 if used else 0, to_iso(now), group_id, mode, state, relevance,
                relevance_source, relevance_reason[:200], reject_stage,
                reject_reason[:200], accessibility_at, availability,
                ",".join(candidate_reasons)[:200], 1 if used_in_reply else 0,
                1 if practice_applied else 0, llm_call_id,
            ),
        )
        return retrieval_id

    def promote_retrieval(
        self,
        *,
        group_id: str,
        memory_id: str,
        state: str,
        used_in_reply: bool = False,
        practice_applied: bool = False,
    ) -> int:
        """Move one memory further along the retrieval states (§2J).

        States only ever advance, so a later stage cannot quietly demote what an
        earlier one recorded.
        """
        cursor = self._db.execute(
            "UPDATE memory_retrievals SET state = ?, "
            "used_in_reply = MAX(used_in_reply, ?), "
            "practice_applied = MAX(practice_applied, ?) "
            "WHERE group_id = ? AND memory_id = ?",
            (state, 1 if used_in_reply else 0, 1 if practice_applied else 0,
             group_id, memory_id),
        )
        return cursor.rowcount if cursor is not None else 0

    def retrievals_in_group(self, group_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM memory_retrievals WHERE group_id = ? ORDER BY rank",
            (group_id,),
        )

    def practices_since(self, memory_id: str, moment: datetime) -> int:
        """How often this memory was actually practised lately (§2L).

        Counts ``practice_applied``, never candidate lookups: repeating a
        search is not repeating a memory, and counting it was half of what
        drove accessibility to saturation.
        """
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM memory_retrievals WHERE memory_id = ? "
                "AND practice_applied = 1 AND retrieved_at >= ?",
                (memory_id, to_iso(moment)),
            )
            or 0
        )

    def retrievals_since(self, memory_id: str, moment: datetime) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM memory_retrievals WHERE memory_id = ? AND used = 1 "
                "AND retrieved_at >= ?",
                (memory_id, to_iso(moment)),
            )
            or 0
        )

    # --- semantic memory ---------------------------------------------------
    def upsert_semantic(
        self,
        *,
        statement: str,
        origin: str,
        topics: Sequence[str],
        confidence: float,
        stability: str,
        now: datetime,
        source_memory_ids: Sequence[str] = (),
    ) -> SemanticMemory:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM semantic_memories WHERE statement = ? AND origin = ?",
                (statement, origin),
            ).fetchone()
            if row is None:
                semantic_id = ids.new_id(ids.SEMANTIC)
                connection.execute(
                    """
                    INSERT INTO semantic_memories
                        (semantic_id, statement, topics_json, origin, confidence, stability,
                         support_count, contradiction_count, first_learned_at, updated_at,
                         status, source_memory_ids_json)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, 'active', ?)
                    """,
                    (
                        semantic_id, statement, json.dumps(list(topics), ensure_ascii=False),
                        origin, confidence, stability, to_iso(now), to_iso(now),
                        json.dumps(list(source_memory_ids)),
                    ),
                )
            else:
                semantic_id = row["semantic_id"]
                merged = sorted(
                    set(json.loads(row["source_memory_ids_json"])) | set(source_memory_ids)
                )
                connection.execute(
                    "UPDATE semantic_memories SET support_count = support_count + 1, "
                    "confidence = ?, updated_at = ?, source_memory_ids_json = ? "
                    "WHERE semantic_id = ?",
                    (confidence, to_iso(now), json.dumps(merged), semantic_id),
                )
            result = connection.execute(
                "SELECT * FROM semantic_memories WHERE semantic_id = ?", (semantic_id,)
            ).fetchone()
        return _to_semantic(result)

    def get_semantic(self, semantic_id: str) -> SemanticMemory | None:
        row = self._db.query_one(
            "SELECT * FROM semantic_memories WHERE semantic_id = ?", (semantic_id,)
        )
        return None if row is None else _to_semantic(row)

    def semantic_by_statement(self, statement: str, origin: str) -> SemanticMemory | None:
        row = self._db.query_one(
            "SELECT * FROM semantic_memories WHERE statement = ? AND origin = ?",
            (statement, origin),
        )
        return None if row is None else _to_semantic(row)

    def active_semantic(self, *, limit: int = 50) -> list[SemanticMemory]:
        rows = self._db.query_all(
            "SELECT * FROM semantic_memories WHERE status = 'active' "
            "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (limit,),
        )
        return [_to_semantic(row) for row in rows]

    # --- links -------------------------------------------------------------
    def link(
        self,
        *,
        from_memory_id: str,
        to_memory_id: str,
        relation: str,
        strength: float,
        now: datetime,
    ) -> bool:
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO memory_links "
            "(link_id, from_memory_id, to_memory_id, relation, strength, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ids.new_id(ids.MEMORY_LINK), from_memory_id, to_memory_id, relation, strength, to_iso(now)),
        )
        return cursor.rowcount == 1

    def links_of(self, memory_id: str) -> list[MemoryLink]:
        rows = self._db.query_all(
            "SELECT * FROM memory_links WHERE from_memory_id = ? OR to_memory_id = ? "
            "ORDER BY created_at",
            (memory_id, memory_id),
        )
        return [
            MemoryLink(
                link_id=row["link_id"],
                from_memory_id=row["from_memory_id"],
                to_memory_id=row["to_memory_id"],
                relation=row["relation"],
                strength=row["strength"],
                created_at=from_iso(row["created_at"]),
            )
            for row in rows
        ]

    # --- counters ----------------------------------------------------------
    def memory_count(self, *, status: str | None = None) -> int:
        if status is None:
            return int(self._db.scalar("SELECT COUNT(*) FROM episodic_memories") or 0)
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM episodic_memories WHERE status = ?", (status,)
            )
            or 0
        )

    def episode_count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM episodes") or 0)

    def semantic_count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM semantic_memories") or 0)


def _memory_columns(alias: str) -> str:
    return ", ".join(f"{alias}.{column.strip()}" for column in _MEMORY_COLUMNS.split(","))


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else to_iso(value)


def _optional_dt(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


def _to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        episode_id=row["episode_id"],
        conversation_id=row["conversation_id"],
        origin=row["origin"],
        started_at=from_iso(row["started_at"]),
        ended_at=_optional_dt(row["ended_at"]),
        event_count=int(row["event_count"]),
        status=row["status"],
        boundary_reason=row["boundary_reason"],
        event_ids=tuple(json.loads(row["event_ids_json"])),
    )


def _to_memory(row: sqlite3.Row) -> EpisodicMemory:
    return EpisodicMemory(
        memory_id=row["memory_id"],
        episode_id=row["episode_id"],
        origin=row["origin"],
        summary=row["summary"],
        topics=tuple(json.loads(row["topics_json"])),
        importance=row["importance"],
        emotional_intensity=row["emotional_intensity"],
        accessibility=row["accessibility"],
        content_confidence=row["content_confidence"],
        source_confidence=row["source_confidence"],
        temporal_confidence=row["temporal_confidence"],
        novelty=row["novelty"],
        prediction_error=row["prediction_error"],
        recall_count=int(row["recall_count"]),
        last_recalled_at=_optional_dt(row["last_recalled_at"]),
        last_decayed_at=_optional_dt(row["last_decayed_at"]),
        occurred_at=from_iso(row["occurred_at"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        revision_count=int(row["revision_count"]),
        status=row["status"],
        source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
    )


def _to_semantic(row: sqlite3.Row) -> SemanticMemory:
    return SemanticMemory(
        semantic_id=row["semantic_id"],
        statement=row["statement"],
        topics=tuple(json.loads(row["topics_json"])),
        origin=row["origin"],
        confidence=row["confidence"],
        stability=row["stability"],
        support_count=int(row["support_count"]),
        contradiction_count=int(row["contradiction_count"]),
        first_learned_at=from_iso(row["first_learned_at"]),
        updated_at=from_iso(row["updated_at"]),
        status=row["status"],
        source_memory_ids=tuple(json.loads(row["source_memory_ids_json"])),
    )


def iter_memory_ids(memories: Iterable[EpisodicMemory]) -> tuple[str, ...]:
    return tuple(memory.memory_id for memory in memories)
