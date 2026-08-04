"""What this turn means (Dialogue Understanding & Grounding v2).

One model call, before anything is retrieved or decided, that reads the USER's
message *in the context of the conversation it arrived in* and says what it is
about. Everything downstream — the memory query, the situation context, the
semantic claim review, the repair — is better for having it, and each of those
was previously guessing at it separately.

The concrete failures this closes:

    「詩？」        a memory query of "詩？" retrieves nothing useful, because
                  the query is one word and the referent is in the last turn.
    「それは？」    same, with no content word at all.
    「今日は何してた？」
                  the *reply* to this is a claim about today's activity, and
                  nothing downstream could know that from the reply alone.

**This is not an authority.** Nothing here is a fact. `referenced_action_owner`
saying "user" does not make it so — it is a reading, used to interpret and to
retrieve, and every actual truth question is settled against rows. The type is
deliberately separate from `GroundingContext` so that no code path can confuse
"the model thinks this turn is about YUI's day" with "YUI had a day on record".
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage

logger = logging.getLogger(__name__)

PROMPT_ID = "turn_understanding"
PURPOSE = "turn_understanding"

#: What the USER is doing with this turn.
UserIntent = Literal[
    "ask_about_yui",
    "ask_about_world",
    "share_own_experience",
    "continue_topic",
    "correct_or_challenge",
    "acknowledge",
    "greet",
    "request_action",
    "unclear",
]

#: What a question is asking after. `yui_activity` and `yui_identity` are
#: separated because they need opposite handling: the first has to be grounded
#: in an Activity row, and the second is a fixed self fact that has nothing to
#: do with memory recall. Conflating them is what turned 「19歳です」 into a
#: memory claim.
QuestionTarget = Literal[
    "none",
    "yui_activity",
    "yui_identity",
    "yui_feeling",
    "yui_opinion",
    "yui_memory",
    "user_fact",
    "world_fact",
    "unclear",
]

#: Whose action the turn is talking about. The single most important field
#: here: 「詠んだよ」 is the USER's doing, and a reply that treats it as shared
#: is a fabrication.
ActionOwner = Literal["yui", "user", "npc", "nobody", "unclear"]

TemporalScope = Literal[
    "now", "today", "yesterday", "recent_past", "distant_past", "habitual",
    "future", "unspecified",
]


class TurnUnderstanding(BaseModel):
    """The model's reading of one turn. Interpretation only, never evidence."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: What the conversation is currently about, in a few words.
    current_topic: str = Field(default="", max_length=120)
    user_intent: UserIntent = "unclear"
    question_target: QuestionTarget = "none"
    #: Whose action the USER's message refers to.
    referenced_action_owner: ActionOwner = "unclear"
    #: The thing being talked about, with ellipsis resolved where possible.
    referenced_subject: str = Field(default="", max_length=120)
    temporal_scope: TemporalScope = "unspecified"
    #: The USER's message rewritten with pronouns and omissions filled in from
    #: the conversation. 「詩？」 → 「さっき言っていた詩のこと？」. Used to build the
    #: memory query; never shown to the USER.
    resolved_message: str = Field(default="", max_length=300)
    #: What a correction is aimed at, when the USER is pushing back. Free text
    #: describing the claim, matched against live claims by Python.
    correction_target: str = Field(default="", max_length=200)
    #: What would actually be worth recalling for this turn, if anything.
    memory_query: str = Field(default="", max_length=200)
    #: Whether recalling something is relevant at all. A greeting does not need
    #: the memory system woken up.
    wants_memory: bool = False
    #: Whether it would be natural for YUI to say something about herself here.
    self_disclosure_relevant: bool = False
    reason: str = Field(default="", max_length=300)

    @property
    def asks_about_yuis_day(self) -> bool:
        """The 「今日は何してた？」 shape, which makes the answer a claim."""
        return self.question_target == "yui_activity"

    @property
    def is_identity_question(self) -> bool:
        """Answered from self facts, not from memory recall."""
        return self.question_target == "yui_identity"

    @property
    def user_owns_the_action(self) -> bool:
        return self.referenced_action_owner == "user"

    def render(self) -> str:
        """For a prompt. Labelled as a reading, because that is what it is."""
        lines = [
            f"- 話題: {self.current_topic or '不明'}",
            f"- USERの意図: {self.user_intent}",
            f"- 質問の対象: {self.question_target}",
            f"- 言及されている行為の主体: {self.referenced_action_owner}",
            f"- 言及対象: {self.referenced_subject or '不明'}",
            f"- 時間範囲: {self.temporal_scope}",
        ]
        if self.resolved_message:
            lines.append(f"- 省略を補ったUSER発話: {self.resolved_message}")
        if self.correction_target:
            lines.append(f"- 訂正の対象: {self.correction_target}")
        return "\n".join(lines)

    def retrieval_query(self, user_text: str) -> str:
        """What to actually search memory for.

        Falls back to the raw message, because a failed interpretation must not
        cost her the ability to recall anything. The interpretation improves the
        query; it is not load-bearing for it.
        """
        for candidate in (self.memory_query, self.resolved_message):
            if candidate and candidate.strip():
                return candidate.strip()
        return user_text


#: What every consumer gets when interpretation could not run. Explicitly a
#: value rather than ``None``: an unavailable interpretation must degrade the
#: turn, not crash it, and callers should not each invent a fallback.
UNREAD = TurnUnderstanding(reason="interpretation unavailable")


class DiscourseInterpreter:
    """Reads one turn in the context of the conversation around it."""

    name = "discourse_interpreter"

    def __init__(
        self,
        *,
        prompts: PromptRegistry,
        structured: StructuredGenerator,
    ) -> None:
        self._prompts = prompts
        self._structured = structured

    async def read(
        self,
        user_text: str,
        *,
        recent_conversation: str = "",
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> TurnUnderstanding:
        """Interpret, or return ``UNREAD``.

        Never raises and never blocks the turn. Interpretation failing means
        downstream stages fall back to what they did before — a raw-text memory
        query, an unresolved reply — which is worse, not broken.
        """
        template = self._prompts.get(PROMPT_ID)
        content = template.render(
            user_message=user_text,
            recent_conversation=recent_conversation or "(まだやりとりはない)",
        )
        outcome = await self._structured.generate(
            TurnUnderstanding,
            (LLMMessage(role="user", content=content),),
            purpose=PURPOSE,
            run_id=run_id,
            event_id=event_id,
            # Reading, not writing. Nothing here benefits from variety.
            temperature=0.0,
            max_tokens=600,
            priority="P0",
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if not outcome.accepted or outcome.value is None:
            logger.info(
                "turn understanding unavailable event_id=%s reason=%s",
                event_id,
                "unknown" if outcome.failure is None else outcome.failure.reason_code,
            )
            return UNREAD
        return outcome.value


__all__ = [
    "ActionOwner",
    "DiscourseInterpreter",
    "PROMPT_ID",
    "PURPOSE",
    "QuestionTarget",
    "TemporalScope",
    "TurnUnderstanding",
    "UNREAD",
    "UserIntent",
]
