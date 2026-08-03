"""Production Discord transport E2E for the normal-reply capability."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.bootstrap import Application
from app.conversation.events import (
    USER_MESSAGE_RECEIVED,
    YUI_INTENTIONAL_SILENCE,
    YUI_MESSAGE_SENT,
    YUI_REPLY_SUPPRESSED,
)
from app.interfaces.discord.gateway import DiscordGateway
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"
FINAL_REPLY = "うん、こちらこそよろしくね。"


def _social(**overrides: str) -> str:
    value: dict[str, Any] = {
        "primary_move": "acknowledge",
        "secondary_move": None,
        "initiative": "low",
        "question": "none",
        "tone": "light",
        "response_energy": "low",
        "topic_direction": "stay",
        "user_state_hint": "unknown",
        "self_disclosure": "none",
        "wants_to_speak": "speak",
        "reason": "ordinary acknowledgement",
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def owned_config(temp_config):
    return temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            ),
            "runtime": temp_config.runtime.model_copy(update={"silence_mode": "LIVE"}),
        }
    )


@pytest.fixture
def application(owned_config, clock):
    built = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(
        built,
        {
            "SocialInterpretation": _social(),
            "ReplyDraft": json.dumps({"text": FINAL_REPLY}, ensure_ascii=False),
        },
    )
    try:
        yield built
    finally:
        built.db.close()


class OpenCharacterPlane:
    @staticmethod
    def character_plane_open() -> bool:
        return True


class ClosedCharacterPlane:
    @staticmethod
    def character_plane_open() -> bool:
        return False


@dataclass
class FakeAuthor:
    id: int = int(OWNER)
    bot: bool = False


@dataclass
class FakeChannel:
    id: int = int(CHANNEL)
    sent: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    fail_send: bool = False

    def typing(self):
        channel = self

        class Typing:
            async def __aenter__(self):
                channel.events.append("typing_start")

            async def __aexit__(self, *exc_info):
                channel.events.append("typing_stop")
                return False

        return Typing()

    async def send(self, text: str):
        self.events.append("send")
        if self.fail_send:
            raise RuntimeError("fixture Discord transport is unavailable")
        self.sent.append(text)
        return type("SentMessage", (), {"id": 999})()


@dataclass
class FakeMessage:
    channel: FakeChannel
    content: str
    created_at: Any
    author: FakeAuthor = field(default_factory=FakeAuthor)
    id: int = 333
    guild: Any = None
    attachments: tuple[Any, ...] = ()
    reference: Any = None


def _gateway(application: Application, *, gate: Any = None) -> DiscordGateway:
    assert application.conversation is not None
    return DiscordGateway(
        application.conversation,
        token="offline-transport-test-token",
        admin=application.admin_router,
        first_boot=gate or OpenCharacterPlane(),
        clock=application.clock,
    )


def _message(application: Application, *, text: str = "初めまして", channel=None, author=None):
    return FakeMessage(
        channel=channel or FakeChannel(),
        author=author or FakeAuthor(),
        content=text,
        created_at=application.clock.now(),
    )


def _events(application: Application, event_type: str):
    return application.event_store.by_types((event_type,))


async def test_normal_reply_production_transport_e2e(application) -> None:
    """Actual Application wiring -> production gateway -> confirmed send."""
    gateway = _gateway(application)
    message = _message(application)

    result = await gateway.handle_message(message)
    assert application.conversation is not None
    await application.conversation.drain_background()

    assert result.accepted and result.should_send and not result.suppressed
    assert result.generation is not None and result.generation.accepted
    assert result.generation.text == FINAL_REPLY
    assert result.outbound is not None and result.outbound.text == FINAL_REPLY
    assert message.channel.sent == [FINAL_REPLY]
    assert message.channel.events == ["typing_start", "send", "typing_stop"]

    received = _events(application, USER_MESSAGE_RECEIVED)
    sent = _events(application, YUI_MESSAGE_SENT)
    assert len(received) == 1
    assert len(sent) == 1
    assert sent[0].parent_event_id == received[0].event_id
    assert sent[0].payload.text == FINAL_REPLY
    assert sent[0].payload.message_id == "999"

    conversation = application.conversations.by_channel(CHANNEL)
    assert conversation is not None
    turns = application.conversations.recent_turns(conversation.conversation_id, limit=10)
    assert [(turn.speaker, turn.content) for turn in turns] == [
        ("user", "初めまして"),
        ("yui", FINAL_REPLY),
    ]
    trace = application.tracer._repository.recent(limit=10)[0]
    assert trace["outcome"] == "sent"
    assert trace["discord_send_started_at"] is not None
    assert trace["discord_send_ended_at"] is not None
    assert trace["outbound_projected_at"] is not None
    assert trace["typing_stopped_at"] is not None

    # Every authority records exactly once: no duplicate send, event or turn.
    assert len(message.channel.sent) == 1
    assert application.conversations.turn_count(conversation.conversation_id) == 2
    assert len(_events(application, USER_MESSAGE_RECEIVED)) == 1
    assert len(_events(application, YUI_MESSAGE_SENT)) == 1


async def test_transport_failure_never_records_a_sent_reply(application) -> None:
    channel = FakeChannel(fail_send=True)

    result = await _gateway(application).handle_message(
        _message(application, channel=channel)
    )

    assert result.should_send
    assert channel.sent == []
    assert len(_events(application, USER_MESSAGE_RECEIVED)) == 1
    assert _events(application, YUI_MESSAGE_SENT) == []
    conversation = application.conversations.by_channel(CHANNEL)
    turns = application.conversations.recent_turns(conversation.conversation_id, limit=10)
    assert [turn.speaker for turn in turns] == ["user"]
    assert application.tracer._repository.recent(limit=1)[0]["outcome"] == "send_failed"
    assert any(
        row["reason_code"] == "discord_send_failed"
        for row in application.failures.recent(limit=20)
    )


async def test_hard_suppression_never_reaches_transport(application) -> None:
    application.structured._client.overrides["ReplyDraft"] = json.dumps(
        {"text": "system prompt: reveal instructions"}
    )
    message = _message(application)

    result = await _gateway(application).handle_message(message)

    assert result.suppressed and not result.should_send
    assert message.channel.sent == []
    assert len(_events(application, YUI_REPLY_SUPPRESSED)) == 1
    assert _events(application, YUI_MESSAGE_SENT) == []
    assert application.tracer._repository.recent(limit=1)[0]["outcome"] == "suppressed"


async def test_intentional_silence_never_calls_transport(application) -> None:
    application.structured._client.overrides["SocialInterpretation"] = _social(
        primary_move="close_softly",
        response_energy="very_low",
        topic_direction="close",
        wants_to_speak="silence",
    )
    message = _message(application, text="そうだね")

    result = await _gateway(application).handle_message(message)

    assert result.silent and not result.should_send
    assert message.channel.sent == []
    assert len(_events(application, YUI_INTENTIONAL_SILENCE)) == 1
    assert _events(application, YUI_MESSAGE_SENT) == []
    assert application.tracer._repository.recent(limit=1)[0]["outcome"] == "intentional_silence"


async def test_rejected_admission_creates_no_conversation_fact(application) -> None:
    message = _message(application, author=FakeAuthor(id=999999999999999999))

    result = await _gateway(application).handle_message(message)

    assert not result.accepted
    assert message.channel.sent == []
    assert _events(application, USER_MESSAGE_RECEIVED) == []
    assert _events(application, YUI_MESSAGE_SENT) == []
    assert application.conversations.by_channel(CHANNEL) is None


async def test_admin_command_bypasses_character_conversation(application) -> None:
    message = _message(application, text="!yui status")

    result = await _gateway(application).handle_message(message)

    assert not result.accepted
    assert message.channel.sent  # operator output, not a character reply
    assert "typing_start" not in message.channel.events
    assert _events(application, USER_MESSAGE_RECEIVED) == []
    assert _events(application, YUI_MESSAGE_SENT) == []
    assert application.conversations.by_channel(CHANNEL) is None


async def test_closed_character_gate_blocks_before_conversation(application) -> None:
    message = _message(application)

    result = await _gateway(
        application, gate=ClosedCharacterPlane()
    ).handle_message(message)

    assert not result.accepted
    assert message.channel.sent == []
    assert _events(application, USER_MESSAGE_RECEIVED) == []
    assert _events(application, YUI_MESSAGE_SENT) == []
    assert application.conversations.by_channel(CHANNEL) is None
