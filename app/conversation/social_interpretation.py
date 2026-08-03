"""Social Interpretation (rebuild spec 10, Phase 3 §3-§7).

One question, asked once per turn, before any sentence exists: *what does this
message ask of me, socially?*

It replaces :class:`~app.conversation.models.DialogueAct`. The two must not
coexist — the same decision held in two places is the setup for a system where
the prose obeys one and the guard checks the other.

What it is deliberately not: a hundred numbers. The temptation is a schema like
``empathy=0.63, warmth=0.72, question_probability=0.28``, and that is the exact
failure the appraisal redesign already fixed — a model inventing precision it
does not have, on a scale nobody can defend. Every field here is a small closed
set of meanings.

What it is also not: a place to decide the USER's feelings. ``user_state_hint``
is a *hint*, and its values say so — ``possibly_negative``, not ``sad``. The
difference reaches the reply as 「それはちょっときつそう」 rather than
「つらかったんだね」, which is the difference between reading someone and telling
them how they feel.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage
from app.conversation.text import looks_like_question

logger = logging.getLogger(__name__)

PROMPT_ID = "social_interpretation"
PURPOSE = "social_interpretation"

#: What this turn is mainly doing (§4).
PrimaryMove = Literal[
    "acknowledge",
    "answer",
    "react",
    "support",
    "clarify",
    "repair",
    "self_disclose",
    "share_thought",
    "continue_story",
    "topic_shift",
    "close_softly",
]

Initiative = Literal["low", "balanced", "high"]

#: §19: never a probability. How much a question is *called for*.
QuestionNeed = Literal["none", "optional", "useful", "necessary"]

Tone = Literal["neutral", "light", "serious", "gentle", "playful"]

ResponseEnergy = Literal["very_low", "low", "normal", "high"]

TopicDirection = Literal["stay", "expand", "shift_softly", "close"]

#: §6: a hint, never a verdict. There is deliberately no ``user_is_sad``.
UserStateHint = Literal[
    "unknown", "possibly_positive", "possibly_negative", "possibly_tired", "mixed"
]

#: §21. How much of herself this turn offers. Bounded by grounding: an
#: experience she cannot evidence is not hers to disclose (§22).
SelfDisclosure = Literal["none", "light", "moderate"]

MOVE_DESCRIPTIONS: dict[str, str] = {
    "acknowledge": "相手の言ったことを受け止める",
    "answer": "聞かれたことに答える",
    "react": "聞いて感じたことを返す",
    "support": "しんどさの側に立つ",
    "clarify": "わからなかったところだけ確かめる",
    "repair": "自分の言ったことを訂正する",
    "self_disclose": "自分のことを少し話す",
    "share_thought": "自分の考えを出す",
    "continue_story": "相手の話の続きを一緒に進める",
    "topic_shift": "話題を移す",
    "close_softly": "この話を静かに終わらせる",
}


class SocialInterpretation(BaseModel):
    """How this turn is read, socially (§3).

    The model proposes it; Python owns what happens next, and the Common Ground
    correction authority can override it (§7).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary_move: PrimaryMove = "acknowledge"
    secondary_move: PrimaryMove | None = None
    initiative: Initiative = "balanced"
    question: QuestionNeed = "none"
    tone: Tone = "neutral"
    response_energy: ResponseEnergy = "normal"
    topic_direction: TopicDirection = "stay"
    user_state_hint: UserStateHint = "unknown"
    self_disclosure: SelfDisclosure = "none"
    reason: str = ""
    #: Provenance, not a model field: where this reading came from.
    source: Literal["llm", "default", "correction"] = Field(
        default="default", exclude=True
    )

    @property
    def moves(self) -> tuple[str, ...]:
        if self.secondary_move is None or self.secondary_move == self.primary_move:
            return (self.primary_move,)
        return (self.primary_move, self.secondary_move)

    @property
    def is_repair(self) -> bool:
        return "repair" in self.moves

    def render(self) -> str:
        """What the realizer is told. Intentions, never sentences."""
        lines = [f"- {MOVE_DESCRIPTIONS[move]}" for move in self.moves]
        lines.append(f"- 声の感じ: {self.tone}")
        lines.append(f"- こちらから出る度合い: {self.initiative}")
        lines.append(f"- 話題の扱い: {self.topic_direction}")
        if self.user_state_hint != "unknown":
            lines.append(
                f"- 相手の様子はたぶん {self.user_state_hint}。"
                "決めつけず、そう見えるという範囲で扱う"
            )
        if self.self_disclosure != "none":
            lines.append(f"- 自分のことを話す量: {self.self_disclosure}")
        return "\n".join(lines)

    @classmethod
    def minimal(cls, *, direct_question: bool = False) -> SocialInterpretation:
        """The safe reading when the model could not produce one.

        Acknowledge, and invent no intention — no question, no topic shift, no
        disclosure. A direct question is the one exception, because ignoring
        one is its own failure.
        """
        if direct_question:
            return cls(
                primary_move="answer",
                initiative="balanced",
                question="none",
                response_energy="normal",
                source="default",
            )
        return cls(
            primary_move="acknowledge",
            initiative="low",
            question="none",
            response_energy="low",
            source="default",
        )

    def with_correction(self, note: str) -> SocialInterpretation:
        """Common Ground is the authority on correction (§7).

        The interpreter may read 「違うよ」 as anything it likes; if the tracker
        has already retracted a claim, this turn is a repair. Leaving that to
        the model would put the same decision in two places, and the one with
        the evidence would not be the one that won.
        """
        if not note:
            return self
        return self.model_copy(
            update={
                "primary_move": "repair",
                "secondary_move": (
                    None if self.primary_move == "repair" else self.primary_move
                ),
                "question": "none",
                "topic_direction": "stay",
                "self_disclosure": "none",
                "source": "correction",
            }
        )


