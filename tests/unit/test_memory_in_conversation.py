"""Memory reaching the conversation (spec 27.1 conditional context, 35 Phase 4)."""

from __future__ import annotations

import pytest

from app.conversation.service import ConversationService
from app.llm.structured import StructuredGenerator
from app.memory.engine import MemoryEngine
from app.memory.material import ConversationEpisodeSource
from app.orchestrator.processor import EventProcessor
from tests.unit.test_conversation import (  # noqa: F401 - pytest fixtures are reused
    CHANNEL,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    inbound,
    make_engine,
)
from tests.unit.test_llm_structured import ScriptedClient
from tests.unit.test_memory import memories, memory_policy  # noqa: F401

SUMMARY = (
    '{"summary": "ユーザーが海に行った話をしてくれた。", "topics": ["海"], '
    '"novelty": 0.8, "felt_significance": 0.7}'
)


@pytest.fixture
def service_with_memory(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy,
    memories, memory_policy, clock,
):
    def build(script: list) -> tuple[ConversationService, MemoryEngine]:
        client = ScriptedClient(script)
        generator = StructuredGenerator(
            client, prompts=prompt_registry, clock=clock, max_attempts=1
        )
        memory_engine = MemoryEngine(
            repository=memories,
            policy=memory_policy,
            structured=generator,
            prompts=prompt_registry,
            material=ConversationEpisodeSource(conversations, yui_label=identity.name),
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
        from app.conversation.engine import ConversationEngine

        engine = ConversationEngine(
            identity=identity,
            prompts=prompt_registry,
            structured=generator,
            guard=guard,
            policy=conversation_policy,
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
            memory=memory_engine,
            clock=clock,
        )
        return service, memory_engine

    return build


async def test_a_conversation_becomes_a_memory_and_comes_back(
    service_with_memory, memories, clock
) -> None:
    service, memory_engine = service_with_memory(
        [
            '{"text": "いいね。"}',      # turn 1 reply
            '{"text": "そうなんだ。"}',   # turn 2 reply
            SUMMARY,                      # encoding of episode 1
            '{"text": "うん、覚えてる。"}',  # turn 3 reply
            SUMMARY,                      # encoding of episode 2
        ]
    )

    first = await service.handle_inbound(
        inbound(clock, text="きのう海に行った話をしていい?", message_id="1")
    )
    await service.confirm_sent(first, message_id="10")
    # Patch spec 5.4: encoding is background work now, so a test that asserts
    # on its result has to wait for it explicitly.
    await service.drain_background()

    clock.advance(seconds=60 * 60)
    second = await service.handle_inbound(inbound(clock, text="つかれた", message_id="2"))
    await service.confirm_sent(second, message_id="20")
    await service.drain_background()

    # The first episode closed and was encoded during background maintenance.
    assert memories.memory_count() == 1

    clock.advance(seconds=60 * 60)
    third = await service.handle_inbound(
        inbound(clock, text="海に行った話、覚えてる?", message_id="3")
    )

    recalled = third.generation.context.get("relevant_memories")
    assert recalled is not None
    assert "海に行った話" in recalled.content


async def test_memory_context_is_optional_and_absent_when_nothing_is_remembered(
    service_with_memory, clock
) -> None:
    service, _ = service_with_memory(['{"text": "はじめまして。"}'])

    result = await service.handle_inbound(inbound(clock, text="はじめまして"))

    assert result.generation.context.includes("relevant_memories") is False
    assert result.should_send


async def test_recall_reads_memory_not_the_transcript_archive(
    service_with_memory, memories, event_store, clock
) -> None:
    """Spec 10.1: the archive holds it, but only memory can recall it."""
    service, memory_engine = service_with_memory(
        ['{"text": "うん。"}', '{"text": "そう。"}', '{"text": "なんだっけ。"}']
    )

    first = await service.handle_inbound(
        inbound(clock, text="ひみつの合言葉はカワセミ", message_id="1")
    )
    await service.confirm_sent(first, message_id="10")
    # Patch spec 5.4: encoding is background work now, so a test that asserts
    # on its result has to wait for it explicitly.
    await service.drain_background()

    # The words are in the objective archive...
    assert any(
        "カワセミ" in getattr(event.payload, "text", "") for event in event_store.recent()
    )
    # ...but nothing has been encoded, so nothing can be recalled.
    assert memories.memory_count() == 0
    assert (await memory_engine.recall("ひみつの合言葉", now=clock.now())).selected == ()


async def test_memory_maintenance_failure_does_not_break_the_reply(
    service_with_memory, failures, clock
) -> None:
    service, memory_engine = service_with_memory(['{"text": "だいじょうぶ。"}'])

    def explode(*args, **kwargs):
        raise RuntimeError("disk on fire")

    memory_engine.close_due_episodes = explode  # type: ignore[method-assign]

    result = await service.handle_inbound(inbound(clock, text="ねえ"))
    sent = await service.confirm_sent(result, message_id="10")
    await service.drain_background()

    assert result.should_send
    assert sent.event_type == "YUI_MESSAGE_SENT"
    assert any(
        row["reason_code"] == "memory_maintenance_failed" for row in failures.recent()
    )
