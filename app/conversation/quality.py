"""Conversation Quality Guard (patch spec 10).

Separate from the Output Guard on purpose. The Output Guard answers "may YUI
say this at all" — physical claims, unearned tool claims, internal leakage.
This stage answers a different question: "is this a reply a person would
actually send?"

The two failures it exists to catch were both produced on real hardware:

    USER: 初めまして～
    YUI:  初めまして、はじめまして。

    YUI:  どうしましたか？
    USER: いや、特に用はないんだ。ゆいのことを知ったから、話したくて
    YUI:  どうしたかった？

Patch spec 10 is explicit that high-false-positive semantic judgement must not
be forced through regex. So every check here is either structural (a question
mark where the decision said no question) or a near-exact repetition. Anything
requiring understanding is left alone.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

from app.conversation.models import ConversationTurn
from app.conversation.text import looks_like_question

MODULE = "conversation_quality_guard"


class QualityIssue:
    """Stable reason codes, recorded in ``failures.reason_code``."""

    EMPTY = "empty_reply"
    DUPLICATE_GREETING = "duplicate_greeting"
    SELF_REPETITION = "self_repetition"
    ECHOES_USER = "echoes_user"
    REPEATED_QUESTION = "repeated_question"
    UNWANTED_QUESTION = "unwanted_question"
    FORMULAIC = "formulaic_repetition"


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    accepted: bool
    issues: tuple[str, ...] = ()
    detail: str = ""

    @property
    def rejected(self) -> bool:
        return not self.accepted


#: Spellings that mean the same greeting. Case A was 「初めまして、はじめまして」 —
#: two spellings of one greeting, which is why these are grouped rather than
#: listed: counting each spelling separately finds one of each and nothing
#: wrong.
_GREETING_GROUPS: tuple[tuple[str, ...], ...] = (
    ("初めまして", "はじめまして", "初めまして"),
    ("こんにちは", "こんにちわ", "今日は"),
    ("こんばんは", "こんばんわ", "今晩は"),
    ("おはようございます", "おはよう", "お早う"),
    ("ひさしぶり", "久しぶり", "おひさしぶり", "お久しぶり"),
)


def _greeting_pattern(spellings: tuple[str, ...]) -> re.Pattern[str]:
    """Longest spelling first, so 「おはようございます」 counts once, not twice."""
    ordered = sorted(set(spellings), key=len, reverse=True)
    return re.compile("|".join(re.escape(word) for word in ordered))


_GREETING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (group[0], _greeting_pattern(group)) for group in _GREETING_GROUPS
)

#: Sentences are matched rather than split on, so the terminator stays attached.
#: Splitting on 「？」 throws away the only thing that makes 「調子はどう？」 a
#: question, and the repeated-question check would then never see one.
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]*")


def _normalise(text: str) -> str:
    """Fold width and case so 「はじめまして」 and 「初めまして」 compare fairly."""
    folded = unicodedata.normalize("NFKC", text).strip().lower()
    return re.sub(r"[\s、。,.！？!?~〜ー…]+", "", folded)


def _sentences(text: str) -> list[str]:
    return [match.group(0).strip() for match in _SENTENCE.finditer(text) if match.group(0).strip()]


#: What each reason code means, in the words the repair prompt is given.
_PROBLEM_TEXT: dict[str, str] = {
    QualityIssue.EMPTY: "返事が空になっている。",
    QualityIssue.DUPLICATE_GREETING: "同じ意味の挨拶を二度言っている。ひとつにする。",
    QualityIssue.SELF_REPETITION: "同じ文をくり返している。一度だけにする。",
    QualityIssue.ECHOES_USER: "相手の言葉をほぼそのまま返している。自分の言葉にする。",
    QualityIssue.REPEATED_QUESTION: "直前に自分がした質問をもう一度している。訊き直さない。",
    QualityIssue.UNWANTED_QUESTION: "質問しないと決めたのに質問している。質問文を外す。",
    QualityIssue.FORMULAIC: "決まり文句のくり返しになっている。",
}


class ConversationQualityGuard:
    name = MODULE

    def __init__(
        self,
        *,
        max_echo_ratio: float = 0.6,
        min_echo_chars: int = 8,
        recent_question_window: int = 3,
    ) -> None:
        self._max_echo_ratio = max_echo_ratio
        self._min_echo_chars = min_echo_chars
        self._recent_question_window = recent_question_window

    def review(
        self,
        text: str,
        *,
        allows_question: bool,
        user_text: str,
        recent_turns: Sequence[ConversationTurn] = (),
    ) -> QualityVerdict:
        issues: list[str] = []
        details: list[str] = []

        stripped = text.strip()
        if not stripped:
            return QualityVerdict(False, (QualityIssue.EMPTY,), "the reply is empty")

        greeting = self._duplicate_greeting(stripped)
        if greeting:
            issues.append(QualityIssue.DUPLICATE_GREETING)
            details.append(f"greeting repeated: {greeting}")

        repeated = self._repeated_sentence(stripped)
        if repeated:
            issues.append(QualityIssue.SELF_REPETITION)
            details.append(f"sentence repeated: {repeated}")

        if self._echoes_user(stripped, user_text):
            issues.append(QualityIssue.ECHOES_USER)
            details.append("the reply is mostly the USER's own words")
        else:
            echoed = self._echoes_recent_user(stripped, recent_turns)
            if echoed:
                issues.append(QualityIssue.ECHOES_USER)
                details.append("the reply replays a recent USER turn")

        if not allows_question and looks_like_question(stripped):
            # Patch spec 10.4 and 8.1, now spending the SurfacePlan's question
            # budget (Phase 3 §19). The decision said no question; asking one
            # anyway means the prose and the decision disagree.
            issues.append(QualityIssue.UNWANTED_QUESTION)
            details.append("a question was asked although the decision said not to")

        asked_again = self._repeated_question(stripped, recent_turns)
        if asked_again:
            issues.append(QualityIssue.REPEATED_QUESTION)
            details.append(f"already asked recently: {asked_again}")

        stock = self._formulaic(stripped, recent_turns)
        if stock:
            issues.append(QualityIssue.FORMULAIC)
            details.append(f"stock phrase across turns: {stock}")

        if issues:
            return QualityVerdict(False, tuple(issues), "; ".join(details))
        return QualityVerdict(True)

    @staticmethod
    def describe(verdict: QualityVerdict) -> str:
        """The rejection, written for the repair prompt rather than for a log."""
        lines = [
            f"- {_PROBLEM_TEXT[issue]}" for issue in verdict.issues if issue in _PROBLEM_TEXT
        ]
        return "\n".join(lines) if lines else "- 不自然な返事になっている。"

    # --- checks -------------------------------------------------------------
    @staticmethod
    def _duplicate_greeting(text: str) -> str | None:
        """One greeting said twice in a single reply (Case A).

        Different spellings of the same greeting count as the same greeting,
        which is exactly the case that got through on real hardware.
        """
        normalised = _normalise(text)
        for canonical, pattern in _GREETING_PATTERNS:
            if len(pattern.findall(normalised)) >= 2:
                return canonical
        return None

    @staticmethod
    def _repeated_sentence(text: str) -> str | None:
        seen: set[str] = set()
        for sentence in _sentences(text):
            key = _normalise(sentence)
            if len(key) < 4:
                continue
            if key in seen:
                return sentence
            seen.add(key)
        return None

    def _echoes_user(self, text: str, user_text: str) -> bool:
        """The reply is largely the USER's message read back to them."""
        reply = _normalise(text)
        user = _normalise(user_text)
        if len(user) < self._min_echo_chars or not reply:
            return False
        if user not in reply:
            return False
        return len(user) / len(reply) >= self._max_echo_ratio

    def _echoes_recent_user(
        self, text: str, recent_turns: Sequence[ConversationTurn]
    ) -> str | None:
        """A prior USER sentence is still theirs on the next turn.

        The current-message echo check cannot see a model copying USER-1 while
        answering USER-2.  That exact failure made a correction sentence come
        back under YUI's name during the real-machine gate.
        """
        recent_user = [turn for turn in recent_turns if turn.speaker == "user"]
        for turn in reversed(recent_user[-self._recent_question_window :]):
            if self._echoes_user(text, turn.content):
                return turn.content
        return None

    def _formulaic(self, text: str, recent_turns: Sequence[ConversationTurn]) -> str | None:
        """The same stock sentence turn after turn (patch spec 10.7).

        Once is a habit; every turn is a template. The threshold is two
        previous turns rather than one so a natural 「うん」-shaped echo does
        not count.
        """
        current = {
            _normalise(sentence): sentence
            for sentence in _sentences(text)
            if len(_normalise(sentence)) >= 6
        }
        if not current:
            return None

        recent_yui = [turn for turn in recent_turns if turn.speaker == "yui"]
        # A full sentence repeated in two consecutive YUI turns is already a
        # stuck response, even though a short acknowledgement such as 「うん」
        # may naturally occur twice.  The old two-previous-turn threshold let
        # the real model answer two different USER turns with the same long
        # sentence before the guard could react.
        if recent_yui:
            previous = {
                _normalise(sentence): sentence
                for sentence in _sentences(recent_yui[-1].content)
                if len(_normalise(sentence)) >= 8
            }
            for key in current:
                if len(key) >= 8 and key in previous:
                    return current[key]

        counts: dict[str, int] = {}
        for turn in recent_yui[-self._recent_question_window :]:
            for key in {_normalise(sentence) for sentence in _sentences(turn.content)}:
                if key in current:
                    counts[key] = counts.get(key, 0) + 1
        for key, count in counts.items():
            if count >= 2:
                return current[key]
        return None

    def _repeated_question(
        self, text: str, recent_turns: Sequence[ConversationTurn]
    ) -> str | None:
        """Asking again what YUI already asked in the last few turns."""
        if not looks_like_question(text):
            return None
        questions = {
            _normalise(sentence)
            for sentence in _sentences(text)
            if looks_like_question(sentence)
        }
        if not questions:
            return None

        recent_yui = [turn for turn in recent_turns if turn.speaker == "yui"]
        for turn in recent_yui[-self._recent_question_window :]:
            for sentence in _sentences(turn.content):
                if not looks_like_question(sentence):
                    continue
                previous = _normalise(sentence)
                if len(previous) < 4:
                    continue
                if previous in questions:
                    return sentence
        return None
