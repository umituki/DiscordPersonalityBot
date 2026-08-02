"""discord.py gateway.

The only module that imports ``discord``. It converts gateway objects into
:class:`InboundMessage`, hands them to the conversation service, sends whatever
the service produced, and reports back what was actually delivered.

It performs no persistence and makes no psychological decision (spec 37:
``Discord handler → DB direct write`` is forbidden).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.clock import Clock, SystemClock, ensure_aware
from app.conversation.service import ConversationResult, ConversationService
from app.interfaces.discord.dto import InboundMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from discord import Message
else:  # discord.py is imported only where it is actually needed
    Message = Any

logger = logging.getLogger(__name__)


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
        """Process one gateway message and send the reply, if any."""
        inbound = to_inbound(message)
        result = await self._service.handle_inbound(inbound)
        if not result.should_send or result.outbound is None:
            return result

        try:
            sent = await self._send(message, result.outbound.text)
        except Exception as exc:  # noqa: BLE001 - the send is the fragile part
            logger.exception("failed to send reply channel=%s", result.outbound.channel_id)
            await self._service.record_send_failure(result, repr(exc))
            return result

        await self._service.confirm_sent(
            result,
            message_id=str(getattr(sent, "id", "unknown")),
            channel_type=inbound.channel_type,
        )
        return result

    @staticmethod
    async def _send(message: Any, text: str) -> Any:
        return await message.channel.send(text)

    async def start(self) -> None:  # pragma: no cover - requires a gateway
        client = self._client or self.build_client()
        await client.start(self._token)

    async def close(self) -> None:  # pragma: no cover - requires a gateway
        if self._client is not None:
            await self._client.close()
