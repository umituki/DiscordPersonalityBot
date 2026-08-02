"""Small text predicates shared by the conversation path.

Deliberately conservative: these decide *fallback* behaviour and guard checks,
so a false positive costs more than a miss. Patch spec 10 is explicit that
high-false-positive semantic judgements must not be forced through regex.
"""

from __future__ import annotations

import re

#: Japanese and Latin question marks, plus the common Japanese sentence-final
#: question particles. Kept to endings so a "か" inside a word does not match.
_QUESTION_MARK = re.compile(r"[?？]\s*$")
_QUESTION_TAIL = re.compile(r"(の|んだ|ん)?(か|かな|かい|の)\s*[?？]?\s*$")
_INTERROGATIVE = re.compile(
    r"(なに|何|なぜ|どうして|どこ|いつ|だれ|誰|どちら|どっち|どんな|どう|いくつ|いくら)"
)


def looks_like_question(text: str) -> bool:
    """Whether this reads as a direct question.

    A question mark is decisive. Without one, an interrogative word plus a
    question-shaped ending is required, so "どうしたの" counts and "どうも" does
    not.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if _QUESTION_MARK.search(stripped):
        return True
    return bool(_INTERROGATIVE.search(stripped) and _QUESTION_TAIL.search(stripped))
