"""Conversation domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

Speaker = Literal["user", "yui"]
ChannelType = Literal["direct_message", "guild_text", "test"]


class ConversationTurn(BaseModel):
    """One recorded utterance.

    This is a projection of the event stream, kept for cheap recent-history
    reads. The events remain authoritative (spec 2.9) and the projection can be
    rebuilt from them.
    """

    model_config = ConfigDict(frozen=True)

    turn_id: str
    conversation_id: str
    event_id: str
    speaker: Speaker
    author_id: str | None
    content: str
    occurred_at: datetime
    message_ref: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    def as_prompt_line(self, user_label: str = "USER", yui_label: str = "YUI") -> str:
        label = user_label if self.speaker == "user" else yui_label
        return f"{label}: {self.content}"


class Conversation(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversation_id: str
    channel_id: str
    channel_type: ChannelType
    started_at: datetime
    last_activity_at: datetime
    turn_count: int = Field(default=0, ge=0)
    status: Literal["active", "idle", "closed"] = "active"

    @field_validator("started_at", "last_activity_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)


class ReplyDraft(BaseModel):
    """The sentence itself (Phase 3 §35).

    The output schema is deliberately one field. The realizer is not asked to
    restate the social interpretation or the surface plan: those were decided
    before it ran, and asking a model to echo a decision back is how the echo
    quietly becomes the decision.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
