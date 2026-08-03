"""Naturalness measurement (Phase 3 §36, §37, §43).

Deliberately **not** a runtime guard. Phase 1 narrowed the Output Guard to hard
detections only, and that decision stands: a reply that is slightly awkward,
repeats a word, runs short or comes out too casual is not unsafe, and a guard
that rejects it is a guard that teaches the system to write blandly.

So these are measurements. They score a transcript after the fact, and the
numbers are for deciding whether a prompt change helped — not for deciding
whether a sentence may be sent. Nothing here is imported by the reply path, and
a test asserts that.

The two failure modes being measured are the ones the running system actually
produced: a reply that ends in a question every single turn, and a reply that
opens the same way every single turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.conversation.text import looks_like_question

MODULE = "naturalness_evaluation"

#: §37. What is measured about a stretch of conversation.
METRICS: tuple[str, ...] = (
    "japanese_naturalness",
    "turn_appropriateness",
    "question_necessity",
    "response_length",
    "repetition",
    "relationship_register",
    "self_disclosure_balance",
    "topic_flow",
    "human_likeness",
)

#: §43. Stock assistant phrasing. Measured, never forbidden: any one of these is
#: a perfectly ordinary thing to say once. Saying them constantly is the tell.
AI_TELLS: tuple[str, ...] = (
    "についてどう思いますか",
    "それは興味深いですね",
    "興味深いです",
    "何か他に話したいこと",
    "お手伝いできること",
    "いかがでしょうか",
    "参考になれば",
    "承知しました",
)

_OPENING = re.compile(r"^[^、。！？!?\s]{1,6}")

#: How much longer than the USER's message a reply may run before it reads as a
#: lecture. Five is generous; the real failure was ten and more.
LENGTH_RATIO_LIMIT = 5.0


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange, for scoring."""

    user: str
    reply: str
    #: Whether a question was actually called for. Supplied by the scenario, not
    #: guessed from the text — the whole question is whether the *reply* matched
    #: what the turn needed.
    question_needed: bool = False


@dataclass(frozen=True, slots=True)
class NaturalnessReport:
    """Rates, not verdicts."""

    turns: int = 0
    question_rate: float = 0.0
    unnecessary_questions: int = 0
    necessary_questions_missed: int = 0
    repeated_opening_rate: float = 0.0
    overlong_rate: float = 0.0
    ai_tell_rate: float = 0.0
    longest_question_streak: int = 0
    most_common_opening: str = ""

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "turns": self.turns,
            "question_rate": round(self.question_rate, 3),
            "unnecessary_questions": self.unnecessary_questions,
            "necessary_questions_missed": self.necessary_questions_missed,
            "repeated_opening_rate": round(self.repeated_opening_rate, 3),
            "overlong_rate": round(self.overlong_rate, 3),
            "ai_tell_rate": round(self.ai_tell_rate, 3),
            "longest_question_streak": self.longest_question_streak,
            "most_common_opening": self.most_common_opening,
        }


def evaluate(turns: Sequence[Turn]) -> NaturalnessReport:
    """Score a stretch of conversation.

    §41: a fixed 「question rate <= 30%」 is not the target, because the right
    rate depends on what was asked. What is counted separately is questions that
    were not called for and questions that were called for and not asked.
    """
    if not turns:
        return NaturalnessReport()

    questions = 0
    unnecessary = 0
    missed = 0
    overlong = 0
    tells = 0
    openings: list[str] = []
    streak = 0
    longest = 0

    for turn in turns:
        reply = turn.reply.strip()
        asked = looks_like_question(reply)
        if asked:
            questions += 1
            streak += 1
            longest = max(longest, streak)
            if not turn.question_needed:
                unnecessary += 1
        else:
            streak = 0
            if turn.question_needed:
                missed += 1

        user_length = max(1, len(turn.user.strip()))
        if len(reply) > user_length * LENGTH_RATIO_LIMIT and len(reply) > 40:
            overlong += 1

        if any(tell in reply for tell in AI_TELLS):
            tells += 1

        match = _OPENING.search(reply)
        if match is not None:
            openings.append(match.group(0))

    total = len(turns)
    common_opening, repeated = _most_common(openings)
    return NaturalnessReport(
        turns=total,
        question_rate=questions / total,
        unnecessary_questions=unnecessary,
        necessary_questions_missed=missed,
        repeated_opening_rate=repeated / total if total else 0.0,
        overlong_rate=overlong / total,
        ai_tell_rate=tells / total,
        longest_question_streak=longest,
        most_common_opening=common_opening,
    )


def _most_common(openings: Sequence[str]) -> tuple[str, int]:
    if not openings:
        return "", 0
    counts: dict[str, int] = {}
    for opening in openings:
        counts[opening] = counts.get(opening, 0) + 1
    phrase = max(counts, key=lambda key: (counts[key], key))
    return phrase, counts[phrase]


__all__ = [
    "AI_TELLS",
    "LENGTH_RATIO_LIMIT",
    "METRICS",
    "NaturalnessReport",
    "Turn",
    "evaluate",
]
