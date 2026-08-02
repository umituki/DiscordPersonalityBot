"""INVARIANT: a reply is never missing from the next turn's context.

Patch spec 5 and 22. The 2026-08-02 run reproduced this exactly: the second
USER message was answered without YUI's own previous reply in context, because
a full processing run and memory maintenance sat between the send and the turn
projection.

These tests deliberately make post-send work slow. The ordering must hold
because of where the projection is, not because the machine happened to be
fast.
"""

from __future__ import annotations

import asyncio

import pytest

from app.psychology.appraisal import AppraisalEngine
from tests.unit.test_conversation import (  # noqa: F401 - shared fixtures
    OWNER,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    inbound,
    service_factory,
)

pytestmark = pytest.mark.invariant


async def test_the_previous_reply_is_in_context_for_the_next_message(
    service_factory, conversations, clock  # noqa: F811
) -> None:
    """Patch spec 5.2, acceptance Scenario 2."""
    service = service_factory(
        ['{"text": "はじめまして。"}', '{"text": "そうなんだ、うれしい。"}']
    )

    first = await service.handle_inbound(inbound(clock, text="初めまして～"))
    await service.confirm_sent(first, message_id="10")

    # USER2 arrives immediately, while post-send background work is still in
    # flight. This is the case that failed on real hardware.
    clock.advance(seconds=2)
    conversation = conversations.by_channel(str(first.outbound.channel_id))
    recent = conversations.recent_turns(conversation.conversation_id, limit=10)

    speakers = [turn.speaker for turn in recent]
    assert "yui" in speakers, "YUI's delivered reply must already be projected"
    assert [turn.content for turn in recent if turn.speaker == "yui"] == ["はじめまして。"]
    assert speakers.index("user") < speakers.index("yui")


async def test_slow_post_send_work_does_not_delay_the_projection(
    service_factory, conversations, clock  # noqa: F811
) -> None:
    """Patch spec 5.4 and prohibition 7: the USER never waits for background work."""
    service = service_factory(['{"text": "やっほー。"}'])
    result = await service.handle_inbound(inbound(clock, text="やっほー"))

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_work() -> None:
        started.set()
        await release.wait()

    service._post_send_work = lambda *args, **kwargs: slow_work()  # noqa: SLF001

    await service.confirm_sent(result, message_id="10")

    # confirm_sent returned while the background work is still blocked, and the
    # turn is already visible.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    conversation = conversations.by_channel(str(result.outbound.channel_id))
    recent = conversations.recent_turns(conversation.conversation_id, limit=10)
    assert any(turn.speaker == "yui" for turn in recent)

    release.set()
    await service.drain_background()


async def test_the_user_message_is_projected_before_the_reply_is_generated(
    service_factory, conversations, clock  # noqa: F811
) -> None:
    """Patch spec 5.1: a crash mid-reply must not lose what the USER said."""
    service = service_factory(['{"text": "うん。"}'])
    result = await service.handle_inbound(inbound(clock, text="聞いてほしいことがある"))

    conversation = conversations.by_channel(str(result.outbound.channel_id))
    recent = conversations.recent_turns(conversation.conversation_id, limit=10)

    user_turns = [turn for turn in recent if turn.speaker == "user"]
    assert [turn.content for turn in user_turns] == ["聞いてほしいことがある"]


async def test_a_reply_that_was_never_sent_is_not_in_the_history(
    service_factory, conversations, clock  # noqa: F811
) -> None:
    """The projection is of what was delivered, not of what was drafted."""
    service = service_factory(['{"text": "これは送られない。"}'])
    result = await service.handle_inbound(inbound(clock, text="やっほー"))

    conversation = conversations.by_channel(str(result.outbound.channel_id))
    recent = conversations.recent_turns(conversation.conversation_id, limit=10)

    assert [turn.speaker for turn in recent] == ["user"]


# --- YUI does not appraise her own words (patch spec 5.3) -------------------
def test_yui_does_not_appraise_her_own_outbound_message(make_event) -> None:
    from app.conversation.events import YUI_MESSAGE_SENT, YuiMessageSentPayload

    sent = make_event(
        event_type=YUI_MESSAGE_SENT,
        category="social",
        actor_type="yui",
        payload=YuiMessageSentPayload(
            text="やっほー", channel_id="1", message_id="10", in_reply_to_event_id="evt_x"
        ),
    )
    assert AppraisalEngine.is_self_authored(sent) is True


def test_a_user_message_is_still_appraised(make_event) -> None:
    assert AppraisalEngine.is_self_authored(make_event(actor_type="user")) is False


async def test_the_sent_event_costs_no_model_call(
    service_factory, clock, llm_calls_repo  # noqa: F811
) -> None:
    """Patch spec 5.3: re-reading her own reply is a call the USER pays for."""
    service = service_factory(['{"text": "やっほー。"}'])
    result = await service.handle_inbound(inbound(clock, text="やっほー"))
    before = len(llm_calls_repo.recent(limit=100, purpose="appraisal"))

    await service.confirm_sent(result, message_id="10")
    await service.drain_background()

    after = len(llm_calls_repo.recent(limit=100, purpose="appraisal"))
    assert after == before
