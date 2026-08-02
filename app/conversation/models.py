"""Conversation domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

Speaker = Literal["user", "yui"]
ChannelType = Literal["direct_message", "guild_text", "test"]


class ConversationTurn(BaseModel):
    """One recorded utterance.

    This is a projection of the event stream, kept for cheap recent-history
    reads. The events remain authoritative (spec 2.9) and the projection can be
    rebuilt from them.
    """

    model_config = ConfigDict(frozen=True)

    turn_id: str
    conversation_id: str
    event_id: str
    speaker: Speaker
    author_id: str | None
    content: str
    occurred_at: datetime
    message_ref: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    def as_prompt_line(self, user_label: str = "USER", yui_label: str = "YUI") -> str:
        label = user_label if self.speaker == "user" else yui_label
        return f"{label}: {self.content}"


class Conversation(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversation_id: str
    channel_id: str
    channel_type: ChannelType
    started_at: datetime
    last_activity_at: datetime
    turn_count: int = Field(default=0, ge=0)
    status: Literal["active", "idle", "closed"] = "active"

    @field_validator("started_at", "last_activity_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)


#: Spec 16.2 conversation goals.
ConversationGoal = Literal[
    "understand_user",
    "share_experience",
    "have_fun",
    "seek_support",
    "give_support",
    "maintain_connection",
    "solve_problem",
    "explore",
    "pass_time",
    "repair",
]


#: Patch spec 8. What kind of exchange this turn is. A conversation with no
#: errand is a normal conversation, and ``smalltalk`` exists so the system can
#: say so rather than treating every turn as a task to resolve.
DialogueMode = Literal[
    "smalltalk",
    "answer",
    "support",
    "explore",
    "repair",
    "task",
    "leave_space",
]

#: Patch spec 8. Whether a question is called for at all. The 2026-08-02
#: conversation asked one every turn, including asking the USER what they
#: wanted after they had just said they wanted nothing in particular.
QuestionNeed = Literal["none", "optional", "needed"]

#: How much of the turn is YUI's own contribution rather than a response.
#: ``low`` reciprocity with ``low`` initiative is how a companion ends up
#: making the USER carry the whole conversation (patch spec 8.1).
Balance = Literal["low", "balanced", "high"]


class DialogueAct(BaseModel):
    """What kind of response this will be — decided before any sentence.

    Spec 16.1: ``文章を直接最適化せず、先に Dialogue Act を決める``. The reply is then
    generated to carry out these acts, rather than the acts being read back out
    of whatever the model happened to write.
    """

    acknowledge: bool = False
    validate_feeling: bool = Field(default=False, alias="validate")
    #: Patch spec 8: answering a direct question is its own act. Without it,
    #: the only way to respond to a question was ``ask_followup``, which is how
    #: a question came back for every question.
    answer: bool = False
    self_disclose: bool = False
    ask_followup: bool = False
    humor: bool = False
    challenge: bool = False
    topic_shift: bool = False
    goal: ConversationGoal = "maintain_connection"
    #: Patch spec 8.
    mode: DialogueMode = "smalltalk"
    question_need: QuestionNeed = "optional"
    reciprocity: Balance = "balanced"
    initiative: Balance = "balanced"

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @property
    def wants_question(self) -> bool:
        """Whether this turn is allowed to contain a question at all.

        Patch spec 8.1: ``question_need=none`` / ``ask_followup=false`` なら
        原則質問しない. The guard checks the produced text against this.
        """
        if self.question_need == "none":
            return False
        return self.ask_followup or self.question_need == "needed"

    @property
    def chosen(self) -> tuple[str, ...]:
        names = (
            ("acknowledge", self.acknowledge),
            ("validate", self.validate_feeling),
            ("answer", self.answer),
            ("self_disclose", self.self_disclose),
            ("ask_followup", self.ask_followup),
            ("humor", self.humor),
            ("challenge", self.challenge),
            ("topic_shift", self.topic_shift),
        )
        return tuple(name for name, active in names if active)

    @property
    def is_empty(self) -> bool:
        return not self.chosen

    def render(self) -> str:
        """The instruction block handed to the reply prompt."""
        descriptions = {
            "acknowledge": "相手の言ったことを受け止める",
            "validate": "相手の感じ方を否定しない",
            "answer": "聞かれたことに答える",
            "self_disclose": "自分のことを少し話す",
            "ask_followup": "質問を返す",
            "humor": "少しだけ軽くする",
            "challenge": "違う見方を示す",
            "topic_shift": "話題を変える",
        }
        lines = [f"- {descriptions[name]}" for name in self.chosen]
        lines.append(f"- この会話のかたち: {self.mode}")
        lines.append(f"- この会話でのねらい: {self.goal}")
        if not self.wants_question:
            # Stated positively and last, because this is the instruction the
            # 2026-08-02 replies kept violating (patch spec 8.1).
            lines.append("- 質問はしない。相手に用件を尋ね返さない")
        elif self.question_need == "needed":
            lines.append("- 必要なことをひとつだけ尋ねる")
        if self.reciprocity == "high" or self.initiative == "high":
            lines.append("- 相手に話題を出させるのではなく、自分からも差し出す")
        return "\n".join(lines)

    @classmethod
    def minimal(cls, *, direct_question: bool = False) -> DialogueAct:
        """The safe fallback when the acts could not be decided.

        Patch spec 3.5: acknowledge, and do not invent an intention — no
        follow-up question, no challenge, no topic shift. A direct question is
        the one exception: ignoring it would be its own kind of failure, so the
        fallback answers instead of merely acknowledging.
        """
        if direct_question:
            return cls(
                acknowledge=True,
                answer=True,
                goal="understand_user",
                mode="answer",
                question_need="none",
            )
        return cls(
            acknowledge=True,
            goal="maintain_connection",
            mode="smalltalk",
            question_need="none",
        )


class ReplyDraft(BaseModel):
    """The sentence itself, generated to carry out already-chosen acts."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
