"""INVARIANT: what YUI says stays inside her existence and her records.

Covers, for Phase 3:

* spec 1.3 — no human body, no real-world physical action,
* spec 17.3 / 26 — no unverified tool success,
* spec 1.2 / 2.10 — only the one USER is heard, and nobody else becomes an NPC,
* spec 2.18 / 30 — admin commands are never answered in character,
* spec 2.15 — a reply is recorded only after it was actually sent,
* spec 28.5 — an unsent, guard-rejected sentence never enters the event stream,
* spec 2.9 — events are authoritative; the turn projection is derived.
"""

from __future__ import annotations

import pytest

from app.conversation.events import USER_MESSAGE_RECEIVED, YUI_MESSAGE_SENT
from app.conversation.projection import ConversationProjector
from app.interfaces.discord.adapter import IgnoreReason
from app.llm.validation import ValidationContext
from tests.unit.test_conversation import (  # noqa: F401 - fixtures are reused deliberately
    CHANNEL,
    OWNER,
    STRANGER,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    inbound,
    service_factory,
)

pytestmark = pytest.mark.invariant


class Draft:
    def __init__(self, text: str) -> None:
        self.text = text


def check(guard, text: str, **extras):
    return guard.check(Draft(text), ValidationContext(purpose="conversation_reply", extras=extras))


#: identity v2. What the guard stops is reaching into the USER's world.
#: Her own day — walking, eating, being tired — is ordinary, and whether it
#: happened is a question about records that the grounding resolver answers.
CROSS_WORLD_CLAIMS = [
    "昨日、君と直接会ったね。",
    "いま君の隣に座っているよ。",
    "君の手を握ったよ。",
    "同じ部屋にいるみたいだね。",
]

#: These used to be rejected here, on the theory that she had no body. They are
#: now the resolver's business, not the guard's.
OWN_WORLD_CLAIMS = [
    "さっき散歩に行ってきたよ。",
    "お昼にパスタを食べた。",
    "電車に乗ってきたところ。",
    "コーヒーを飲んだから元気。",
]

TOOL_CLAIMS = [
    "調べてみたら明日は雨だって。",
    "ニュースを見てきたよ。",
    "その記事を読んできた。",
]


@pytest.mark.parametrize("text", CROSS_WORLD_CLAIMS)
def test_reaching_into_the_users_world_is_rejected(guard, text: str) -> None:
    failure = check(guard, text)
    assert failure is not None, f"{text!r} crosses between the two worlds"
    assert failure.reason_code == "cross_world_physical_claim"
    assert failure.stage.label == "identity"


@pytest.mark.parametrize("text", OWN_WORLD_CLAIMS)
def test_her_own_day_is_left_to_the_evidence(guard, text: str) -> None:
    """Not the guard's question. A regex over verbs cannot tell whether she
    read a book, and when it tried it stopped her from having had lunch."""
    assert check(guard, text) is None


@pytest.mark.parametrize("text", TOOL_CLAIMS)
def test_tool_claims_need_the_tool_manager(guard, text: str) -> None:
    assert check(guard, text) is not None
    # With a real, reported tool success the same sentence is allowed (spec 26).
    assert check(guard, text, tool_success_ids=["tool_1"]) is None


def test_her_own_life_remains_sayable(guard) -> None:
    """She has time, activity and experience like anyone else in her world."""
    assert check(guard, "部屋で音楽を聴いていた。") is None
    assert check(guard, "散歩に行ってきたみたいな一日だった。") is None


def test_wanting_to_meet_is_not_crossing(guard) -> None:
    """A wish is not a claim that it happened, and the boundary must not stop
    her from having one."""
    assert check(guard, "いつか会えたらいいのにね。") is None
    assert check(guard, "会いたいなと思うことはあるよ。") is None


