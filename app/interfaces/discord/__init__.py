"""Discord interface.

``dto`` and ``adapter`` are transport-neutral and fully testable without the
``discord`` package. ``gateway`` is the only module that imports discord.py.
"""

from app.interfaces.discord.adapter import (
    DiscordMessageAdapter,
    IgnoreReason,
    InboundDecision,
)
from app.interfaces.discord.dto import InboundMessage, OutboundMessage

__all__ = [
    "DiscordMessageAdapter",
    "IgnoreReason",
    "InboundDecision",
    "InboundMessage",
    "OutboundMessage",
]
