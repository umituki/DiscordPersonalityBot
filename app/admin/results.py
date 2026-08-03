"""Typed debug results, redacted at construction (rebuild spec 30, Phase 5).

Every admin answer is one of these. Commands do not format, and they do not
return raw rows: a raw row is how a bot token ends up in a Discord channel
because somebody added a column six months later.

Redaction happens *here*, on the way in, rather than in the formatter. A result
that has been built cannot contain a secret, so a second output path — a log
line, a test dump, a future web view — cannot leak one by forgetting to call
the sanitiser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: What replaces a redacted value. Deliberately obvious in a channel.
REDACTED = "[redacted]"

#: Field names whose *value* is never shown, whatever it happens to contain.
#: Matched case-insensitively as a substring, because the real shapes are
#: ``discord_bot_token``, ``DISCORD_BOT_TOKEN`` and ``api_key_id`` alike.
SECRET_FIELD_MARKERS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "auth_header",
    "private_key",
    "session_key",
    "webhook",
    "dsn",
)

#: Fields that carry model instructions or raw generated scaffolding. Spec 30
#: and Phase 1 §16: the prompt is never shown, in character or out of it.
PROMPT_FIELD_MARKERS: tuple[str, ...] = (
    "prompt_text",
    "system_prompt",
    "request_transcript",
    "response_text",
    "rendered",
    "messages",
    "instruction",
)

#: Values that look like a credential even in a field with an innocent name.
_SECRET_VALUE = re.compile(
    r"(?:sk-[A-Za-z0-9]{16,}|Bot\s+[A-Za-z0-9._-]{40,}|[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27})"
)

#: Longest single value shown. A debug field is a fact, not a document.
MAX_VALUE_CHARS = 400


def redact(name: str, value: Any) -> Any:
    """One field, made safe to print."""
    lowered = name.lower()
    if any(marker in lowered for marker in SECRET_FIELD_MARKERS):
        return REDACTED
    if any(marker in lowered for marker in PROMPT_FIELD_MARKERS):
        return REDACTED
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return REDACTED
        if len(value) > MAX_VALUE_CHARS:
            return value[:MAX_VALUE_CHARS] + "…"
    if isinstance(value, Mapping):
        return {key: redact(str(key), item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(name, item) for item in value]
    return value


def safe_row(row: Any, *, fields: Sequence[str] | None = None) -> dict[str, Any]:
    """A database row, reduced to named fields and redacted.

    ``fields`` is required in practice by every caller: a debug view should say
    which columns it means, so a migration that adds one does not silently
    start printing it.
    """
    if fields is None:
        keys = list(row.keys()) if hasattr(row, "keys") else []
    else:
        keys = list(fields)
    result: dict[str, Any] = {}
    for key in keys:
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            value = getattr(row, key, None)
        result[key] = redact(key, value)
    return result


@dataclass(frozen=True, slots=True)
class DebugSection:
    """One block of an answer: a title and some already-safe rows."""

    title: str
    rows: tuple[dict[str, Any], ...] = ()
    #: Free text for a section that is a note rather than a table.
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.note


@dataclass(frozen=True, slots=True)
class DebugResult:
    """What an admin command answers with.

    It is data, not a rendered string. The formatter decides how it looks in
    Discord; a future web view or CLI can render the same object differently
    without either of them re-implementing redaction.
    """

    command: str
    sections: tuple[DebugSection, ...] = ()
    summary: str = ""
    #: Set when the command could not answer. An admin error is an admin error
    #: — it never becomes a psychological failure or a character reply.
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return bool(self.error)

    @classmethod
    def of(
        cls,
        command: str,
        *,
        summary: str = "",
        rows: Iterable[Mapping[str, Any]] = (),
        title: str = "",
        **meta: Any,
    ) -> DebugResult:
        """Build a one-section result from already-safe rows."""
        materialised = tuple(dict(row) for row in rows)
        sections = (
            (DebugSection(title=title or command, rows=materialised),)
            if materialised
            else ()
        )
        return cls(command=command, sections=sections, summary=summary, meta=meta)

    @classmethod
    def failure(cls, command: str, message: str) -> DebugResult:
        return cls(command=command, error=message)


__all__ = [
    "MAX_VALUE_CHARS",
    "PROMPT_FIELD_MARKERS",
    "REDACTED",
    "SECRET_FIELD_MARKERS",
    "DebugResult",
    "DebugSection",
    "redact",
    "safe_row",
]
