"""Conversation tuning values (spec 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.context.builder import ContextBudget


class ConversationPolicyError(RuntimeError):
    """Raised when the conversation policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextPolicy(_Frozen):
    max_tokens: int = Field(default=3000, gt=0)
    chars_per_token: float = Field(default=2.0, gt=0)
    recent_turn_limit: int = Field(default=12, ge=0)
    recent_turn_max_chars: int = Field(default=600, gt=0)

    def budget(self) -> ContextBudget:
        return ContextBudget(max_tokens=self.max_tokens, chars_per_token=self.chars_per_token)


class GenerationPolicy(_Frozen):
    temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    max_tokens: int = Field(default=400, gt=0)
    max_attempts: int = Field(default=2, ge=1)
    timeout_s: float = Field(default=90.0, gt=0)


class ReplyPolicy(_Frozen):
    #: Phase 3 keeps silent when the guard rejects. Regeneration with an
    #: adjusted plan belongs to the Agency phase.
    on_rejection: Literal["suppress"] = "suppress"


class ConversationPolicy(_Frozen):
    policy_version: int = 1
    context: ContextPolicy = ContextPolicy()
    generation: GenerationPolicy = GenerationPolicy()
    reply: ReplyPolicy = ReplyPolicy()

    @classmethod
    def load(cls, path: Path | str) -> ConversationPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise ConversationPolicyError(f"conversation policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ConversationPolicyError(f"conversation policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise ConversationPolicyError(f"invalid conversation policy {policy_path}: {exc}") from exc
