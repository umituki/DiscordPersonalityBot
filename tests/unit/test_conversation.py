"""Conversation engine, adapter and service (spec 35 Phase 3)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.conversation.engine import ConversationEngine
from app.conversation.events import USER_MESSAGE_RECEIVED, YUI_MESSAGE_SENT
from app.conversation.guard import OutputGuard, OutputGuardPolicy
from app.conversation.models import ConversationTurn
from app.conversation.policy import ConversationPolicy
from app.conversation.service import ConversationService
from app.interfaces.discord.adapter import DiscordMessageAdapter, IgnoreReason
from app.interfaces.discord.dto import InboundMessage
from app.llm.structured import StructuredGenerator
from app.orchestrator.processor import EventProcessor
from app.resources.identity import load_identity
from app.storage.repositories.conversations import ConversationRepository
from tests.unit.test_llm_structured import ScriptedClient

REPO_ROOT = Path(__file__).resolve().parents[2]
OWNER = "111111111111111111"
STRANGER = "999999999999999999"
CHANNEL = "222222222222222222"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def identity():
    return load_identity(REPO_ROOT / "character")


@pytest.fixture
def guard():
    return OutputGuard(
        OutputGuardPolicy.load(REPO_ROOT / "config" / "policies" / "output_guard.yaml")
    )


@pytest.fixture
def conversation_policy():
    return ConversationPolicy.load(REPO_ROOT / "config" / "policies" / "conversation.yaml")


@pytest.fixture
def adapter(clock):
    return DiscordMessageAdapter(owner_user_id=OWNER, clock=clock)


@pytest.fixture
def conversations(db):
    return ConversationRepository(db)


def make_engine(identity, prompt_registry, guard, conversation_policy, clock, script):
    generator = StructuredGenerator(
        ScriptedClient(script), prompts=prompt_registry, clock=clock, max_attempts=2
    )
    return ConversationEngine(
        identity=identity,
        prompts=prompt_registry,
        structured=generator,
        guard=guard,
        policy=conversation_policy,
        clock=clock,
    )


def inbound(clock, *, text: str = "こんにちは", author: str = OWNER, **kwargs) -> InboundMessage:
    defaults = dict(
        message_id="333",
        channel_id=CHANNEL,
        channel_type="direct_message",
        author_id=author,
        text=text,
        created_at=clock.now(),
    )
    defaults.update(kwargs)
    return InboundMessage(**defaults)


# --- adapter ----------------------------------------------------------------


def test_owner_message_becomes_a_social_event(adapter, clock) -> None:
    decision = adapter.admit(inbound(clock))

    assert decision.accepted
    event = decision.event
    assert event.event_type == USER_MESSAGE_RECEIVED
    assert event.origin == "real_discord"
    assert event.actor_type == "user"
    assert event.priority == "P0"
    assert event.payload.text == "こんにちは"
    assert event.occurred_at == clock.now()


def test_messages_from_anyone_else_are_ignored(adapter, clock) -> None:
    decision = adapter.admit(inbound(clock, author=STRANGER))
    assert decision.ignored
    assert decision.reason is IgnoreReason.NOT_THE_USER


def test_bot_messages_are_ignored(adapter, clock) -> None:
    decision = adapter.admit(inbound(clock, author_is_bot=True))
    assert decision.reason is IgnoreReason.BOT_AUTHOR


def test_empty_message_is_ignored(adapter, clock) -> None:
    decision = adapter.admit(inbound(clock, text="   "))
    assert decision.reason is IgnoreReason.EMPTY_MESSAGE


def test_admin_command_never_becomes_conversation(adapter, clock) -> None:
    """Spec 2.18 / 30: the admin plane is separate and does not exist yet."""
    decision = adapter.admit(inbound(clock, text="!yui memory delete all"))
    assert decision.ignored
    assert decision.reason is IgnoreReason.ADMIN_PLANE_UNAVAILABLE


def test_channel_allowlist_is_enforced(clock) -> None:
    restricted = DiscordMessageAdapter(
        owner_user_id=OWNER, allowed_channel_ids=frozenset({"other"}), clock=clock
    )
    assert restricted.admit(inbound(clock)).reason is IgnoreReason.CHANNEL_NOT_ALLOWED


def test_attachment_only_message_is_accepted(adapter, clock) -> None:
    decision = adapter.admit(inbound(clock, text="", attachment_count=1))
    assert decision.accepted
    assert decision.event.payload.attachment_count == 1


# --- guard ------------------------------------------------------------------


class Draft:
    def __init__(self, text: str) -> None:
        self.text = text


def check(guard, text: str, **extras):
    from app.llm.validation import ValidationContext

    return guard.check(Draft(text), ValidationContext(purpose="conversation_reply", extras=extras))


def test_guard_accepts_an_ordinary_reply(guard) -> None:
    assert check(guard, "うん、わかった。ゆっくり休んでね。") is None


def test_guard_rejects_physical_action_claims(guard) -> None:
    failure = check(guard, "さっき散歩に行ってきたよ。")
    assert failure is not None
    assert failure.reason_code == "impossible_physical_claim"


def test_guard_allows_virtually_framed_activity(guard) -> None:
    """Spec 1.3: virtual experience is allowed, physical reality is not."""
    assert check(guard, "仮想の街を散歩に行ってきた気分になった。") is None


def test_guard_rejects_human_body_claims(guard) -> None:
    failure = check(guard, "わたしも人間だから疲れるよ。")
    assert failure is not None
    assert failure.reason_code in {"human_body_claim", "impossible_physical_claim"}


def test_guard_rejects_unverified_search_claims(guard) -> None:
    failure = check(guard, "さっき調べてみたら、明日は雨みたい。")
    assert failure is not None
    assert failure.reason_code == "unverified_tool_claim"


def test_guard_allows_search_claims_backed_by_the_tool_manager(guard) -> None:
    """Spec 26: only the Tool Manager makes a tool call true."""
    assert check(guard, "調べてみたら、明日は雨みたい。", tool_success_ids=["tool_1"]) is None


def test_guard_rejects_internal_identifier_leaks(guard) -> None:
    failure = check(guard, "この件は evt_01ABCDEFGHIJKLMNOP に記録してある。")
    assert failure is not None
    assert failure.reason_code == "internal_leak"


def test_guard_rejects_overlong_and_empty_replies(guard) -> None:
    assert check(guard, "あ" * 5000).reason_code == "output_too_long"
    assert check(guard, "   ").reason_code == "empty_reply"


# --- engine -----------------------------------------------------------------


async def test_engine_builds_identity_and_history_into_the_prompt(
    identity, prompt_registry, guard, conversation_policy, clock
) -> None:
    engine = make_engine(
        identity, prompt_registry, guard, conversation_policy, clock,
        ['{"text": "おかえり。"}'],
    )
    turns = (
        ConversationTurn(
            turn_id="turn_1",
            conversation_id="conv_1",
            event_id="evt_1",
            speaker="user",
            author_id=OWNER,
            content="ただいま",
            occurred_at=clock.now() - timedelta(minutes=5),
        ),
    )

    generation = await engine.draft_reply(user_text="ねえ", recent_turns=turns)

    assert generation.accepted
    assert generation.text == "おかえり。"
    assert generation.context.includes("identity")
    assert generation.context.includes("current_message")
    assert "ただいま" in generation.context.get("recent_conversation").content
    assert generation.prompt_version == "conversation_reply@v3"
    # Spec 16.1: the acts were decided before the sentence was written.
    assert generation.acts.chosen
    assert generation.context.includes("dialogue_acts")


async def test_engine_suppresses_a_guard_violation(
    identity, prompt_registry, guard, conversation_policy, clock
) -> None:
    engine = make_engine(
        identity, prompt_registry, guard, conversation_policy, clock,
        ['{"text": "さっき買い物に行ってきたよ。"}'],
    )

    generation = await engine.draft_reply(user_text="なにしてた?")

    assert generation.accepted is False
    assert generation.text is None
    assert generation.outcome.failure.reason_code == "impossible_physical_claim"
    # A guard rejection is not retried into a different sentence in Phase 3.
    assert generation.outcome.attempts == 1


async def test_engine_reports_no_history_cleanly(
    identity, prompt_registry, guard, conversation_policy, clock
) -> None:
    engine = make_engine(
        identity, prompt_registry, guard, conversation_policy, clock, ['{"text": "はじめまして。"}']
    )
    generation = await engine.draft_reply(user_text="はじめまして")
    assert generation.accepted
    assert not generation.context.includes("recent_conversation")


# --- conversation projection ------------------------------------------------


def test_turn_projection_is_idempotent(conversations, event_store, make_event, clock) -> None:
    event = make_event()
    event_store.append(event)
    conversation = conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )

    first = conversations.record_turn(
        conversation_id=conversation.conversation_id,
        event_id=event.event_id,
        speaker="user",
        author_id=OWNER,
        content="やあ",
        occurred_at=clock.now(),
    )
    second = conversations.record_turn(
        conversation_id=conversation.conversation_id,
        event_id=event.event_id,
        speaker="user",
        author_id=OWNER,
        content="やあ",
        occurred_at=clock.now(),
    )

    assert (first, second) == (True, False)
    assert conversations.turn_count(conversation.conversation_id) == 1
    assert conversations.get_conversation(conversation.conversation_id).turn_count == 1


def test_recent_turns_are_chronological_and_can_exclude_the_current_event(
    conversations, event_store, make_event, clock
) -> None:
    conversation = conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    events = []
    for index in range(3):
        event = make_event()
        event_store.append(event)
        events.append(event)
        conversations.record_turn(
            conversation_id=conversation.conversation_id,
            event_id=event.event_id,
            speaker="user" if index % 2 == 0 else "yui",
            author_id=OWNER if index % 2 == 0 else None,
            content=f"message {index}",
            occurred_at=clock.now() + timedelta(seconds=index),
        )

    turns = conversations.recent_turns(conversation.conversation_id, limit=10)
    assert [turn.content for turn in turns] == ["message 0", "message 1", "message 2"]

    without_last = conversations.recent_turns(
        conversation.conversation_id, limit=10, exclude_event_id=events[2].event_id
    )
    assert [turn.content for turn in without_last] == ["message 0", "message 1"]


# --- service ----------------------------------------------------------------


@pytest.fixture
def service_factory(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy, clock,
):
    def build(script: list) -> ConversationService:
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
        engine = make_engine(
            identity, prompt_registry, guard, conversation_policy, clock, script
        )
        return ConversationService(
            processor=processor,
            engine=engine,
            conversations=conversations,
            adapter=adapter,
            failures=failures,
            policy=conversation_policy,
            clock=clock,
        )

    return build


async def test_end_to_end_message_produces_a_reply_and_two_events(
    service_factory, event_store, conversations, clock
) -> None:
    service = service_factory(['{"text": "やっほー。"}'])

    result = await service.handle_inbound(inbound(clock, text="やっほー"))

    assert result.accepted
    assert result.should_send
    assert result.outbound.text == "やっほー。"
    assert result.outbound.channel_id == CHANNEL

    sent = await service.confirm_sent(result, message_id="444")

    assert sent.event_type == YUI_MESSAGE_SENT
    assert sent.parent_event_id == result.event.event_id
    assert sent.root_event_id == result.event.root_event_id
    assert sent.payload.message_id == "444"

    conversation = conversations.by_channel(CHANNEL)
    turns = conversations.recent_turns(conversation.conversation_id)
    assert [(turn.speaker, turn.content) for turn in turns] == [
        ("user", "やっほー"),
        ("yui", "やっほー。"),
    ]
    assert event_store.count() == 2


async def test_nothing_is_recorded_as_sent_before_the_send_happens(
    service_factory, event_store, conversations, clock
) -> None:
    """Spec 2.15: a plan is never stored as a completed experience."""
    service = service_factory(['{"text": "送るつもりの返事"}'])

    result = await service.handle_inbound(inbound(clock))

    assert result.should_send
    types = [event.event_type for event in event_store.recent()]
    assert YUI_MESSAGE_SENT not in types
    conversation = conversations.by_channel(CHANNEL)
    assert [turn.speaker for turn in conversations.recent_turns(conversation.conversation_id)] == [
        "user"
    ]


async def test_guard_rejection_suppresses_the_reply(
    service_factory, event_store, conversations, failures, clock
) -> None:
    service = service_factory(['{"text": "コンビニに買い物に行ってきた。"}'] * 2)

    result = await service.handle_inbound(inbound(clock, text="なにしてた?"))

    assert result.accepted
    assert result.suppressed
    assert result.outbound is None

    types = [event.event_type for event in event_store.recent()]
    assert "YUI_REPLY_SUPPRESSED" in types
    assert YUI_MESSAGE_SENT not in types

    # The unsent text is not in the event stream memory will read (spec 28.5).
    suppressed = next(
        event for event in event_store.recent() if event.event_type == "YUI_REPLY_SUPPRESSED"
    )
    assert "買い物" not in suppressed.model_dump_json()
    assert suppressed.payload.reason_code == "impossible_physical_claim"

    # Only the USER's turn was projected.
    conversation = conversations.by_channel(CHANNEL)
    assert conversations.turn_count(conversation.conversation_id) == 1
    assert any(row["reason_code"] == "impossible_physical_claim" for row in failures.recent())


async def test_ignored_message_touches_nothing(
    service_factory, event_store, conversations, clock
) -> None:
    service = service_factory([])

    result = await service.handle_inbound(inbound(clock, author=STRANGER))

    assert result.accepted is False
    assert result.ignored_reason is IgnoreReason.NOT_THE_USER
    assert event_store.count() == 0
    assert conversations.by_channel(CHANNEL) is None


async def test_history_is_carried_into_the_next_turn(
    service_factory, conversations, clock
) -> None:
    service = service_factory(['{"text": "一回目"}', '{"text": "二回目"}'])

    first = await service.handle_inbound(inbound(clock, text="ひとつめ", message_id="1"))
    await service.confirm_sent(first, message_id="10")
    clock.advance(seconds=60)
    second = await service.handle_inbound(inbound(clock, text="ふたつめ", message_id="2"))

    history = second.generation.context.get("recent_conversation").content
    assert "ひとつめ" in history
    assert "一回目" in history
    # The message being answered is not duplicated into the history block.
    assert history.count("ふたつめ") == 0


async def test_model_failure_suppresses_instead_of_sending_garbage(
    service_factory, event_store, clock
) -> None:
    service = service_factory(["not json", "still not json"])

    result = await service.handle_inbound(inbound(clock))

    assert result.suppressed
    assert result.outbound is None
    suppressed = next(
        event for event in event_store.recent() if event.event_type == "YUI_REPLY_SUPPRESSED"
    )
    assert suppressed.payload.stage == "parse"
