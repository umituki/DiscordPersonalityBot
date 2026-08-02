"""INVARIANT: an unsupported claim gets one rewrite, then silence
(rebuild spec GROUND-002, GROUND-003, GROUND-004).

The service is the real one — real processor, real event store, real memory
engine, real conversation projection. Only the model is scripted, and it is
scripted to produce exactly the sentence that has no basis: 「今日は本を読んだ」
on a day with no completed Activity.

What is asserted is not that the guard returned False. It is that the sentence
never reached the USER, never became a conversation turn, and never became a
memory — because the way this fails in production is that it *is* sent, and
three days later it is a remembered afternoon.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.conversation.engine import ConversationEngine
from app.conversation.service import ConversationService
from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
from app.grounding.context import GroundingContextBuilder
from app.grounding.policy import GroundingPolicy
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMResponse
from app.memory.engine import MemoryEngine
from app.memory.material import ConversationEpisodeSource
from app.orchestrator.processor import EventProcessor
from tests.unit.test_conversation import (  # noqa: F401 - shared fixtures
    CHANNEL,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    inbound,
)
from tests.unit.test_memory import memories, memory_policy  # noqa: F401

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

ACTS = (
    '{"acknowledge": true, "self_disclose": true, "goal": "maintain_connection", '
    '"mode": "smalltalk", "question_need": "none", "reciprocity": "high"}'
)

#: The fabrication. No Activity was ever completed; no Event says so.
FABRICATED = '{"text": "今日は本を読んだよ。"}'
#: A rewrite that stops asserting it.
HONEST = '{"text": "そっか。いま何してるの、って聞きたくなるね。"}'
#: A rewrite that asserts it again.
STILL_FABRICATED = '{"text": "さっき本を読んだところ。"}'


class SequencedClient:
    def __init__(self, *, acts: list[str], replies: list[str]) -> None:
        self._acts = list(acts)
        self._replies = list(replies)
        self.purposes: list[str] = []

    async def generate(self, request):
        schema = request.format_schema or {}
        queue = self._acts if schema.get("title") == "DialogueAct" else self._replies
        self.purposes.append(request.purpose)
        if not queue:
            raise AssertionError(f"unscripted call: {request.purpose}")
        return LLMResponse(text=queue.pop(0), model="test-model", created_at=NOW, latency_ms=5)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def grounded_conversation(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy,  # noqa: F811
    memories, memory_policy, clock,  # noqa: F811
):
    """The real service, with the grounding guard wired as bootstrap wires it."""

    def build(*, replies: list[str]):
        client = SequencedClient(acts=[ACTS] * 4, replies=replies)
        generator = StructuredGenerator(
            client, prompts=prompt_registry, clock=clock, max_attempts=1
        )
        policy = GroundingPolicy.load(REPO_ROOT / "config" / "policies" / "grounding.yaml")
        engine = ConversationEngine(
            identity=identity,
            prompts=prompt_registry,
            structured=generator,
            guard=guard,
            policy=conversation_policy,
            grounding=ClaimGroundingGuard(ClaimExtractor(policy)),
            clock=clock,
        )
        processor = EventProcessor(
            db=db,
            event_store=event_store,
            dispatcher=dispatcher,
            snapshots=snapshots,
            arbitrator=arbitrator,
            committer=committer,
            runs=runs,
            failures=failures,
            mode="test",
            clock=clock,
        )
        memory = MemoryEngine(
            repository=memories,
            policy=memory_policy,
            structured=generator,
            prompts=prompt_registry,
            material=ConversationEpisodeSource(conversations, yui_label=identity.name),
            clock=clock,
        )
        service = ConversationService(
            processor=processor,
            engine=engine,
            event_store=event_store,
            conversations=conversations,
            adapter=adapter,
            failures=failures,
            policy=conversation_policy,
            memory=memory,
            # There is no completed Activity, no goal, no tool call: the whole
            # point is that the context is honestly empty.
            grounding=GroundingContextBuilder(events=event_store, clock=clock),
            clock=clock,
        )
        return service, client

    return build


async def test_an_unsupported_claim_is_rewritten_once(
    grounded_conversation, clock
) -> None:
    """GROUND-002: ``Unsupported claim があれば 1 回のみ repair``."""
    service, client = grounded_conversation(replies=[FABRICATED, HONEST])

    result = await service.handle_inbound(inbound(clock, text="なにしてたの？"))

    assert result.should_send
    assert result.outbound.text != "今日は本を読んだよ。"
    assert result.generation.repaired is True
    assert client.purposes.count("conversation_repair") == 1


async def test_a_failed_repair_suppresses_the_send(
    grounded_conversation, failures, clock
) -> None:
    """GROUND-003: ``repair 失敗なら送信 suppress``.

    One rewrite is the budget, and it is spent. Saying nothing is the correct
    outcome — the alternative is asserting an afternoon that did not happen.
    """
    service, client = grounded_conversation(replies=[FABRICATED, STILL_FABRICATED])

    result = await service.handle_inbound(inbound(clock, text="なにしてたの？"))

    assert result.suppressed
    assert result.should_send is False
    assert client.purposes.count("conversation_repair") == 1
    assert any(
        row["reason_code"] == "unsupported_yui_completed_action"
        for row in failures.recent()
    )


async def test_a_suppressed_draft_leaves_no_trace(
    grounded_conversation, conversations, event_store, memories, clock  # noqa: F811
) -> None:
    """GROUND-004: ``未送信 draft を Memory に encode しない``.

    The strongest form of the rule: the text is not in the turns, not in the
    event stream, and not in any episode. What she never said cannot become
    something she remembers doing.
    """
    service, _ = grounded_conversation(replies=[FABRICATED, STILL_FABRICATED])

    result = await service.handle_inbound(inbound(clock, text="なにしてたの？"))
    await service.drain_background()
    assert result.suppressed

    conversation = conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    turns = conversations.recent_turns(conversation.conversation_id, limit=50)
    assert all("本を読んだ" not in turn.content for turn in turns)
    assert all(turn.speaker == "user" for turn in turns)

    texts = [
        str(getattr(event.payload, "text", "") or "")
        for event in event_store.recent(limit=50)
    ]
    assert all("本を読んだ" not in text for text in texts)

    encoded = memories.all_memories(limit=100)
    assert all("本を読んだ" not in memory.summary for memory in encoded)


async def test_a_grounded_claim_is_sent_unchanged(
    grounded_conversation, clock
) -> None:
    """The guard is a gate, not a ban on saying anything.

    With no claim in the sentence there is nothing to resolve, and the reply
    goes out on the first draft.
    """
    service, client = grounded_conversation(replies=[HONEST])

    result = await service.handle_inbound(inbound(clock, text="なにしてたの？"))

    assert result.should_send
    assert result.generation.repaired is False
    assert "conversation_repair" not in client.purposes
