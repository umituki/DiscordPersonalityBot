"""Rendering a debug result for Discord (Phase 5).

Discord truncates at 2000 characters, and a truncated JSON blob is worse than
no answer: it looks like data, reads like data, and is unparseable. So this
paginates at *row* boundaries and says which page you are on. A page is always
a complete thought.

It is also not a character voice. Admin output is monospace, terse and
unmistakably out-of-character — the operator must never be unsure whether they
are reading YUI or reading the database.

Nothing here redacts: :mod:`app.admin.results` did that on the way in, so a
second renderer cannot forget to.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.admin.results import DebugResult, DebugSection

#: Discord's hard limit, with room for the code fence and the page footer.
DISCORD_LIMIT = 2000
_BUDGET = DISCORD_LIMIT - 120

#: How many rows go on one page before it is split regardless of length.
MAX_ROWS_PER_PAGE = 20


class DiscordAdminFormatter:
    """Turns a :class:`DebugResult` into one or more sendable messages."""

    name = "discord_admin_formatter"

    def format(self, result: DebugResult) -> tuple[str, ...]:
        """Pages, in order. Always at least one."""
        if result.failed:
            return (f"```\n[{result.command}] error\n{result.error}\n```",)

        header = f"[{result.command}]"
        if result.summary:
            header += f" {result.summary}"

        lines: list[str] = []
        for section in result.sections:
            lines.extend(self._section_lines(section))
        if not lines:
            lines.append("(no rows)")

        pages = self._paginate(lines)
        if len(pages) == 1:
            return (_fence(f"{header}\n" + pages[0]),)
        return tuple(
            _fence(f"{header}  (page {index}/{len(pages)})\n" + page)
            for index, page in enumerate(pages, start=1)
        )

    # --- internals -----------------------------------------------------------
    @staticmethod
    def _section_lines(section: DebugSection) -> list[str]:
        lines: list[str] = []
        if section.is_empty:
            return lines
        if section.title:
            lines.append(f"-- {section.title}")
        if section.note:
            lines.append(section.note)
        for row in section.rows:
            lines.append(_row_line(row))
        return lines

    @staticmethod
    def _paginate(lines: Sequence[str]) -> list[str]:
        pages: list[str] = []
        current: list[str] = []
        size = 0
        for line in lines:
            # A single line longer than a whole page is trimmed rather than
            # split: half an identifier is not more useful than a marked cut.
            if len(line) > _BUDGET:
                line = line[: _BUDGET - 1] + "…"
            if current and (size + len(line) + 1 > _BUDGET or len(current) >= MAX_ROWS_PER_PAGE):
                pages.append("\n".join(current))
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        if current:
            pages.append("\n".join(current))
        return pages or [""]


def _row_line(row: Mapping[str, Any]) -> str:
    return "  ".join(f"{key}={_value(value)}" for key, value in row.items())


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    return text if text else "-"


def _fence(body: str) -> str:
    return f"```\n{body}\n```"


__all__ = ["DISCORD_LIMIT", "MAX_ROWS_PER_PAGE", "DiscordAdminFormatter"]
