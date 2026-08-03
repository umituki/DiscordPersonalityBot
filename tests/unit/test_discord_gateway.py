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
from tests.unit.test_conversation import (  # noqa: F401 - pytest fixtures are reused
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


# --- typing indicator (patch spec 6) ----------------------------------------
@dataclass
class TypingChannel(FakeChannel):
    """A channel that records when the typing indicator was on."""

    events: list[str] = field(default_factory=list)
    fail_on_enter: bool = False

    def typing(self) -> Any:
        channel = self

        class _Typing:
            async def __aenter__(self) -> None:
                if channel.fail_on_enter:
                    raise RuntimeError("typing is unavailable")
                channel.events.append("start")

            async def __aexit__(self, *exc_info: Any) -> bool:
                channel.events.append("stop")
                return False

        return _Typing()

    async def send(self, text: str) -> Any:
        self.events.append("send")
        return await super().send(text)


@dataclass
class TypingBrokenChannel(TypingChannel):
    async def send(self, text: str) -> Any:
        self.events.append("send")
        raise RuntimeError("discord is down")


async def test_typing_starts_before_the_work_and_stops_after_the_send(
    service_factory, clock
) -> None:
    """Patch spec 6.1 and 6.3."""
    service = service_factory(['{"text": "やっほー。"}'])
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="やっほー", created_at=clock.now()
    )

    await gateway.handle_message(message)

    assert channel.events == ["start", "send", "stop"]


async def test_typing_stops_when_the_send_fails(service_factory, clock) -> None:
    """Patch spec 6.3: a Discord error must still end the indicator."""
    service = service_factory(['{"text": "届かない返事"}'])
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingBrokenChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="ねえ", created_at=clock.now()
    )

    await gateway.handle_message(message)

    assert channel.events == ["start", "send", "stop"]


async def test_typing_stops_when_the_reply_is_suppressed(service_factory, clock) -> None:
    service = service_factory(['{"text": "さっき散歩に行ってきた。"}'] * 2)
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="なにしてた?", created_at=clock.now()
    )

    result = await gateway.handle_message(message)

    assert result.suppressed
    assert channel.events == ["start", "stop"]


async def test_typing_stops_when_generation_raises(service_factory, clock) -> None:
    """Patch spec 6.3: an LLM error is not an excuse to leave it running."""
    service = service_factory([])
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="やっほー", created_at=clock.now()
    )

    async def explode(_inbound, *, on_speaking=None, **kwargs):
        # The failure happens *after* the decision to speak, which is the case
        # this test is about: the indicator is up, and it has to come down.
        if on_speaking is not None:
            await on_speaking()
        raise RuntimeError("the model is gone")

    class Trace:
        def __init__(self) -> None:
            self.marks: list[str] = []

        def mark(self, stage: str) -> None:
            self.marks.append(stage)

    trace = Trace()
    finished: list[str] = []
    service.start_trace = lambda _inbound: trace  # type: ignore[method-assign]
    service.finish_trace = (  # type: ignore[method-assign]
        lambda _trace, *, outcome: finished.append(outcome)
    )
    service.handle_inbound = explode  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await gateway.handle_message(message)

    assert channel.events == ["start", "stop"]
    assert trace.marks[-1] == "typing_stopped_at"
    assert finished == ["failed"]


async def test_no_typing_for_a_message_that_will_not_be_answered(
    service_factory, clock
) -> None:
    """Patch spec 6.1: reply intent first, indicator second."""
    service = service_factory([])
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel,
        author=FakeAuthor(id=999999999999999999),
        content="やっほー",
        created_at=clock.now(),
    )

    result = await gateway.handle_message(message)

    assert result.accepted is False
    assert channel.events == []


async def test_a_broken_typing_indicator_does_not_stop_the_reply(
    service_factory, clock
) -> None:
    service = service_factory(['{"text": "やっほー。"}'])
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingChannel(fail_on_enter=True)
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="やっほー", created_at=clock.now()
    )

    await gateway.handle_message(message)

    assert channel.sent == ["やっほー。"]
    assert "stop" not in channel.events


