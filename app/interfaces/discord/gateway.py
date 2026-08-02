"""discord.py gateway.

The only module that imports ``discord``. It converts gateway objects into
:class:`InboundMessage`, hands them to the conversation service, sends whatever
the service produced, and reports back what was actually delivered.

It performs no persistence and makes no psychological decision (spec 37:
``Discord handler → DB direct write`` is forbidden).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.clock import Clock, SystemClock, ensure_aware
from app.conversation.service import ConversationResult, ConversationService
from app.interfaces.discord.dto import InboundMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from discord import Message
else:  # discord.py is imported only where it is actually needed
    Message = Any

logger = logging.getLogger(__name__)


def _mark(trace, stage: str) -> None:
    """Patch spec 19.2. A missing timestamp must never cost a reply."""
    if trace is not None:
        trace.mark(stage)


class DiscordGatewayError(RuntimeError):
    """Raised when the gateway cannot be started."""


def to_inbound(message: Any) -> InboundMessage:
    """Convert a ``discord.Message`` into a transport-neutral message."""
    channel = message.channel
    guild = getattr(message, "guild", None)
    channel_type = "direct_message" if guild is None else "guild_text"
    reference = getattr(message, "reference", None)
    reply_to = (
        str(reference.message_id)
        if reference is not None and getattr(reference, "message_id", None) is not None
        else None
    )
    return InboundMessage(
        message_id=str(message.id),
        channel_id=str(channel.id),
        channel_type=channel_type,  # type: ignore[arg-type]
        author_id=str(message.author.id),
        author_is_bot=bool(getattr(message.author, "bot", False)),
        text=message.content or "",
        created_at=ensure_aware(message.created_at),
        attachment_count=len(getattr(message, "attachments", ()) or ()),
        reply_to_message_id=reply_to,
        guild_id=None if guild is None else str(guild.id),
    )


class DiscordGateway:
    """Runs the Discord client and pumps messages through the service."""

    def __init__(
        self,
        service: ConversationService,
        *,
        token: str,
        clock: Clock | None = None,
    ) -> None:
        if not token:
            raise DiscordGatewayError("a Discord bot token is required")
        self._service = service
        self._token = token
        self._clock = clock or SystemClock()
        self._client: Any = None

    def build_client(self) -> Any:
        import discord  # imported here so the rest of the app never needs it

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:  # pragma: no cover - requires a gateway
            logger.info("discord connected as %s", client.user)

        @client.event
        async def on_message(message: Message) -> None:  # pragma: no cover
            if client.user is not None and message.author.id == client.user.id:
                return
            await self.handle_message(message)

        self._client = client
        return client

    async def handle_message(self, message: Any) -> ConversationResult:
        """Process one gateway message and send the reply, if any.

        The typing indicator wraps everything the USER is waiting for and
        nothing else (patch spec 6). It starts once the message is known to be
        one YUI will answer, and ``async with`` ends it on every exit — send
        success, suppression, an LLM error, a Discord error, cancellation and
        shutdown alike (6.3). It is never recorded: typing is transient UI, not
        an experience (6.4). No artificial delay is added (6.5).
        """
        inbound = to_inbound(message)
        if not self._service.intends_to_reply(inbound):
            return await self._service.handle_inbound(inbound)

        # Patch spec 19.2: the trace starts here, before the indicator, so the
        # USER's wait is measured from when the message arrived rather than
        # from when the service got round to it.
        trace = self._service.start_trace(inbound)

        async with self._typing(message):
            _mark(trace, "typing_started_at")
            result = await self._service.handle_inbound(inbound, trace=trace)
            if not result.should_send or result.outbound is None:
                return result

            _mark(trace, "discord_send_started_at")
            try:
                sent = await self._send(message, result.outbound.text)
            except Exception as exc:  # noqa: BLE001 - the send is the fragile part
                _mark(trace, "discord_send_ended_at")
                logger.exception("failed to send reply channel=%s", result.outbound.channel_id)
                await self._service.record_send_failure(result, repr(exc))
                return result
            _mark(trace, "discord_send_ended_at")

        # Outside the indicator: the reply is already delivered, and the
        # post-send run must not keep "typing" on screen after it.
        _mark(trace, "typing_stopped_at")
        await self._service.confirm_sent(
            result,
            message_id=str(getattr(sent, "id", "unknown")),
            channel_type=inbound.channel_type,
        )
        return result

    @staticmethod
    @asynccontextmanager
    async def _typing(message: Any) -> AsyncIterator[None]:
        """``channel.typing()`` where the channel has one, otherwise nothing.

        A channel object without ``typing`` is not a reason to drop a reply, so
        this degrades to a no-op rather than raising.
        """
        factory = getattr(getattr(message, "channel", None), "typing", None)
        indicator = None
        if factory is not None:
            try:
                indicator = factory()
                await indicator.__aenter__()
            except Exception:  # noqa: BLE001 - a UI hint must never block a reply
                logger.warning("typing indicator could not be started", exc_info=True)
                indicator = None
        try:
            yield
        finally:
            # ``finally`` rather than ``except``: the indicator has to stop on
            # cancellation and shutdown too, not only on ordinary returns.
            if indicator is not None:
                try:
                    await indicator.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    logger.warning("typing indicator did not stop cleanly", exc_info=True)

    @staticmethod
    async def _send(message: Any, text: str) -> Any:
        return await message.channel.send(text)

    async def start(self) -> None:  # pragma: no cover - requires a gateway
        client = self._client or self.build_client()
        await client.start(self._token)

    async def close(self) -> None:  # pragma: no cover - requires a gateway
        if self._client is not None:
            await self._client.close()
