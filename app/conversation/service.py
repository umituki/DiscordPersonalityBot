"""Conversation application service (spec 35 Phase 3).

Order of work for one incoming message::

    admit (USER? admin? empty?)
    → USER_MESSAGE_RECEIVED event → processor run (persist, snapshot, commit)
    → project the user's turn
    → generate a reply from identity + recent turns, reading the run's S0
    → Output Guard
    → hand the text to the interface to send
    → after the send succeeds: YUI_MESSAGE_SENT event → processor run

Two boundaries matter here:

* The service never sends anything itself. Sending is the interface's job, and
  ``YUI_MESSAGE_SENT`` is only recorded once a send actually happened, so a
  plan is never stored as a completed experience (spec 2.15).
* The service never writes state. The processor commits; the conversation
  projection is a rebuildable read model over the events (spec 2.9).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.clock import Clock, SystemClock
from app.conversation.engine import ConversationEngine, ReplyGeneration
from app.conversation.events import (
    USER_MESSAGE_RECEIVED,
    YUI_MESSAGE_SENT,
    YUI_REPLY_SUPPRESSED,
    UserMessageReceivedPayload,
    YuiMessageSentPayload,
    YuiReplySuppressedPayload,
)
from app.conversation.policy import ConversationPolicy
from app.events.model import Event
from app.interfaces.discord.adapter import DiscordMessageAdapter, IgnoreReason
from app.memory.engine import MemoryEngine
from app.interfaces.discord.dto import InboundMessage, OutboundMessage
from app.orchestrator.processor import EventProcessor, ProcessingOutcome
from app.storage.repositories.conversations import ConversationRepository
from app.storage.repositories.failures import FailureRecord, FailureRepository

logger = logging.getLogger(__name__)

COMPONENT = "conversation_service"


@dataclass(frozen=True, slots=True)
class ConversationResult:
    """What the interface should do next."""

    accepted: bool
    ignored_reason: IgnoreReason | None = None
    event: Event | None = None
    outcome: ProcessingOutcome | None = None
    generation: ReplyGeneration | None = None
    outbound: OutboundMessage | None = None
    suppressed: bool = False

    @property
    def should_send(self) -> bool:
        return self.outbound is not None


class ConversationService:
    def __init__(
        self,
        *,
        processor: EventProcessor,
        engine: ConversationEngine,
        conversations: ConversationRepository,
        adapter: DiscordMessageAdapter,
        failures: FailureRepository,
        policy: ConversationPolicy,
        memory: MemoryEngine | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._processor = processor
        self._engine = engine
        self._conversations = conversations
        self._adapter = adapter
        self._failures = failures
        self._policy = policy
        self._memory = memory
        self._clock = clock or SystemClock()

    # --- inbound -----------------------------------------------------------
    async def handle_inbound(self, message: InboundMessage) -> ConversationResult:
        decision = self._adapter.admit(message)
        if not decision.accepted or decision.event is None:
            return ConversationResult(accepted=False, ignored_reason=decision.reason)

        event = decision.event
        outcome = await self._processor.process(event)

        conversation = await asyncio.to_thread(
            self._conversations.ensure_conversation,
            channel_id=message.channel_id,
            channel_type=message.channel_type,
            now=event.occurred_at,
        )
        await asyncio.to_thread(
            self._conversations.record_turn,
            conversation_id=conversation.conversation_id,
            event_id=event.event_id,
            speaker="user",
            author_id=str(message.author_id),
            content=event.payload.text,
            occurred_at=event.occurred_at,
            message_ref=str(message.message_id),
        )
        recent = await asyncio.to_thread(
            self._conversations.recent_turns,
            conversation.conversation_id,
            limit=self._policy.context.recent_turn_limit,
            exclude_event_id=event.event_id,
        )

        memories = ()
        if self._memory is not None:
            await asyncio.to_thread(
                self._memory.observe, event, conversation_id=conversation.conversation_id
            )
            # Recall reads subjective memory only — never the archive (spec 10.1).
            memories = await asyncio.to_thread(
                self._memory.recall,
                event.payload.text,
                now=event.occurred_at,
                run_id=outcome.run.run_id,
                event_id=event.event_id,
            )

        generation = await self._engine.draft_reply(
            user_text=event.payload.text,
            recent_turns=recent,
            memories=memories,
            snapshot=outcome.snapshot,
            run_id=outcome.run.run_id,
            event_id=event.event_id,
        )

        if not generation.accepted or generation.text is None:
            await self._suppress(event, generation, outcome)
            return ConversationResult(
                accepted=True,
                event=event,
                outcome=outcome,
                generation=generation,
                suppressed=True,
            )

        return ConversationResult(
            accepted=True,
            event=event,
            outcome=outcome,
            generation=generation,
            outbound=OutboundMessage(
                channel_id=message.channel_id,
                text=generation.text,
                in_reply_to_message_id=str(message.message_id),
            ),
        )

    # --- outbound ----------------------------------------------------------
    async def confirm_sent(
        self,
        result: ConversationResult,
        *,
        message_id: str,
        channel_type: str = "direct_message",
    ) -> Event:
        """Record what was actually delivered (spec 2.15)."""
        if result.event is None or result.outbound is None:
            raise ValueError("confirm_sent requires a result that produced an outbound message")

        generation = result.generation
        sent = result.event.child(
            event_type=YUI_MESSAGE_SENT,
            category="social",
            actor_type="yui",
            source_type="discord_message",
            source_id=str(message_id),
            target_type="user",
            target_id=result.event.actor_id,
            priority="P1",
            clock=self._clock,
            payload=YuiMessageSentPayload(
                text=result.outbound.text,
                channel_id=result.outbound.channel_id,
                message_id=str(message_id),
                in_reply_to_event_id=result.event.event_id,
                generation_call_id=(
                    generation.outcome.call_ids[-1]
                    if generation and generation.outcome.call_ids
                    else None
                ),
                generation_attempts=generation.outcome.attempts if generation else 1,
            ),
        )
        await self._processor.process(sent)

        conversation = await asyncio.to_thread(
            self._conversations.ensure_conversation,
            channel_id=result.outbound.channel_id,
            channel_type=channel_type,
            now=sent.occurred_at,
        )
        await asyncio.to_thread(
            self._conversations.record_turn,
            conversation_id=conversation.conversation_id,
            event_id=sent.event_id,
            speaker="yui",
            author_id=None,
            content=result.outbound.text,
            occurred_at=sent.occurred_at,
            message_ref=str(message_id),
        )

        if self._memory is not None:
            await asyncio.to_thread(
                self._memory.observe, sent, conversation_id=conversation.conversation_id
            )
            await self.run_memory_maintenance()
        return sent

    async def run_memory_maintenance(self) -> None:
        """Close finished episodes and encode them.

        This runs after the reply has been delivered, so a slow summarisation
        never makes the USER wait (spec 28.3). The scheduler takes it over in
        the virtual-life phase.
        """
        if self._memory is None:
            return
        try:
            await asyncio.to_thread(self._memory.close_due_episodes)
            await self._memory.encode_pending()
            await asyncio.to_thread(self._memory.apply_forgetting)
        except Exception as exc:  # noqa: BLE001 - background work must not break a reply
            logger.exception("memory maintenance failed")
            await asyncio.to_thread(
                self._failures.record,
                FailureRecord(
                    failure_type="behavior",
                    component=COMPONENT,
                    reason_code="memory_maintenance_failed",
                    severity="warning",
                    detail={"error": repr(exc)[:1000]},
                ),
                now=self._clock.now(),
            )

    async def record_send_failure(self, result: ConversationResult, error: str) -> None:
        """A reply that could not be delivered is not an utterance."""
        if result.event is None:
            return
        await asyncio.to_thread(
            self._failures.record,
            FailureRecord(
                failure_type="transport",
                component=COMPONENT,
                reason_code="discord_send_failed",
                severity="error",
                run_id=result.outcome.run.run_id if result.outcome else None,
                event_id=result.event.event_id,
                detail={"error": error[:1000]},
            ),
            now=self._clock.now(),
        )

    # --- suppression -------------------------------------------------------
    async def _suppress(
        self,
        event: Event,
        generation: ReplyGeneration,
        outcome: ProcessingOutcome,
    ) -> Event:
        failure = generation.outcome.failure
        reason_code = failure.reason_code if failure else "generation_failed"
        stage = failure.stage.label if failure else "unknown"
        detail = failure.detail if failure else "no candidate was produced"

        logger.warning(
            "reply suppressed event_id=%s stage=%s reason=%s", event.event_id, stage, reason_code
        )

        # The rejected text stays out of the event stream that memory reads
        # (spec 28.5). Operators can still see it in the failure record.
        rejected_text = (
            generation.outcome.response.text[:500]
            if generation.outcome.response is not None
            else ""
        )
        await asyncio.to_thread(
            self._failures.record,
            FailureRecord(
                failure_type="behavior",
                component=COMPONENT,
                reason_code=reason_code,
                severity="warning",
                run_id=outcome.run.run_id,
                event_id=event.event_id,
                detail={
                    "stage": stage,
                    "detail": detail,
                    "attempts": generation.outcome.attempts,
                    # Operations only. Never read back into memory or belief.
                    "unsent_candidate": rejected_text,
                },
            ),
            now=self._clock.now(),
        )

        suppressed = event.child(
            event_type=YUI_REPLY_SUPPRESSED,
            category="internal",
            actor_type="system",
            source_type="output_guard",
            priority="P2",
            clock=self._clock,
            payload=YuiReplySuppressedPayload(
                reason_code=reason_code,
                stage=stage,
                detail=detail[:500],
                in_reply_to_event_id=event.event_id,
                attempts=generation.outcome.attempts,
            ),
        )
        await self._processor.process(suppressed)
        return suppressed


__all__ = [
    "ConversationResult",
    "ConversationService",
    "USER_MESSAGE_RECEIVED",
    "UserMessageReceivedPayload",
]