async def test_typing_is_not_recorded_as_an_experience(
    service_factory, event_store, clock
) -> None:
    """Patch spec 6.4: transient UI state, never an event."""
    service = service_factory(['{"text": "やっほー。"}'])
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    message = FakeMessage(
        channel=TypingChannel(), author=FakeAuthor(), content="やっほー", created_at=clock.now()
    )

    await gateway.handle_message(message)

    types = {event.event_type for event in event_store.recent()}
    assert not any("TYPING" in event_type for event_type in types)


async def test_a_failure_before_the_decision_shows_no_typing_at_all(
    service_factory, clock
) -> None:
    """Rebuild spec 12.4. The indicator is a promise that a reply is coming.

    If the turn falls over before that promise is made, it must never have been
    shown — three dots for a message that never arrives is worse than silence.
    """

    async def explode(_inbound, *, on_speaking=None, **kwargs):
        raise RuntimeError("the model is gone")

    service = service_factory(['{"text": "うん。"}'])
    service.handle_inbound = explode  # type: ignore[method-assign]
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="やっほー", created_at=clock.now()
    )

    with pytest.raises(RuntimeError):
        await gateway.handle_message(message)

    assert channel.events == []


# --- the admin plane (rebuild spec 30, Phase 5) ------------------------------


class _RecordingService:
    """A conversation service that must never be reached by an admin command."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def intends_to_reply(self, inbound) -> bool:
        self.seen.append(inbound.text)
        return True

    def start_trace(self, inbound):  # pragma: no cover - never reached
        raise AssertionError("an admin command reached the conversation service")

    async def handle_inbound(self, inbound, **kwargs):  # pragma: no cover
        raise AssertionError("an admin command reached the conversation service")


class _AnsweringAdmin:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.calls: list[str] = []

    async def route(self, *, text: str, author_id: str, channel_id: str) -> Any:
        self.calls.append(text)
        return self._outcome


def _outcome(**kwargs: Any) -> Any:
    from app.admin.router import AdminOutcome

    return AdminOutcome(**kwargs)


async def test_an_admin_command_is_routed_before_the_conversation_service(clock) -> None:
    """Spec 30. "Notice afterwards and undo" does not exist: by then the USER
    event is written, the appraisal has run and the episode is open."""
    from app.admin.results import DebugResult

    service = _RecordingService()
    admin = _AnsweringAdmin(
        _outcome(handled=True, result=DebugResult.of("status", summary="ok", rows=[{"a": 1}]))
    )
    gateway = DiscordGateway(service, token="fake-token", admin=admin, clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="!yui status", created_at=clock.now()
    )

    result = await gateway.handle_message(message)

    assert admin.calls == ["!yui status"]
    assert service.seen == []  # the conversation service never saw it
    assert result.accepted is False
    assert channel.sent  # the operator got an answer


async def test_admin_output_shows_no_typing_indicator(clock) -> None:
    """Typing means YUI is composing. Reading the database is not that."""
    from app.admin.results import DebugResult

    admin = _AnsweringAdmin(
        _outcome(handled=True, result=DebugResult.of("status", summary="ok", rows=[{"a": 1}]))
    )
    gateway = DiscordGateway(
        _RecordingService(), token="fake-token", admin=admin, clock=clock
    )
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="!yui status", created_at=clock.now()
    )

    await gateway.handle_message(message)

    assert "start" not in channel.events
    assert channel.events == ["send"]


async def test_a_refused_admin_command_says_nothing_at_all(clock) -> None:
    """A non-owner must not get a reply, in character or out of it."""
    service = _RecordingService()
    admin = _AnsweringAdmin(_outcome(handled=True, refusal="not_the_owner"))
    gateway = DiscordGateway(service, token="fake-token", admin=admin, clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="!yui status", created_at=clock.now()
    )

    await gateway.handle_message(message)

    assert channel.events == []
    assert service.seen == []


async def test_an_ordinary_message_still_reaches_the_conversation(service_factory, clock) -> None:
    """The admin plane is a boundary, not a filter on everything."""
    admin = _AnsweringAdmin(_outcome(handled=False))
    service = service_factory(['{"text": "やっほー。"}'])
    gateway = DiscordGateway(service, token="fake-token", admin=admin, clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="やっほー", created_at=clock.now()
    )

    await gateway.handle_message(message)

    assert channel.sent
