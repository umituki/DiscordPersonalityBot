"""Typed events owned by the subjective-memory domain."""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

MEMORY_SPONTANEOUSLY_RECALLED = "MEMORY_SPONTANEOUSLY_RECALLED"


@register_payload(MEMORY_SPONTANEOUSLY_RECALLED)
class MemorySpontaneouslyRecalledPayload(EventPayload):
    """A committed recall, without copying private memory text into events."""

    cue_id: str
    cue_types: tuple[str, ...]
    retrieval_group_id: str
    primary_memory_id: str
    memory_ids: tuple[str, ...]
    selected_count: int
    minimum_availability: float
    maximum_availability: float


__all__ = [
    "MEMORY_SPONTANEOUSLY_RECALLED",
    "MemorySpontaneouslyRecalledPayload",
]
