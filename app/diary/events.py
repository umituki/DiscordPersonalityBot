"""Diary events (rebuild spec 26.3, 26.7, 26.8).

``DIARY_WRITTEN 自体は経験`` — writing the diary is something she did, and it
goes through the normal pipeline like anything else she does. The entry's
*content* is not thereby an experience; that distinction is 26.7's, and it is
the reason these are two different things rather than one.
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

BEDTIME_REFLECTION_STARTED = "BEDTIME_REFLECTION_STARTED"
DIARY_WRITTEN = "DIARY_WRITTEN"
DIARY_READ = "DIARY_READ"


@register_payload(BEDTIME_REFLECTION_STARTED)
class BedtimeReflectionStartedPayload(EventPayload):
    """26.3. She sat down to look back at the day; nothing is written yet."""

    life_day_id: str
    intended_at: str = ""
    hours_awake: float = 0.0


@register_payload(DIARY_WRITTEN)
class DiaryWrittenPayload(EventPayload):
    diary_id: str
    life_day_id: str
    summary: str = ""
    #: True when a retry produced it after she was already asleep (26.3).
    late: bool = False
    attempts: int = 1
    referenced_memories: int = 0


@register_payload(DIARY_READ)
class DiaryReadPayload(EventPayload):
    """26.8. Reading the diary is a deliberate act with its own consequences.

    It is emphatically not what ordinary recall does — a memory she has lost
    stays lost until she goes and looks.
    """

    diary_id: str
    life_day_id: str
    summary: str = ""
    days_ago: int = 0


__all__ = [
    "BEDTIME_REFLECTION_STARTED",
    "DIARY_READ",
    "DIARY_WRITTEN",
    "BedtimeReflectionStartedPayload",
    "DiaryReadPayload",
    "DiaryWrittenPayload",
]
