"""Discord gateway plumbing (spec 37: the interface only converts I/O).

The gateway is exercised with duck-typed stand-ins, so these tests need no
gateway connection and no ``discord`` import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.conversation.events import YUI_MESSAGE_SENT
from app.interfaces.discord.gateway import DiscordGateway, DiscordGatewayError, to_inbound
from tests.unit.test_conversation import (  # noqa: F401 - shared fixtures
    CHANNEL,
    OWNER,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    service_factory,
)


@dataclass
class FakeChannel:
    id: int = 222222222222222222
    sent: list[str] = field(default_factory=list)

    async def send(self, text: str) -> Any:
        self.sent.append(text)
        return type("SentMessage", (), {"id": 999})()


@dataclass
class BrokenChannel(FakeChannel):
    async def send(self, text: str) -> Any:
        raise RuntimeError("discord is down")


@dataclass
class FakeAuthor:
    id: int = int(OWNER)
    bot: bool = False


@dataclass
class FakeMessage:
    channel: FakeChannel
    author: FakeAuthor
    content: str
    created_at: Any
    id: int = 333
    guild: Any = None
    attachments: tuple = ()
    reference: Any = None


def test_to_inbound_maps_a_direct_message(clock) -> None:
    message = FakeMessage(
        channel=FakeChannel(), author=FakeAuthor(), content="やあ", created_at=clock.now()
    )

    inbound = to_inbound(message)

    assert inbound.message_id == "333"
    assert inbound.channel_id == CHANNEL
    assert inbound.channel_type == "direct_message"
    assert inbound.author_id == OWNER
    assert inbound.author_is_bot is False
    assert inbound.text == "やあ"


def test_to_inbound_marks_guild_channels(clock) -> None:
    message = FakeMessage(
        channel=FakeChannel(),
        author=FakeAuthor(),
        content="hi",
        created_at=clock.now(),
        guild=type("Guild", (), {"id": 42})(),
    )
    inbound = to_inbound(message)
    assert inbound.channel_type == "guild_text"
    assert inbound.guild_id == "42"


def test_gateway_requires_a_token(service_factory) -> None:
    with pytest.raises(DiscordGatewayError):
        DiscordGateway(service_factory([]), token="")


async def test_gateway_sends_the_reply_and_records_it(
    service_factory, event_store, clock
) -> None:
    service = service_factory(['{"text": "こんばんは。"}'])
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = FakeChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="こんばんは", created_at=clock.now()
    )

    result = await gateway.handle_message(message)

    assert channel.sent == ["こんばんは。"]
    assert result.should_send
    sent_events = [
        event for event in event_store.recent() if event.event_type == YUI_MESSAGE_SENT
    ]
    assert len(sent_events) == 1
    assert sent_events[0].payload.message_id == "999"


async def test_failed_send_is_recorded_and_not_treated_as_said(
    service_factory, event_store, failures, clock
) -> None:
    """Spec 2.15: only a delivered message is a completed experience."""
    service = service_factory(['{"text": "届かない返事"}'])
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    message = FakeMessage(
        channel=BrokenChannel(), author=FakeAuthor(), content="ねえ", created_at=clock.now()
    )

    await gateway.handle_message(message)

    types = [event.event_type for event in event_store.recent()]
    assert YUI_MESSAGE_SENT not in types
    assert any(row["reason_code"] == "discord_send_failed" for row in failures.recent())


async def test_suppressed_reply_sends_nothing(service_factory, clock) -> None:
    service = service_factory(['{"text": "さっき散歩に行ってきた。"}'] * 2)
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = FakeChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="なにしてた?", created_at=clock.now()
    )

    result = await gateway.handle_message(message)

    assert channel.sent == []
    assert result.suppressed
