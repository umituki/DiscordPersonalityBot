"""Subjective memory domain models (spec 10).

These are YUI's memories, not the archive. A memory is a *reconstruction* of
what happened: it has its own confidences, it fades, and it can be revised
without touching the events it came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware
from app.events.model import EventOrigin

EpisodeStatus = Literal["open", "closed", "encoded", "discarded"]
MemoryStatus = Literal["active", "suppressed", "invalidated"]
LinkRelation = Literal["associated", "derived_from", "contradicts", "summarizes", "continues"]
#: Spec 24 stability classes.
Stability = Literal["STATIC", "SLOW_CHANGING", "CHANGEABLE", "FAST_CHANGING", "EPHEMERAL"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class Episode(_Frozen):
    """A bounded stretch of experience (spec 8.4)."""

    episode_id: str
    conversation_id: str | None
    origin: EventOrigin
    started_at: datetime
    ended_at: datetime | None = None
    event_count: int = Field(default=0, ge=0)
    status: EpisodeStatus = "open"
    boundary_reason: str | None = None
    event_ids: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class EpisodicMemory(_Frozen):
    """One remembered episode (spec 10.3)."""

    memory_id: str
    episode_id: str
    origin: EventOrigin
    summary: str
    topics: tuple[str, ...] = ()
    #: How much this mattered. Independent of how reachable it is (spec 10.5).
    importance: float = Field(ge=0.0, le=1.0)
    emotional_intensity: float = Field(default=0.0, ge=0.0, le=1.0)
    #: How reachable it currently is. Decays with time, rises with recall.
    accessibility: float = Field(ge=0.0, le=1.0)
    content_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    source_confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    temporal_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    novelty: float = Field(default=0.0, ge=0.0, le=1.0)
    prediction_error: float = Field(default=0.0, ge=0.0, le=1.0)
    recall_count: int = Field(default=0, ge=0)
    last_recalled_at: datetime | None = None
    last_decayed_at: datetime | None = None
    occurred_at: datetime
    created_at: datetime
    updated_at: datetime
    revision_count: int = Field(default=0, ge=0)
    status: MemoryStatus = "active"
    source_event_ids: tuple[str, ...] = ()

    @property
    def is_recallable(self) -> bool:
        """Suppressed and invalidated memories never surface in normal recall."""
        return self.status == "active"

    @property
    def is_from_real_history(self) -> bool:
        return self.origin == "real_discord"


#: What a piece of general knowledge is *about*, recorded by whoever formed it.
#:
#: Distinct from ``origin``, which says where it came from. The two answer
#: different questions and neither implies the other: a fact acquired through
#: real Discord conversation can be about the world, and a fact consolidated in
#: virtual life is about her. Deriving one from the other is how a subject gets
#: assigned by accident.
#:
#: ``unknown`` is the honest answer for a row written before anything recorded
#: this, and it grounds nothing — which is the safe direction.
SemanticSubject = Literal["yui", "user", "other", "world", "unknown"]


class SemanticMemory(_Frozen):
    """A general fact YUI believes she knows (spec 10.2)."""

    semantic_id: str
    statement: str
    topics: tuple[str, ...] = ()
    origin: EventOrigin
    #: Whose life or which world this knowledge is about. Set by the subsystem
    #: that formed the memory, never inferred from the statement text.
    subject: SemanticSubject = "unknown"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    stability: Stability = "CHANGEABLE"
    support_count: int = Field(default=1, ge=0)
    contradiction_count: int = Field(default=0, ge=0)
    first_learned_at: datetime
    updated_at: datetime
    status: MemoryStatus = "active"
    source_memory_ids: tuple[str, ...] = ()


class MemoryLink(_Frozen):
    link_id: str
    from_memory_id: str
    to_memory_id: str
    relation: LinkRelation
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime


class RetrievalCandidate(_Frozen):
    """A memory the retriever considered, with why it scored as it did."""

    memory: EpisodicMemory
    score: float
    relevance: float
    components: dict[str, float] = Field(default_factory=dict)

    @property
    def memory_id(self) -> str:
        return self.memory.memory_id

    def as_context_line(self) -> str:
        return f"- {self.memory.summary}"
