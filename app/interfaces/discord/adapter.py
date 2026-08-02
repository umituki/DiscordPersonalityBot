"""Inbound message admission and event construction.

Two separations are enforced right here, at the door:

* **USER vs everyone else** (spec 1.2, 2.10). This is a single-user bot. A
  message from anyone but the configured owner is not YUI's USER and never
  becomes a social event.
* **Normal conversation vs Admin Control Plane** (spec 2.18, 30). An admin
  command is not a thing YUI hears; it is refused here because the admin plane
  does not exist yet (Phase 12).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from app.clock import Clock, SystemClock
from app.conversation.events import USER_MESSAGE_RECEIVED, UserMessageReceivedPayload
from app.events.model import Event
from app.interfaces.discord.dto import InboundMessage

logger = logging.getLogger(__name__)

#: Prefix reserved for the Admin Control Plane (spec 30).
ADMIN_PREFIX = "!yui"


class IgnoreReason(str, Enum):
    BOT_AUTHOR = "bot_author"
    NOT_THE_USER = "not_the_user"
    CHANNEL_NOT_ALLOWED = "channel_not_allowed"
    EMPTY_MESSAGE = "empty_message"
    ADMIN_PLANE_UNAVAILABLE = "admin_plane_unavailable"


@dataclass(frozen=True, slots=True)
class InboundDecision:
    accepted: bool
    event: Event | None = None
    reason: IgnoreReason | None = None

    @property
    def ignored(self) -> bool:
        return not self.accepted


class DiscordMessageAdapter:
    def __init__(
        self,
        *,
        owner_user_id: str,
        allowed_channel_ids: frozenset[str] = frozenset(),
        clock: Clock | None = None,
    ) -> None:
        if not owner_user_id:
            raise ValueError("the single USER must be identified by owner_user_id")
        self._owner_user_id = str(owner_user_id)
        self._allowed_channel_ids = frozenset(str(item) for item in allowed_channel_ids)
        self._clock = clock or SystemClock()

    @property
    def owner_user_id(self) -> str:
        return self._owner_user_id

    def admit(self, message: InboundMessage) -> InboundDecision:
        """Decide whether a message is USER conversation, and build its event."""
        if message.author_is_bot:
            return self._ignore(message, IgnoreReason.BOT_AUTHOR)
        if str(message.author_id) != self._owner_user_id:
            # Spec 1.2 / 2.10: only one real USER exists. Anyone else is not an
            # NPC either — they are simply not part of YUI's world.
            return self._ignore(message, IgnoreReason.NOT_THE_USER)
        if self._allowed_channel_ids and str(message.channel_id) not in self._allowed_channel_ids:
            return self._ignore(message, IgnoreReason.CHANNEL_NOT_ALLOWED)

        text = message.text.strip()
        if text.startswith(ADMIN_PREFIX):
            # Never answer an admin command in character (spec 2.18, 30).
            return self._ignore(message, IgnoreReason.ADMIN_PLANE_UNAVAILABLE)
        if not text and message.attachment_count == 0:
            return self._ignore(message, IgnoreReason.EMPTY_MESSAGE)

        event = Event.create(
            event_type=USER_MESSAGE_RECEIVED,
            category="social",
            actor_type="user",
            actor_id=str(message.author_id),
            target_type="yui",
            source_type="discord_message",
            source_id=str(message.message_id),
            origin="real_discord",
            priority="P0",
            occurred_at=message.created_at,
            clock=self._clock,
            payload=UserMessageReceivedPayload(
                text=text,
                channel_id=str(message.channel_id),
                message_id=str(message.message_id),
                author_id=str(message.author_id),
                is_direct_message=message.is_direct_message,
                attachment_count=message.attachment_count,
                reply_to_message_id=message.reply_to_message_id,
            ),
        )
        return InboundDecision(accepted=True, event=event)

    def _ignore(self, message: InboundMessage, reason: IgnoreReason) -> InboundDecision:
        logger.info(
            "inbound message ignored reason=%s channel=%s author=%s",
            reason.value,
            message.channel_id,
            message.author_id,
        )
        return InboundDecision(accepted=False, reason=reason)
