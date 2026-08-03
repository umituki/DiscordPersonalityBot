"""The admin routing boundary (rebuild spec 30, Phase 5).

This sits **upstream of everything**. A message beginning with ``!yui`` is
caught here and never reaches :class:`~app.conversation.service.
ConversationService`, because the alternative — letting it through and undoing
the social processing afterwards — is not a thing that can be done. By the time
you know it was an admin command, the USER message event exists, the appraisal
has run, the relationship has moved and the episode is open. There is no
"cancel"; there is only "never started".

So the order is: route, then converse. Never converse, then discover.

Ownership is checked in exactly one place — here. Individual commands do not
repeat the check, both because repeated checks drift and because a command that
forgot one would be a hole nobody notices. A ``!yui`` from anyone who is not the
owner is refused *and does not fall through to conversation*: an outsider must
not be able to make YUI talk by prefixing their message.

An admin error is an admin error. It never becomes a psychological failure, a
character reply, or a row in the failure table that the drift monitor reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Sequence

from app.admin.commands import AdminCommand, CommandKind, lookup
from app.admin.queries import DebugQueryService
from app.admin.results import DebugResult
from app.clock import Clock, SystemClock

logger = logging.getLogger(__name__)

#: Spec 30. The reserved namespace, shared with the Discord adapter.
ADMIN_PREFIX = "!yui"


@dataclass(frozen=True, slots=True)
class AdminOutcome:
    """What the router decided about a message."""

    handled: bool
    result: DebugResult | None = None
    #: Set when the message was refused rather than answered.
    refusal: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.refusal)


def is_admin_command(text: str) -> bool:
    """Whether this text belongs to the admin plane at all."""
    return (text or "").strip().startswith(ADMIN_PREFIX)


class AdminRouter:
    """Owner-only entry to the debug plane."""

    name = "admin_router"

    def __init__(
        self,
        queries: DebugQueryService,
        *,
        owner_user_id: str | None,
        allowed_channel_ids: frozenset[str] = frozenset(),
        backup: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._queries = queries
        self._owner_user_id = str(owner_user_id) if owner_user_id else None
        self._allowed_channel_ids = allowed_channel_ids
        #: ``!yui backup`` is the one command that is not read-only: it writes
        #: a file. It is wired separately from the query service so that the
        #: read-only guarantee is a property of a whole object rather than of
        #: every method in it (Phase 5, SAFE_MUTATING).
        self._backup = backup
        self._clock = clock or SystemClock()

    async def route(
        self, *, text: str, author_id: str, channel_id: str
    ) -> AdminOutcome:
        """Handle an admin command, or say this message is not one.

        Returns ``handled=False`` only for messages that are not admin at all.
        A refused admin command is still handled — it must not fall through to
        conversation.
        """
        if not is_admin_command(text):
            return AdminOutcome(handled=False)

        if self._owner_user_id is None or str(author_id) != self._owner_user_id:
            logger.warning(
                "admin command refused: not the owner author_id=%s", author_id
            )
            return AdminOutcome(handled=True, refusal="not_the_owner")

        if self._allowed_channel_ids and str(channel_id) not in self._allowed_channel_ids:
            logger.warning("admin command refused: channel %s", channel_id)
            return AdminOutcome(handled=True, refusal="channel_not_allowed")

        tokens = _tokens(text)
        command, args = lookup(tokens)
        if command is None:
            return AdminOutcome(
                handled=True,
                result=DebugResult.failure(
                    tokens[0] if tokens else "?",
                    f"unknown command; try `{ADMIN_PREFIX} help`",
                ),
            )

        try:
            result = await self._run(command, args)
        except Exception as exc:  # noqa: BLE001 - an admin error stays admin
            # Never a FailureRecord: the failure table feeds drift and health,
            # and a mistyped debug query is not something that happened to her.
            logger.exception("admin command failed command=%s", command.key)
            result = DebugResult.failure(command.key, f"{type(exc).__name__}: {exc}")
        return AdminOutcome(handled=True, result=result)

    async def _run(
        self, command: AdminCommand, args: Sequence[str]
    ) -> DebugResult:
        if command.kind is CommandKind.SAFE_MUTATING and command.name == "backup":
            return self._take_backup()
        outcome = command.run(self._queries, args)
        if isinstance(outcome, Awaitable):
            return await outcome
        return outcome

    def _take_backup(self) -> DebugResult:
        """The one command here that changes something outside the database."""
        if self._backup is None:
            return DebugResult(command="backup", summary="backups are not configured")
        record = self._backup.create(reason="admin_command")
        return DebugResult.of(
            "backup",
            summary="verified backup taken",
            rows=[
                {
                    "path": str(getattr(record, "path", "")),
                    "integrity": getattr(record, "integrity", ""),
                    "size_bytes": getattr(record, "size_bytes", 0),
                }
            ],
        )


def _tokens(text: str) -> tuple[str, ...]:
    stripped = (text or "").strip()
    without_prefix = stripped[len(ADMIN_PREFIX) :].strip()
    return tuple(token for token in without_prefix.split() if token)


__all__ = ["ADMIN_PREFIX", "AdminOutcome", "AdminRouter", "is_admin_command"]
