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
from app.conversation.common_ground import detect_correction
from app.conversation.text import looks_like_question
from app.dialogue.response_contract import QuestionPolicy

MODULE = "conversation_quality_guard"


class QualityIssue:
    """Stable reason codes, recorded in ``failures.reason_code``."""

    EMPTY = "empty_reply"
    DUPLICATE_GREETING = "duplicate_greeting"
    SELF_REPETITION = "self_repetition"
    ECHOES_USER = "echoes_user"
    REPEATED_QUESTION = "repeated_question"
    UNWANTED_QUESTION = "unwanted_question"
    #: The opposite failure, and deliberately not the same code. "asked when
    #: told not to" and "did not ask when told to" call for opposite repairs,
    #: and a trace that cannot tell them apart cannot say which happened.
    REQUIRED_QUESTION_MISSING = "required_question_missing"
    FORMULAIC = "formulaic_repetition"
    CORRECTION_ARGUMENT = "correction_doubled_down"
    DIRECT_ANSWER_MISSING = "direct_answer_missing"


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
    QualityIssue.REQUIRED_QUESTION_MISSING: (
        "このターンでは質問が必要だと決めたのに、返信に質問が含まれていない。"
        "話の流れに合う質問をひとつ加える。"
    ),
    QualityIssue.FORMULAIC: "決まり文句のくり返しになっている。",
    QualityIssue.CORRECTION_ARGUMENT: (
        "相手の訂正に反論している。説明で押し切らず、短く認めて訂正を受け入れる。"
    ),
    QualityIssue.DIRECT_ANSWER_MISSING: (
        "安全な文章にはなっているが、USERが尋ねた対象へ答えていない。"
        "分からない場合も、分からない範囲を質問への答えとして明示する。"
    ),
}


_CORRECTION_ARGUMENT = re.compile(
    r"(?:(?:わたし|私)[^。！？!?\n]{0,32}?(?:思っていた|記憶して|覚えている|言っていない)|"
    r"(?:誤解|勘違い)[^。！？!?\n]{0,16}?(?:あった|ある)(?:の|ん)?(?:でしょう|ですか))"
)


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
        question_policy: str | None = None,
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

        if (
            detect_correction(user_text).is_denial
            and _CORRECTION_ARGUMENT.search(stripped)
        ):
            issues.append(QualityIssue.CORRECTION_ARGUMENT)
            details.append("the reply argues with an explicit USER correction")

        # The ResponseContract's question policy has four states, and until now
        # only one of them did anything here: `forbidden` was checked and the
        # other three all meant "accept". So a turn whose contract *required* a
        # question was satisfied by a reply containing none — the state existed
        # in the contract and had no effect at the boundary that enforces it.
        #
        # All four are decided here, from the one policy value, structurally.
        # Nothing new reads the Japanese: `looks_like_question` is the same
        # detector the forbidden case has always used, and what changed is what
        # the policy *means*, not how a question is recognised.
        policy = self._question_policy(question_policy, allows_question=allows_question)
        asks = looks_like_question(stripped)
        if policy is QuestionPolicy.FORBIDDEN and asks:
            # Patch spec 10.4 and 8.1, now spending the SurfacePlan's question
            # budget (Phase 3 §19). The decision said no question; asking one
            # anyway means the prose and the decision disagree.
            issues.append(QualityIssue.UNWANTED_QUESTION)
            details.append("a question was asked although the decision said not to")
        elif policy is QuestionPolicy.REQUIRED and not asks:
            issues.append(QualityIssue.REQUIRED_QUESTION_MISSING)
            details.append("the turn requires a question and the reply asks none")
        # OPTIONAL and ENCOURAGED accept either way. `encouraged` is a
        # preference the realizer is told about, not an obligation — rejecting a
        # good reply for declining a suggestion would make "encouraged" a second
        # spelling of "required", and then the contract would have three states.

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
    def _question_policy(
        question_policy: str | None, *, allows_question: bool
    ) -> QuestionPolicy:
        """The contract's policy, or the boolean that predates it.

        The ResponseContract is the authority whenever one reached here. The
        `allows_question` fallback exists for callers written before the
        contract — it can only distinguish two states, so it never yields
        `required`: a caller that could not express the obligation has not
        imposed one.
        """
        if question_policy is not None:
            try:
                return QuestionPolicy(question_policy)
            except ValueError:
                # An unrecognised policy is not a licence to skip the check.
                # Fall through to the conservative boolean reading.
                pass
        return QuestionPolicy.OPTIONAL if allows_question else QuestionPolicy.FORBIDDEN

    @staticmethod
    def with_contract_result(
        verdict: QualityVerdict, *, fulfilled: bool, detail: str = ""
    ) -> QualityVerdict:
        """Merge the answer-fulfilment check into the one quality verdict."""
        if fulfilled:
            return verdict
        issues = tuple(dict.fromkeys((*verdict.issues, QualityIssue.DIRECT_ANSWER_MISSING)))
        details = "; ".join(
            part
            for part in (verdict.detail, detail or "the direct answer obligation was not met")
            if part
        )
        return QualityVerdict(False, issues, details)

    @staticmethod
    def describe(verdict: QualityVerdict) -> str:
        """The rejection, written for the repair prompt rather than for a log.

        The per-issue text says what kind of failure it was; the detail says
        what this particular reply was judged to be. Both matter to a rewrite:
        "you did not answer" and "you addressed memory under an identity
        question" call for different second attempts, and the first on its own
        is what led a model to conclude it had to invent a value.
        """
        lines = [
            f"- {_PROBLEM_TEXT[issue]}" for issue in verdict.issues if issue in _PROBLEM_TEXT
        ]
        if not lines:
            lines = ["- 不自然な返事になっている。"]
        if verdict.detail:
            lines.append(f"- 判定の詳細: {verdict.detail}")
        return "\n".join(lines)

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
        reply = _normalise(text)
        recent_user = [turn for turn in recent_turns if turn.speaker == "user"]
        for turn in reversed(recent_user[-self._recent_question_window :]):
            if self._echoes_user(text, turn.content):
                return turn.content
            for sentence in _sentences(turn.content):
                sentence_key = _normalise(sentence)
                if len(sentence_key) >= self._min_echo_chars and sentence_key in reply:
                    return sentence
                if self._echoes_user(text, sentence):
                    return sentence
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