class SocialInterpreter:
    """Produces the one social reading of a turn."""

    name = "social_interpreter"

    def __init__(
        self,
        *,
        identity,
        prompts: PromptRegistry,
        structured: StructuredGenerator,
    ) -> None:
        self._identity = identity
        self._prompts = prompts
        self._structured = structured

    async def interpret(
        self,
        *,
        user_text: str,
        recent_conversation: str,
        state_summary: str = "",
        common_ground: str = "",
        relationship_band: str = "acquaintance",
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> SocialInterpretation:
        template = self._prompts.get(PROMPT_ID)
        content = template.render(
            identity=self._identity.render_for_prompt(),
            user_message=user_text,
            recent_conversation=recent_conversation or "(まだやりとりはない)",
            state_summary=state_summary or "(とくに強い状態はない)",
            common_ground=common_ground or "(この会話でまだ前提になっていることはない)",
            relationship_band=relationship_band,
        )
        outcome = await self._structured.generate(
            SocialInterpretation,
            (LLMMessage(role="user", content=content),),
            purpose=PURPOSE,
            run_id=run_id,
            event_id=event_id,
            temperature=0.4,
            max_tokens=300,
            # §33: the USER is waiting on this before a single token is written.
            priority="P0",
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if outcome.accepted and outcome.value is not None:
            return outcome.value.model_copy(update={"source": "llm"})
        logger.info(
            "social interpretation unavailable event_id=%s; using the minimal reading",
            event_id,
        )
        return SocialInterpretation.minimal(
            direct_question=looks_like_question(user_text)
        )


__all__ = [
    "MOVE_DESCRIPTIONS",
    "Initiative",
    "PrimaryMove",
    "QuestionNeed",
    "ResponseEnergy",
    "SelfDisclosure",
    "SocialInterpretation",
    "SocialInterpreter",
    "Tone",
    "TopicDirection",
    "UserStateHint",
]
