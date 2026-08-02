"""Recall modes (rebuild spec 17.4, Phase 2 §2D).

Remembering is not one operation. 「何歳？」 is a lookup of a fact about
oneself; 「本の話をしてたら昔のことを思い出した」 is one memory pulling another
in; ordinary chat is neither. They differ in what should be looked for, how
strict the relevance bar is, and how many memories may surface at all.

The mode is decided *before* candidate generation, because it changes what
counts as a candidate. It is a closed set for the same reason the appraisal
categories are: a string that arrives from somewhere else is a mode nobody
designed.
"""

from __future__ import annotations

import re
from enum import Enum


class RecallMode(str, Enum):
    """Why memory is being consulted at all."""

    #: Ordinary conversation. Something may come to mind; nothing has to.
    CONVERSATIONAL = "CONVERSATIONAL"
    #: A direct question about her own life — age, birth, first memory. She is
    #: looking something up about herself, so the bar is different.
    AUTOBIOGRAPHICAL_FACT = "AUTOBIOGRAPHICAL_FACT"
    #: One memory reminding her of another, from cues rather than a query.
    ASSOCIATIVE = "ASSOCIATIVE"
    #: Deliberately thinking back over a stretch of life.
    REFLECTIVE = "REFLECTIVE"
    #: Reading the diary. The only mode in which diary entries are material
    #: (Phase 2 test 14) — everywhere else the diary is not a memory substitute.
    DIARY_READING = "DIARY_READING"
    #: Re-checking something she claimed, for the correction path (spec 11).
    #: Separated so a correction check is never mistaken for remembering.
    CORRECTION_CHECK = "CORRECTION_CHECK"

    @property
    def is_deliberate(self) -> bool:
        """Whether she is *trying* to remember.

        This is what separates ``consciously_recalled`` from a memory that
        merely happened to be in context (§2J, §2K). Ordinary conversation is
        not an act of recollection; being asked her age is.
        """
        return self in (
            RecallMode.AUTOBIOGRAPHICAL_FACT,
            RecallMode.REFLECTIVE,
            RecallMode.DIARY_READING,
        )


#: Questions that are asking about her own life rather than about a topic.
#: Kept from the conceptual-bridge work: 「何歳」「年齢」「生まれ」「最初の記憶」.
#: Its role here is narrower than it was — it decides the *mode*, and later
#: helps generate candidates. It no longer decides that anything is recalled
#: (§2G): Stage 2 does that.
_AUTOBIOGRAPHICAL = re.compile(
    r"(?:何歳|なんさい|歳は|年齢|いくつ|生まれ|誕生日|出身|"
    r"最初の記憶|一番古い記憶|子供の頃|子どもの頃|昔の(?:こと|話))"
)

#: Looking back over a stretch rather than asking one fact.
_REFLECTIVE = re.compile(
    r"(?:印象に残って|思い出深い|振り返(?:る|って)|これまでで|今までで|一番.*?だった)"
)

_DIARY = re.compile(r"(?:日記|ダイアリー)を?(?:読|見|振り返)")


def classify(user_text: str) -> RecallMode:
    """Which kind of remembering this message asks for.

    Deliberately shallow and deterministic. The mode only shapes the search;
    getting it wrong costs a differently-sized candidate pool, not a false
    memory, because Stage 2 still has to find the candidates relevant.
    """
    text = (user_text or "").strip()
    if not text:
        return RecallMode.CONVERSATIONAL
    if _DIARY.search(text):
        return RecallMode.DIARY_READING
    if _AUTOBIOGRAPHICAL.search(text):
        return RecallMode.AUTOBIOGRAPHICAL_FACT
    if _REFLECTIVE.search(text):
        return RecallMode.REFLECTIVE
    return RecallMode.CONVERSATIONAL


__all__ = ["RecallMode", "classify"]