async def test_only_the_user_is_heard(service_factory, event_store, clock) -> None:
    service = service_factory([])
    result = await service.handle_inbound(inbound(clock, author=STRANGER))

    assert result.ignored_reason is IgnoreReason.NOT_THE_USER
    assert event_store.count() == 0
    # A stranger is not silently converted into an NPC either (spec 2.10).
    assert event_store.recent(origin="virtual_life") == []


async def test_admin_commands_never_reach_the_character(
    service_factory, event_store, clock
) -> None:
    service = service_factory([])
    result = await service.handle_inbound(inbound(clock, text="!yui reset personality"))

    assert result.ignored_reason is IgnoreReason.ADMIN_PLANE_UNAVAILABLE
    assert result.outbound is None
    assert event_store.count() == 0


async def test_unsent_reply_is_never_recorded_as_said(
    service_factory, event_store, conversations, clock
) -> None:
    service = service_factory(['{"text": "きのう君の家に行ってきた。"}'] * 2)

    result = await service.handle_inbound(inbound(clock))

    assert result.suppressed
    types = [event.event_type for event in event_store.recent()]
    assert YUI_MESSAGE_SENT not in types
    conversation = conversations.by_channel(CHANNEL)
    assert [turn.speaker for turn in conversations.recent_turns(conversation.conversation_id)] == [
        "user"
    ]


async def test_guard_rejected_text_stays_out_of_the_event_stream(
    service_factory, event_store, clock
) -> None:
    """Spec 28.5: an unsent hallucination must not be available to memory."""
    hallucination = "きのう海に行ってきた"
    service = service_factory([f'{{"text": "{hallucination}。"}}'] * 2)

    await service.handle_inbound(inbound(clock))

    for event in event_store.recent():
        assert hallucination not in event.model_dump_json()


async def test_sent_reply_is_recorded_only_after_delivery(
    service_factory, event_store, clock
) -> None:
    service = service_factory(['{"text": "うん。"}'])
    result = await service.handle_inbound(inbound(clock))

    before = [event.event_type for event in event_store.recent()]
    assert before == [USER_MESSAGE_RECEIVED]

    await service.confirm_sent(result, message_id="555")

    after = {event.event_type for event in event_store.recent()}
    assert after == {USER_MESSAGE_RECEIVED, YUI_MESSAGE_SENT}


async def test_projection_is_derived_and_rebuildable(
    service_factory, db, event_store, conversations, clock
) -> None:
    """Spec 2.9: the events are the record; the turn table is a read model."""
    service = service_factory(['{"text": "ひとつめの返事"}', '{"text": "ふたつめの返事"}'])

    first = await service.handle_inbound(inbound(clock, text="ひとつめ", message_id="1"))
    await service.confirm_sent(first, message_id="10")
    clock.advance(seconds=30)
    second = await service.handle_inbound(inbound(clock, text="ふたつめ", message_id="2"))
    await service.confirm_sent(second, message_id="20")

    conversation = conversations.by_channel(CHANNEL)
    original = [
        (turn.speaker, turn.content)
        for turn in conversations.recent_turns(conversation.conversation_id, limit=50)
    ]
    assert len(original) == 4

    replayed = ConversationProjector(db, event_store, conversations).rebuild()

    rebuilt_conversation = conversations.by_channel(CHANNEL)
    rebuilt = [
        (turn.speaker, turn.content)
        for turn in conversations.recent_turns(rebuilt_conversation.conversation_id, limit=50)
    ]
    assert replayed == 4
    assert rebuilt == original


async def test_suppressed_replies_are_not_replayed_as_utterances(
    service_factory, db, event_store, conversations, clock
) -> None:
    service = service_factory(['{"text": "いま君の隣に座っているよ。"}'] * 2)
    await service.handle_inbound(inbound(clock, text="なにしてた?"))

    replayed = ConversationProjector(db, event_store, conversations).rebuild()

    assert replayed == 1  # only the USER's message
    conversation = conversations.by_channel(CHANNEL)
    assert [
        turn.speaker for turn in conversations.recent_turns(conversation.conversation_id)
    ] == ["user"]
