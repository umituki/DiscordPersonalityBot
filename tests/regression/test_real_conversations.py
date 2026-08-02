"""The two real conversations from 2026-08-02 (patch spec 21, 26, 27).

These are not invented examples. Both exchanges below were produced by the
running system on the owner's machine, and Patch C's definition of done is
``今回の実会話2例がregression合格``.

Case A::

    USER: 初めまして～
    YUI:  初めまして、はじめまして。          ← must never be sent

Case B::

    YUI:  どうしましたか？
    USER: いや、特に用はないんだ。ゆいのことを知ったから、話したくて
    YUI:  どうしたかった？                    ← must never be sent

The tests drive the whole service, not the guard alone: the model is scripted
to produce exactly the bad reply that was produced on hardware, and what is
asserted is that it does not reach the USER.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.conversation.engine import ConversationEngine
from app.conversation.service import ConversationService
from app.conversation.quality import QualityIssue
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMResponse
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

pytestmark = pytest.mark.regression

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

#: A decision that does not call for a question. Both real failures asked one
#: anyway (patch spec 8.1, prohibition 9).
NO_QUESTION_ACTS = (
    '{"acknowledge": true, "self_disclose": true, "goal": "maintain_connection", '
    '"mode": "smalltalk", "question_need": "none", "reciprocity": "high"}'
)

#: The decision that produced 「どうしましたか？」 in the real run: an errand was
#: assumed, so a question was needed.
ASKING_ACTS = (
    '{"acknowledge": true, "ask_followup": true, "goal": "understand_user", '
    '"mode": "task", "question_need": "needed"}'
)


class SequencedClient:
    """Scripts the dialogue decision and the reply text independently.

    ``ScriptedClient`` answers every dialogue-act call with one fixed decision,
    which cannot express these two cases: what is being reproduced is a *turn
    sequence* where the decision changes between turns.
    """

    model = "test-model"

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
        return LLMResponse(text=queue.pop(0), model=self.model, created_at=NOW, latency_ms=5)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def real_conversation(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy, clock,  # noqa: F811
):
    def build(*, acts: list[str], replies: list[str]):
        client = SequencedClient(acts=acts, replies=replies)
        generator = StructuredGenerator(
            client, prompts=prompt_registry, clock=clock, max_attempts=1
        )
        engine = ConversationEngine(
            identity=identity,
            prompts=prompt_registry,
            structured=generator,
            guard=guard,
            policy=conversation_policy,
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
        service = ConversationService(
            processor=processor,
            engine=engine,
            event_store=event_store,
            conversations=conversations,
            adapter=adapter,
            failures=failures,
            policy=conversation_policy,
            clock=clock,
        )
        return service, client

    return build


# --- Case A ------------------------------------------------------------------
async def test_case_a_the_doubled_greeting_is_never_sent(real_conversation, clock) -> None:
    service, client = real_conversation(
        acts=[NO_QUESTION_ACTS],
        replies=[
            '{"text": "初めまして、はじめまして。"}',   # what hardware produced
            '{"text": "はじめまして。話せてうれしい。"}',  # the rewrite
        ],
    )

    result = await service.handle_inbound(inbound(clock, text="初めまして～"))

    assert result.should_send
    assert result.outbound.text != "初めまして、はじめまして。"
    assert result.outbound.text == "はじめまして。話せてうれしい。"
    assert result.generation.repaired is True
    # The rewrite is a repair call, not a second draft (patch spec 10).
    assert client.purposes.count("conversation_repair") == 1


async def test_case_a_is_rejected_even_if_the_rewrite_also_fails(
    real_conversation, failures, clock
) -> None:
    """One rewrite is the budget. A second bad draft is silence, not a send."""
    service, client = real_conversation(
        acts=[NO_QUESTION_ACTS],
        replies=[
            '{"text": "初めまして、はじめまして。"}',
            '{"text": "はじめまして、はじめまして！"}',
        ],
    )

    result = await service.handle_inbound(inbound(clock, text="初めまして～"))

    assert result.suppressed
    assert result.should_send is False
    assert client.purposes.count("conversation_repair") == 1
    assert any(
        row["reason_code"] == QualityIssue.DUPLICATE_GREETING for row in failures.recent()
    )


# --- Case B ------------------------------------------------------------------
async def test_case_b_the_errand_is_not_demanded_again(real_conversation, clock) -> None:
    """Patch spec 21 Case B and 27 Scenario 2.

    The USER has just said they have no errand. Asking 「どうしたかった？」 asks
    for one anyway, and it is the exact reply the real run sent.
    """
    service, client = real_conversation(
        acts=[ASKING_ACTS, NO_QUESTION_ACTS],
        replies=[
            '{"text": "どうしましたか？"}',
            '{"text": "どうしたかった？"}',            # what hardware produced
            '{"text": "そっか。うれしいな、そう言ってもらえて。"}',  # the rewrite
        ],
    )

    first = await service.handle_inbound(inbound(clock, text="ねえ", message_id="1"))
    assert first.outbound.text == "どうしましたか？"
    await service.confirm_sent(first, message_id="10")

    clock.advance(seconds=5)
    second = await service.handle_inbound(
        inbound(
            clock,
            text="いや、特に用はないんだ。ゆいのことを知ったから、話したくて",
            message_id="2",
        )
    )

    assert second.should_send
    assert second.outbound.text != "どうしたかった？"
    assert "?" not in second.outbound.text and "？" not in second.outbound.text
    assert second.generation.repaired is True
    await service.drain_background()


async def test_case_b_the_previous_reply_is_in_the_second_turns_context(
    real_conversation, clock
) -> None:
    """Patch spec 27 Scenario 2: ``Context MUST contain直前YUI reply``."""
    service, _ = real_conversation(
        acts=[ASKING_ACTS, NO_QUESTION_ACTS],
        replies=[
            '{"text": "どうしましたか？"}',
            '{"text": "そっか。話しかけてくれてうれしい。"}',
        ],
    )

    first = await service.handle_inbound(inbound(clock, text="ねえ", message_id="1"))
    await service.confirm_sent(first, message_id="10")

    clock.advance(seconds=5)
    second = await service.handle_inbound(
        inbound(
            clock,
            text="いや、特に用はないんだ。ゆいのことを知ったから、話したくて",
            message_id="2",
        )
    )

    history = second.generation.context.get("recent_conversation").content
    assert "どうしましたか？" in history
    await service.drain_background()


async def test_a_smalltalk_turn_does_not_have_to_ask_anything(real_conversation, clock) -> None:
    """``用件のない雑談を用件処理へ戻さない`` (patch spec 26).

    A reply that answers without a question is accepted as-is: the guard exists
    to stop the errand loop, not to force one particular shape of reply.
    """
    service, client = real_conversation(
        acts=[NO_QUESTION_ACTS],
        replies=['{"text": "そっか。話したいと思ってくれたの、うれしい。"}'],
    )

    result = await service.handle_inbound(
        inbound(clock, text="いや、特に用はないんだ。ゆいのことを知ったから、話したくて")
    )

    assert result.should_send
    assert result.generation.repaired is False
    assert "conversation_repair" not in client.purposes
