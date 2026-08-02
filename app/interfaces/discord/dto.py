"""Transport-neutral message objects.

Nothing above this module sees a ``discord.Message``. Keeping the boundary
narrow means the conversation path is testable without a gateway connection.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware
from app.conversation.models import ChannelType


class InboundMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    channel_id: str
    channel_type: ChannelType
    author_id: str
    author_is_bot: bool = False
    text: str = ""
    created_at: datetime
    attachment_count: int = Field(default=0, ge=0)
    reply_to_message_id: str | None = None
    guild_id: str | None = None

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @property
    def is_direct_message(self) -> bool:
        return self.channel_type == "direct_message"


class OutboundMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str
    text: str
    in_reply_to_message_id: str | None = None
