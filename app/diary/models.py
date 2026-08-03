"""Diary domain models (rebuild spec 26, 36 — Phase 10).

26.1 draws the line this whole subsystem depends on::

    Event ≠ Memory ≠ Diary

An event is what happened. A memory is what she kept of it, already reshaped by
how it felt. A diary entry is neither: it is **what she wrote at the end of a
day about how that day went**, which is a third thing with its own authority
(26.6) — authoritative that she wrote it, and not authoritative about whether
what she wrote was true.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

#: 36. ``pending`` was intended and not yet written; ``late_written`` came out
#: of a retry after she was already asleep, and is deliberately not the same
#: status as one written at the time.
DiaryStatus = Literal["pending", "written", "late_written", "failed"]

#: 26.7 and 36. What an entry ended up talking about.
ReferenceType = Literal["event", "memory", "activity", "npc", "goal"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class LifeDay(_Frozen):
    """One day as she lived it (26.2).

        日付境界は 0:00 固定ではなく major wake → next major sleep

    A night that begins at 01:40 belongs to the day before it. Using the
    calendar date instead would split a single evening across two diary entries
    and give her a day that started in the middle of a conversation.
    """

    life_day_id: str
    started_at: datetime
    ended_at: datetime | None = None
    #: The sleep episodes on either side, when they are known.
    wake_sleep_id: str | None = None
    sleep_sleep_id: str | None = None
    ordinal: int = 0

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def hours(self, until: datetime) -> float:
        end = self.ended_at or until
        return max(0.0, (end - self.started_at).total_seconds() / 3600.0)


class DiaryReference(_Frozen):
    """Something the entry was about (26.7).

    ``mentioned_in_text`` separates "this was in the context she was given" from
    "she actually wrote about it", because only the second earns the light
    recall practice 26.7 permits.
    """

    reference_type: ReferenceType
    reference_id: str
    mentioned_in_text: bool = False


class DiaryEntry(_Frozen):
    """What she wrote, and when she meant to write it.

    ``intended_at`` and ``generated_at`` are separate (26.3, 36). A diary
    retried at four in the morning is still last night's diary; collapsing the
    two would make every late entry claim she was awake and reflective at a
    moment when she was asleep.
    """

    diary_id: str
    life_day_id: str
    intended_at: datetime
    generated_at: datetime | None = None
    sleep_episode_id: str | None = None
    content: str = ""
    summary: str = ""
    importance: float = Field(default=0.3, ge=0.0, le=1.0)
    mood_valence: float = Field(default=0.0, ge=-1.0, le=1.0)
    mood_arousal: float = Field(default=0.0, ge=0.0, le=1.0)
    prompt_version: str = ""
    model_version: str = ""
    attempts: int = Field(default=0, ge=0)
    status: DiaryStatus = "pending"
    references: tuple[DiaryReference, ...] = ()

    @property
    def is_written(self) -> bool:
        return self.status in ("written", "late_written")

    @property
    def was_late(self) -> bool:
        return self.status == "late_written"

    @property
    def mentioned(self) -> tuple[DiaryReference, ...]:
        """Only what she actually wrote about (26.7)."""
        return tuple(item for item in self.references if item.mentioned_in_text)


class DiaryDraft(BaseModel):
    """What the model returns. Free prose (26.5).

    ``固定フォーム禁止`` — no headings, no bullet template, no required
    sections. 何もなかった日は短くてよい, so there is no minimum length either:
    a day where nothing happened should produce a short entry, and a schema
    that demanded three paragraphs would produce three paragraphs of invention.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    content: str = Field(default="", max_length=4000)
    #: One line for listings and for the memory context. Not a section header.
    summary: str = Field(default="", max_length=200)
    #: How the day felt, in her own reckoning. Used for the entry's mood
    #: fields; it is not a state change and never becomes one directly.
    felt: Literal["good", "quiet", "hard", "mixed"] = "quiet"
    #: What she found herself writing about, so 26.7's practice can be applied
    #: to those memories and only those.
    about_memory_ids: tuple[str, ...] = ()


#: Spec 26.5's mood mapping, kept out of the prompt so it is versionable.
FELT_TO_MOOD: dict[str, tuple[float, float]] = {
    "good": (0.55, 0.45),
    "quiet": (0.10, 0.20),
    "hard": (-0.45, 0.50),
    "mixed": (0.00, 0.45),
}


__all__ = [
    "FELT_TO_MOOD",
    "DiaryDraft",
    "DiaryEntry",
    "DiaryReference",
    "DiaryStatus",
    "LifeDay",
    "ReferenceType",
]
