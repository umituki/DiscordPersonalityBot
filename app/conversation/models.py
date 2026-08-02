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


class DialogueAct(BaseModel):
    """What kind of response this will be — decided before any sentence.

    Spec 16.1: ``文章を直接最適化せず、先に Dialogue Act を決める``. The reply is then
    generated to carry out these acts, rather than the acts being read back out
    of whatever the model happened to write.
    """

    acknowledge: bool = False
    validate_feeling: bool = Field(default=False, alias="validate")
    self_disclose: bool = False
    ask_followup: bool = False
    humor: bool = False
    challenge: bool = False
    topic_shift: bool = False
    goal: ConversationGoal = "maintain_connection"

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @property
    def chosen(self) -> tuple[str, ...]:
        names = (
            ("acknowledge", self.acknowledge),
            ("validate", self.validate_feeling),
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
            "self_disclose": "自分のことを少し話す",
            "ask_followup": "質問を返す",
            "humor": "少しだけ軽くする",
            "challenge": "違う見方を示す",
            "topic_shift": "話題を変える",
        }
        lines = [f"- {descriptions[name]}" for name in self.chosen]
        lines.append(f"- この会話でのねらい: {self.goal}")
        return "\n".join(lines)

    @classmethod
    def minimal(cls) -> DialogueAct:
        """The safe fallback when the acts could not be decided."""
        return cls(acknowledge=True, goal="maintain_connection")


class ReplyDraft(BaseModel):
    """The sentence itself, generated to carry out already-chosen acts."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
