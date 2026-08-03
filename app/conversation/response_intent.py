"""Response Intent and intentional silence (rebuild spec 12, Phase 4).

Whether to speak at all is a different question from what the turn asks of her,
and it is decided separately and later.

    SocialInterpretation   what does this turn ask of me?
    ResponseIntent         do I actually perform a speech act?

The division of authority is the whole design. The model says what it feels
like doing — it rides along in the social interpretation rather than costing a
second call — and Python decides, with a veto the model cannot argue with.

    NORMAL_REPLY          answer properly
    BRIEF_REPLY           answer, small
    LEAVE_SPACE           answer, and leave room rather than pushing
    INTENTIONAL_SILENCE   say nothing, on purpose

Only the last sends nothing, and it is a **decision**, not a failure. It must
never be confused with :data:`YUI_REPLY_SUPPRESSED` (a draft that was refused),
an LLM timeout, a validation failure or a Discord error. Those are things that
went wrong; this is something she chose.

Silence is also never the fallback. Every degraded path here lands on
``NORMAL_REPLY``: a system that goes quiet when it is confused is
indistinguishable from a broken one, and the USER cannot tell which they have.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from app.conversation.social_interpretation import SocialInterpretation
from app.conversation.text import looks_like_question

logger = logging.getLogger(__name__)


class ResponseIntent(str, Enum):
    """What she is actually going to do (spec 12.1)."""

    NORMAL_REPLY = "NORMAL_REPLY"
    BRIEF_REPLY = "BRIEF_REPLY"
    LEAVE_SPACE = "LEAVE_SPACE"
    INTENTIONAL_SILENCE = "INTENTIONAL_SILENCE"

    @property
    def speaks(self) -> bool:
        """Whether a message is going to be produced and sent."""
        return self is not ResponseIntent.INTENTIONAL_SILENCE


#: Spec 12.2. Reasons a turn *must* be answered. Each is a case where silence
#: would not read as restraint — it would read as being ignored, or broken.
class VetoReason(str, Enum):
    DIRECT_QUESTION = "direct_question"
    EXPLICIT_REQUEST = "explicit_request"
    CORRECTION = "correction"
    SUPPORT_NEEDED = "support_needed"
    URGENT = "urgent"
    MOVE_REQUIRES_SPEECH = "move_requires_speech"


#: Moves that are speech acts by definition. Deciding to answer and then not
#: answering is not restraint, it is a contradiction.
_SPEAKING_MOVES = frozenset({"answer", "clarify", "repair", "support"})

#: 「〜して」「〜してほしい」「お願い」 — an explicit request.
_REQUEST = re.compile(
    r"(?:お願い|おねがい|頼む|たのむ|して(?:ほしい|くれ|ください|くださ)|"
    r"教えて|おしえて|してみて|やって)"
)

#: Something that cannot wait.
_URGENT = re.compile(r"(?:助けて|たすけて|緊急|至急|やばい|まずい|危な)")

#: Low-information acknowledgements. A turn that is only one of these is a turn
#: where saying nothing is a normal thing a person does.
_BACKCHANNELS = frozenset(
    {
        "うん", "うんうん", "そう", "そうだね", "そうなんだ", "なるほど", "ふーん",
        "はい", "ええ", "おk", "ok", "オッケー", "了解", "りょうかい", "わかった",
        "だね", "ね", "へー", "ほう", "まあね", "そっか",
    }
)

#: How long a message can be and still count as a bare acknowledgement.
_BACKCHANNEL_MAX_CHARS = 8


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """What was decided, and by whom (spec 12.2)."""

    intent: ResponseIntent
    #: ``llm`` when the model's inclination stood, ``veto`` when Python
    #: overrode it, ``default`` when there was nothing usable to work from.
    source: str = "default"
    #: Set only when a veto fired. This is the audit trail for "why did she
    #: answer when the model wanted to stay quiet".
    veto: VetoReason | None = None
    #: What the model wanted, kept even when it was overruled.
    proposed: ResponseIntent | None = None
    reason: str = ""

    @property
    def speaks(self) -> bool:
        return self.intent.speaks

    @property
    def is_silence(self) -> bool:
        return self.intent is ResponseIntent.INTENTIONAL_SILENCE

    @property
    def was_vetoed(self) -> bool:
        return self.veto is not None

    def render(self) -> str:
        """For the debug preview. Never for the reply prompt — the realizer is
        only ever asked to write when the answer is already 'speak'."""
        lines = [f"intent: {self.intent.value}", f"source: {self.source}"]
        if self.proposed is not None and self.proposed is not self.intent:
            lines.append(f"proposed: {self.proposed.value}")
        if self.veto is not None:
            lines.append(f"veto: {self.veto.value}")
        if self.reason:
            lines.append(f"reason: {self.reason}")
        return "\n".join(lines)


#: What the model's inclination maps to before Python has its say.
_INCLINATIONS: dict[str, ResponseIntent] = {
    "speak": ResponseIntent.NORMAL_REPLY,
    "brief": ResponseIntent.BRIEF_REPLY,
    "leave_space": ResponseIntent.LEAVE_SPACE,
    "silence": ResponseIntent.INTENTIONAL_SILENCE,
}


class ResponseIntentGate:
    """Python authority over whether a speech act happens (spec 12.2).

    The gate never invents an intention to speak *more* than the model wanted;
    it only ever raises the floor. A veto turns silence into a reply, never a
    brief reply into a long one.
    """

    name = "response_intent_gate"

    def decide(
        self,
        social: SocialInterpretation,
        *,
        user_text: str,
        correction: str = "",
        conversation_turns: int = 0,
    ) -> IntentDecision:
        proposed = _INCLINATIONS.get(social.wants_to_speak)
        if proposed is None:
            # Nothing usable. Answer — a confused system that goes quiet looks
            # exactly like a broken one.
            return IntentDecision(
                intent=ResponseIntent.NORMAL_REPLY,
                source="default",
                reason="発話意図を読み取れなかった",
            )

        veto = self._veto(social, user_text=user_text, correction=correction)
        if veto is not None and not proposed.speaks:
            return IntentDecision(
                intent=ResponseIntent.NORMAL_REPLY,
                source="veto",
                veto=veto,
                proposed=proposed,
                reason=f"沈黙できない理由がある: {veto.value}",
            )

        if proposed.speaks:
            return IntentDecision(
                intent=proposed,
                source="llm",
                proposed=proposed,
                reason=social.reason[:200],
            )

        eligible = self._silence_is_natural(social, user_text=user_text)
        if not eligible:
            # No veto, but nothing about the turn makes silence natural either.
            # Speaking briefly is the honest middle.
            return IntentDecision(
                intent=ResponseIntent.BRIEF_REPLY,
                source="veto",
                proposed=proposed,
                reason="黙るほどの理由がない",
            )

        return IntentDecision(
            intent=ResponseIntent.INTENTIONAL_SILENCE,
            source="llm",
            proposed=proposed,
            reason=social.reason[:200] or "会話が自然に閉じている",
        )

    # --- the veto (spec 12.2) ----------------------------------------------
    @staticmethod
    def _veto(
        social: SocialInterpretation, *, user_text: str, correction: str
    ) -> VetoReason | None:
        text = (user_text or "").strip()
        if correction:
            # Phase 1 owns this: something she said has been retracted, and
            # leaving that unsaid is worse than anything she could say.
            return VetoReason.CORRECTION
        if _URGENT.search(text):
            return VetoReason.URGENT
        if looks_like_question(text):
            return VetoReason.DIRECT_QUESTION
        if _REQUEST.search(text):
            return VetoReason.EXPLICIT_REQUEST
        if social.primary_move in _SPEAKING_MOVES:
            return VetoReason.MOVE_REQUIRES_SPEECH
        if social.user_state_hint in ("possibly_negative", "possibly_tired") and (
            len(text) > _BACKCHANNEL_MAX_CHARS
        ):
            # Someone who has just said something heavy is not helped by being
            # left on read.
            return VetoReason.SUPPORT_NEEDED
        return None

    # --- when silence is a normal thing to do (spec 12.2) -------------------
    @staticmethod
    def _silence_is_natural(
        social: SocialInterpretation, *, user_text: str
    ) -> bool:
        text = (user_text or "").strip().rstrip("。！!、 ")
        if text in _BACKCHANNELS or (
            len(text) <= _BACKCHANNEL_MAX_CHARS
            and any(text.startswith(word) for word in _BACKCHANNELS)
        ):
            return True
        if social.topic_direction == "close":
            return True
        if social.primary_move == "close_softly":
            return True
        return social.response_energy == "very_low"


__all__ = [
    "IntentDecision",
    "ResponseIntent",
    "ResponseIntentGate",
    "VetoReason",
]
