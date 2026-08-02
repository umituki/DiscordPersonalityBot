"""Conversation event payloads (spec 8.2).

These are objective records of what was actually said. A reply that the Output
Guard rejected is *not* an utterance: it is recorded as a suppression, and its
text never enters the event stream that memory will later read (spec 28.5).
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

USER_MESSAGE_RECEIVED = "USER_MESSAGE_RECEIVED"
YUI_MESSAGE_SENT = "YUI_MESSAGE_SENT"
YUI_REPLY_SUPPRESSED = "YUI_REPLY_SUPPRESSED"


@register_payload(USER_MESSAGE_RECEIVED)
class UserMessageReceivedPayload(EventPayload):
    """What the single USER actually sent (spec 1.2: never an NPC)."""

    text: str
    channel_id: str
    message_id: str
    author_id: str
    is_direct_message: bool = False
    attachment_count: int = 0
    reply_to_message_id: str | None = None


@register_payload(YUI_MESSAGE_SENT)
class YuiMessageSentPayload(EventPayload):
    """Recorded only after the send succeeded — never a plan (spec 2.15)."""

    text: str
    channel_id: str
    message_id: str
    in_reply_to_event_id: str | None = None
    generation_call_id: str | None = None
    generation_attempts: int = 1


@register_payload(YUI_REPLY_SUPPRESSED)
class YuiReplySuppressedPayload(EventPayload):
    """A reply that was generated but never sent.

    The rejected text is deliberately absent: an unsent hallucination must not
    be stored where memory or beliefs can pick it up (spec 28.5). Operators can
    still find the text in the ``failures`` record for the same run.
    """

    reason_code: str
    stage: str
    detail: str
    in_reply_to_event_id: str | None = None
    attempts: int = 0
