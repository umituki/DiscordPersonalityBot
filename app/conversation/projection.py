"""Rebuilding the conversation projection from events.

The projection in ``conversation_turns`` exists for cheap recent-history reads.
The events are the authority (spec 2.9), so the projection must be derivable
from them at any time. This module is what makes that claim testable rather
than aspirational: it drops the projection and replays the message events.

It only ever rewrites the projection. Events are never touched.
"""

from __future__ import annotations

import logging

from app.conversation.events import USER_MESSAGE_RECEIVED, YUI_MESSAGE_SENT
from app.conversation.models import ChannelType
from app.events.store import EventStore
from app.storage.database import Database
from app.storage.repositories.conversations import ConversationRepository

logger = logging.getLogger(__name__)

MESSAGE_EVENT_TYPES = (USER_MESSAGE_RECEIVED, YUI_MESSAGE_SENT)


class ConversationProjector:
    def __init__(
        self,
        db: Database,
        events: EventStore,
        conversations: ConversationRepository,
    ) -> None:
        self._db = db
        self._events = events
        self._conversations = conversations

    def rebuild(self, *, channel_type: ChannelType = "direct_message") -> int:
        """Rebuild every conversation turn from the event store."""
        message_events = self._events.by_types(MESSAGE_EVENT_TYPES)
        with self._db.transaction():
            self._conversations.clear_projection()
            replayed = 0
            for event in message_events:
                payload = event.payload
                channel_id = getattr(payload, "channel_id", None)
                if channel_id is None:
                    continue
                conversation = self._conversations.ensure_conversation(
                    channel_id=str(channel_id),
                    channel_type=channel_type,
                    now=event.occurred_at,
                )
                speaker = "user" if event.event_type == USER_MESSAGE_RECEIVED else "yui"
                if self._conversations.record_turn(
                    conversation_id=conversation.conversation_id,
                    event_id=event.event_id,
                    speaker=speaker,
                    author_id=event.actor_id if speaker == "user" else None,
                    content=getattr(payload, "text", ""),
                    occurred_at=event.occurred_at,
                    message_ref=getattr(payload, "message_id", None),
                ):
                    replayed += 1

        logger.info("conversation projection rebuilt turns=%d", replayed)
        return replayed
