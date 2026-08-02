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
from typing import Awaitable
from dataclasses import dataclass

from app.clock import Clock, SystemClock
from app.conversation.common_ground import CommonGroundTracker, CorrectionOutcome
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
from app.grounding.context import GroundingContextBuilder
from app.observability.trace import ConversationTrace, ConversationTracer
from app.interfaces.discord.adapter import DiscordMessageAdapter, IgnoreReason
from app.memory.engine import MemoryEngine
from app.events.store import EventStore
from app.psychology.appraisal import AppraisalEngine
from app.tools.manager import ToolManager
from app.interfaces.discord.dto import InboundMessage, OutboundMessage
from app.orchestrator.processor import EventProcessor, ProcessingOutcome
from app.storage.repositories.conversations import ConversationRepository
from app.storage.repositories.failures import FailureRecord, FailureRepository

logger = logging.getLogger(__name__)


def _log_background_result(task: "asyncio.Task[None]") -> None:
    """Post-send work failing must be visible, never silent (patch spec 5.4)."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("post-send background work failed: %r", error)

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
    #: Patch spec 19.2. Carried back so the interface can add the marks only it
    #: knows — when typing started, when Discord actually took the message.
    trace: ConversationTrace | None = None
    #: Rebuild spec 11. Carried so that, once the send is confirmed, the claims
    #: the reply made are entered into the common ground against the same
    #: evidence they were checked against — not against a context rebuilt
    #: later, which would have moved on.
    grounding_context: object | None = None
    #: Phase 2 §2J. Carried so Stage 4 can mark, after delivery, which recalled
    #: memories the sent reply actually rests on. A memory that sat in context
    #: and left no mark on the sentence was available, not used.
    retrieval: object | None = None

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
        event_store: EventStore,
        appraisal: AppraisalEngine | None = None,
        tools: ToolManager | None = None,
        grounding: GroundingContextBuilder | None = None,
        common_ground: CommonGroundTracker | None = None,
        tracer: ConversationTracer | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._processor = processor
        self._engine = engine
        self._conversations = conversations
        self._adapter = adapter
        self._failures = failures
        self._policy = policy
        self._memory = memory
        self._appraisal = appraisal
        #: Patch spec 5.1-5.2: turns are persisted and projected before any
        #: expensive work, which needs a direct append rather than waiting for
        #: a full processing run. Required, so no caller can end up with the
        #: ordering that lost YUI's previous reply from the next turn.
        self._events = event_store
        #: In-flight post-send work (patch spec 5.4).
        self._background: set[asyncio.Task[None]] = set()
        self._tools = tools
        #: Rebuild spec 15.1: what is actually known, assembled before the
        #: reply exists so nothing the model writes can end up in it.
        self._grounding = grounding
        #: Rebuild spec 11: what this conversation is treating as true, and
        #: what happens when the USER says it is not.
        self._common_ground = common_ground
        #: Patch spec 19.2: where the USER's wait went, stage by stage. Optional
        #: because observability must never be a precondition for answering.
        self._tracer = tracer
        self._clock = clock or SystemClock()

    # --- inbound -----------------------------------------------------------
    def intends_to_reply(self, message: InboundMessage) -> bool:
        """Whether this message will be answered, before any work is done.

        Patch spec 6.1: an accepted owner message counts as reply intent, and
        the typing indicator starts from that. When intentional silence exists
        it will be decided here too (6.2), which is why the interface asks the
        service rather than the adapter.
        """
        return self._adapter.rejection_reason(message) is None

    async def handle_inbound(
        self, message: InboundMessage, *, trace: ConversationTrace | None = None
    ) -> ConversationResult:
        # Patch spec 19.2. The interface may have started the trace already —
        # the typing indicator goes up before this call — so one is adopted
        # when offered and started here otherwise.
        trace = trace or self._start_trace(message)
        decision = self._adapter.admit(message)
        if not decision.accepted or decision.event is None:
            self._mark(trace, "admitted_at")
            self._finish(trace, outcome=f"ignored:{decision.reason.value if decision.reason else 'unknown'}")
            return ConversationResult(accepted=False, ignored_reason=decision.reason)

        event = decision.event
        self._mark(trace, "admitted_at")
        if trace is not None:
            trace.event_id = event.event_id
        conversation = await asyncio.to_thread(
            self._conversations.ensure_conversation,
            channel_id=message.channel_id,
            channel_type=message.channel_type,
            now=event.occurred_at,
        )
        # Recent history is read before the run so appraisal can use it: an
        # event is read in context, not in isolation (spec 11.1).
        recent = await asyncio.to_thread(
            self._conversations.recent_turns,
            conversation.conversation_id,
            limit=self._policy.context.recent_turn_limit,
        )
        if self._appraisal is not None:
            self._appraisal.set_recent_turns(recent)
            self._appraisal.set_trace(trace)

        # Patch spec 5.1: the USER message is persisted and projected before
        # the reply is generated, so a crash mid-reply leaves it recorded
        # rather than lost. Both writes are idempotent on the event id, and
        # the processor's own append below is then a no-op.
        await asyncio.to_thread(self._events.append, event)
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

        self._mark(trace, "state_commit_started_at")
        outcome = await self._processor.process(event)
        self._mark(trace, "state_commit_ended_at")
        if trace is not None:
            trace.run_id = outcome.run.run_id

        memories = ()
        retrieval = None
        if self._memory is not None:
            self._mark(trace, "memory_recall_started_at")
            await asyncio.to_thread(
                self._memory.observe, event, conversation_id=conversation.conversation_id
            )
            # Recall reads subjective memory only — never the archive
            # (spec 10.1, Phase 2 §2H). Recalling nothing is a normal result.
            retrieval = await self._memory.recall(
                event.payload.text,
                now=event.occurred_at,
                run_id=outcome.run.run_id,
                event_id=event.event_id,
            )
            memories = retrieval.selected
            self._mark(trace, "memory_recall_ended_at")

        # Spec 26: only the Tool Manager's record makes a tool claim sayable.
        tool_success_ids: tuple[str, ...] = ()
        if self._tools is not None:
            tool_success_ids = tuple(
                await asyncio.to_thread(
                    self._tools.successful_call_ids, run_id=outcome.run.run_id
                )
            )

        # Rebuild spec 15.1. Built here, from rows, before a single token of
        # the reply exists — which is what makes GROUND-001 structural rather
        # than a rule somebody has to remember.
        grounding_context = None
        if self._grounding is not None:
            grounding_context = await asyncio.to_thread(
                self._grounding.build,
                now=event.occurred_at,
                recalled=memories,
                run_id=outcome.run.run_id,
            )

        # Rebuild spec 11, CORR-001. Before writing anything, check whether the
        # USER just pushed back on something YUI herself claimed — and if she
        # cannot back it up now, take it back (CORR-002) rather than hand the
        # reply prompt an argument to make.
        correction = CorrectionOutcome()
        if self._common_ground is not None:
            correction = await asyncio.to_thread(
                self._common_ground.review_correction,
                event.payload.text,
                conversation_id=conversation.conversation_id,
                context=grounding_context,
                now=event.occurred_at,
            )

        self._mark(trace, "reply_started_at")
        generation = await self._engine.draft_reply(
            trace=trace,
            user_text=event.payload.text,
            recent_turns=recent,
            memories=memories,
            # Patch spec 9: she speaks as who she is *after* taking the message
            # in, so the reply reads the state this event produced.
            snapshot=outcome.post_commit_snapshot,
            run_id=outcome.run.run_id,
            event_id=event.event_id,
            tool_success_ids=tool_success_ids,
            grounding=grounding_context,
            common_ground=(
                self._common_ground.render(conversation.conversation_id)
                if self._common_ground is not None
                else ""
            ),
            correction=correction.render(),
        )
        self._mark(trace, "reply_ended_at")

        if not generation.accepted or generation.text is None:
            await self._suppress(event, generation, outcome)
            self._finish(trace, outcome="suppressed")
            return ConversationResult(
                accepted=True,
                event=event,
                outcome=outcome,
                generation=generation,
                suppressed=True,
                trace=trace,
                retrieval=retrieval,
            )

        return ConversationResult(
            accepted=True,
            event=event,
            outcome=outcome,
            generation=generation,
            trace=trace,
            grounding_context=grounding_context,
            retrieval=retrieval,
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
        # Patch spec 5.2. The objective event and the turn projection come
        # first, and nothing expensive is allowed between them. The 2026-08-02
        # run lost YUI's previous reply from the next USER turn's context
        # because a full processing run sat in this gap: if USER2 arrives now,
        # `recent_turns` must already contain YUI's reply.
        conversation = await asyncio.to_thread(
            self._conversations.ensure_conversation,
            channel_id=result.outbound.channel_id,
            channel_type=channel_type,
            now=sent.occurred_at,
        )
        await asyncio.to_thread(self._project_sent, sent, conversation, result, message_id)
        # Rebuild spec 11.1. Only now: a claim enters the common ground because
        # it was *said*, and a suppressed draft was never said (GROUND-004).
        if self._common_ground is not None:
            await asyncio.to_thread(
                self._common_ground.record_reply,
                result.outbound.text,
                conversation_id=conversation.conversation_id,
                event_id=sent.event_id,
                context=result.grounding_context,
                now=sent.occurred_at,
            )
        # 19.2: the moment the reply is actually visible in the next turn's
        # context, which is what the USER's wait was for.
        self._mark(result.trace, "outbound_projected_at")
        self._finish(result.trace, outcome="sent")

        # Phase 2 Stage 4 (§2J, §2K). The reply is out; now — and only now —
        # can it be said which recalled memories it actually rests on, and only
        # those practise. Being in context was availability, not use.
        if self._memory is not None and result.retrieval is not None:
            await asyncio.to_thread(
                self._memory.mark_used_in_reply,
                result.retrieval,
                result.outbound.text,
                now=sent.occurred_at,
            )

        # Only now the psychology of having spoken. This is a full run, and it
        # is deliberately behind the projection above.
        await self._processor.process(sent)

        # Patch spec 5.4 and prohibition 7: memory maintenance, reflection and
        # consolidation are background work at P3 or below. They must never sit
        # between a delivered reply and the next USER message.
        self.schedule_background(self._post_send_work(sent, conversation))
        return sent

    async def _post_send_work(self, sent: Event, conversation) -> None:
        if self._memory is None:
            return
        await asyncio.to_thread(
            self._memory.observe, sent, conversation_id=conversation.conversation_id
        )
        await self.run_memory_maintenance()

    # --- tracing (patch spec 19.2) -----------------------------------------
    def start_trace(self, message: InboundMessage) -> ConversationTrace | None:
        """Begin a trace before any work, for an interface that shows typing."""
        return self._start_trace(message)

    def _start_trace(self, message: InboundMessage) -> ConversationTrace | None:
        if self._tracer is None:
            return None
        return self._tracer.start(channel_id=str(message.channel_id))

    def _mark(self, trace: ConversationTrace | None, stage: str) -> None:
        if self._tracer is not None:
            self._tracer.mark(trace, stage)

    def _finish(self, trace: ConversationTrace | None, *, outcome: str) -> None:
        if self._tracer is not None:
            self._tracer.finish(trace, outcome=outcome)

    def finish_trace(self, trace: ConversationTrace | None, *, outcome: str) -> None:
        """Persist marks added by the gateway while the typing context exits."""
        self._finish(trace, outcome=outcome)

    # --- background work (patch spec 5.4) ----------------------------------
    def schedule_background(self, work: Awaitable[None]) -> None:
        """Run post-send work without making the next USER turn wait.

        Exceptions are logged rather than lost: a background failure degrades
        memory, and silence about it would be worse than the failure.
        """
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_background_result)

    async def drain_background(self) -> None:
        """Wait for scheduled post-send work. For shutdown and for tests."""
        while self._background:
            await asyncio.gather(*tuple(self._background), return_exceptions=True)

    def _project_sent(self, sent, conversation, result, message_id: str) -> None:
        """Idempotent projection of one delivered YUI turn (patch spec 5.2).

        The event row has to exist before the turn row can reference it, and
        both have to exist before the psychology run starts — otherwise a USER
        message arriving during that run sees a conversation without the reply
        it is answering.
        """
        self._events.append(sent)
        self._conversations.record_turn(
            conversation_id=conversation.conversation_id,
            event_id=sent.event_id,
            speaker="yui",
            author_id=None,
            content=result.outbound.text,
            occurred_at=sent.occurred_at,
            message_ref=str(message_id),
        )

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
        self._finish(result.trace, outcome="send_failed")
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
