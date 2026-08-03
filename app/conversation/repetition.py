"""Surface repetition monitor (Phase 3 §18, §17).

The 「ふむ」 problem: a character sheet that says she says 「ふむ」 produces a bot
that says 「ふむ」 every single turn. A trait should show up as a *distribution*,
not as a rule, and the way to get that is to tell the realizer what it has been
doing lately rather than to forbid anything.

So this is not a guard. It rejects nothing. It looks at the last handful of her
own turns, notices what has been repeating, and hands the realizer a note.
People do repeat their own backchannels; asking for zero repetition would be
its own kind of unnatural. What is worth noticing is three in a row.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from app.conversation.models import ConversationTurn
from app.conversation.text import looks_like_question

#: How many of her own turns to look back over (§18).
DEFAULT_WINDOW = 12

#: How many repeats before it is worth mentioning. Two is a coincidence.
DEFAULT_THRESHOLD = 3

_OPENING = re.compile(r"^[^、。！？!?\s]{1,6}")
_CLOSING = re.compile(r"[^、。！？!?\s]{1,6}[。！？!?]?$")


@dataclass(frozen=True, slots=True)
class StyleHints:
    """What she has been overusing. Advice, never a rule."""

    openings: tuple[str, ...] = ()
    closings: tuple[str, ...] = ()
    question_streak: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.openings or self.closings or self.question_streak >= 3)

    def render(self) -> str:
        if self.is_empty:
            return ""
        lines: list[str] = []
        for phrase in self.openings:
            lines.append(f"- 最近「{phrase}」で始めることが続いている")
        for phrase in self.closings:
            lines.append(f"- 最近「{phrase}」で終わることが続いている")
        if self.question_streak >= 3:
            lines.append(
                f"- 直近 {self.question_streak} 回続けて質問で終わっている"
            )
        lines.append("- 避けろという意味ではない。同じ形が続いていることだけ伝えている")
        return "\n".join(lines)


class SurfaceRepetitionMonitor:
    """Reads her recent turns and reports what is repeating (§18)."""

    name = "surface_repetition_monitor"

    def __init__(
        self,
        *,
        window: int = DEFAULT_WINDOW,
        threshold: int = DEFAULT_THRESHOLD,
    ) -> None:
        self._window = window
        self._threshold = threshold

    def review(self, recent_turns: Sequence[ConversationTurn]) -> StyleHints:
        mine = [
            turn.content.strip()
            for turn in list(recent_turns)[-self._window :]
            if turn.speaker == "yui" and turn.content.strip()
        ]
        if len(mine) < self._threshold:
            return StyleHints()

        return StyleHints(
            openings=self._overused(mine, _OPENING),
            closings=self._overused(mine, _CLOSING),
            question_streak=self._question_streak(mine),
        )

    def _overused(self, turns: Sequence[str], pattern: re.Pattern[str]) -> tuple[str, ...]:
        found = Counter(
            match.group(0)
            for match in (pattern.search(turn) for turn in turns)
            if match is not None
        )
        return tuple(
            phrase
            for phrase, count in found.most_common(3)
            if count >= self._threshold
        )

    @staticmethod
    def _question_streak(turns: Sequence[str]) -> int:
        streak = 0
        for turn in reversed(turns):
            if not looks_like_question(turn):
                break
            streak += 1
        return streak


__all__ = ["DEFAULT_THRESHOLD", "DEFAULT_WINDOW", "StyleHints", "SurfaceRepetitionMonitor"]
