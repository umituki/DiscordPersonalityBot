"""LLM request/response value objects.

The model itself is hidden behind these types (spec 3.1: ``モデルは Interface の
背後に隠蔽する``). Business logic never sees an Ollama payload.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

Role = Literal["system", "user", "assistant"]

#: Spec 33 queue priorities, scheduled by the Resource Manager.
CallPriority = Literal["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7"]

#: Patch spec 3.1. Whether the model may spend tokens reasoning before it
#: answers. Thinking models otherwise leak reasoning into ordinary structured
#: tasks, which is what turned a 5-second appraisal into a 113-second one.
#:
#: ``provider_default`` is forbidden in normal conversation (patch spec 3.2):
#: leaving it to the provider is how the problem went unnoticed.
ThinkingPolicy = Literal["disabled", "enabled", "provider_default"]


class LLMMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Role
    content: str


class LLMRequest(BaseModel):
    """One model call. Immutable, hashable, and traceable."""

    model_config = ConfigDict(frozen=True)

    messages: tuple[LLMMessage, ...]
    #: What this call is for, e.g. ``appraisal`` or ``conversation_reply``.
    purpose: str
    model: str | None = None
    #: JSON schema the model must satisfy. ``None`` means free-form text.
    format_schema: dict[str, Any] | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    num_ctx: int | None = Field(default=None, gt=0)
    stop: tuple[str, ...] = ()
    timeout_s: float | None = Field(default=None, gt=0)
    priority: CallPriority = "P3"
    #: Patch spec 3.1-3.2. Defaults to ``disabled``: a caller that wants
    #: reasoning has to ask for it, so no purpose acquires it by accident.
    thinking: ThinkingPolicy = "disabled"
    #: Which attempt this is, for telemetry (patch spec 19.1).
    attempt: int = Field(default=1, ge=1)
    #: Stable across the retries of one logical call, so queue time and
    #: attempts can be summed per decision rather than per HTTP request.
    logical_call_id: str | None = None
    #: Prompt provenance for spec 29 reproducibility.
    prompt_id: str | None = None
    prompt_version: str | None = None

    @field_validator("messages")
    @classmethod
    def _non_empty(cls, value: tuple[LLMMessage, ...]) -> tuple[LLMMessage, ...]:
        if not value:
            raise ValueError("an LLM request needs at least one message")
        return value

    @property
    def structured(self) -> bool:
        return self.format_schema is not None

    def with_messages(self, messages: tuple[LLMMessage, ...]) -> LLMRequest:
        return self.model_copy(update={"messages": messages})

    def append(self, *messages: LLMMessage) -> LLMRequest:
        return self.with_messages(self.messages + messages)

    def fingerprint(self) -> str:
        """Stable hash of everything that determines the model's input."""
        material = json.dumps(
            {
                "messages": [message.model_dump() for message in self.messages],
                "model": self.model,
                "format_schema": self.format_schema,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "num_ctx": self.num_ctx,
                "stop": list(self.stop),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def transcript(self, *, max_chars: int = 4000) -> str:
        """Bounded rendering of the input, for the call trace."""
        rendered = json.dumps(
            [message.model_dump() for message in self.messages], ensure_ascii=False
        )
        return rendered if len(rendered) <= max_chars else rendered[:max_chars] + "…[truncated]"


class LLMResponse(BaseModel):
    """A raw model answer. Not yet trusted for anything."""

    model_config = ConfigDict(frozen=True)

    text: str
    model: str
    created_at: datetime
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    done_reason: str | None = None
    truncated: bool = False
    #: Patch spec 3.3: reasoning is measured, never stored. Only these three
    #: facts about it may reach a production trace.
    thinking_enabled: bool = False
    thinking_present: bool = False
    thinking_char_count: int = 0
    #: Server-side timings, in nanoseconds as Ollama reports them, converted
    #: to milliseconds here (patch spec 19.1).
    total_duration_ms: int | None = None
    load_duration_ms: int | None = None
    prompt_eval_duration_ms: int | None = None
    eval_duration_ms: int | None = None
    #: How long this call waited for a model slot before it started.
    queue_wait_ms: int | None = None

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens
