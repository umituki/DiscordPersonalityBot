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
    #: Phase 3 §34. The realizer may be warmer than a judgement call: it needs
    #: variety, and grounding is what keeps the extra freedom honest. Too high
    #: and every other reply needs a repair, so the real value comes from
    #: measurement on hardware.
    realizer_temperature: float = Field(default=0.85, ge=0.0, le=2.0)
    #: Dialogue v2. Repair is a *constrained rewrite*, not composition: the
    #: intention is already fixed, the facts are supplied, and the failed
    #: claims are named. Variety buys nothing there and costs accuracy, so this
    #: is deliberately below the realizer's.
    #:
    #: The value is a candidate, not a measurement. It is a separate knob so a
    #: fixture run can compare it against the realizer's 0.85 — which is what
    #: it silently reused before — and the real-machine tuning comes later.
    repair_temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    #: Phase 3 §25. Two to five examples. More and the model copies them.
    reference_limit: int = Field(default=4, ge=0, le=5)
    max_tokens: int = Field(default=400, gt=0)
    max_attempts: int = Field(default=2, ge=1)
    timeout_s: float = Field(default=90.0, gt=0)


class QualityPolicy(_Frozen):
    """Conversation Quality Guard thresholds (patch spec 10).

    Tuning, not code constants: how much of a reply may be the USER's own words
    before it reads as an echo is a judgement that will move.
    """

    #: Fraction of the reply that may be the USER's message read back.
    max_echo_ratio: float = Field(default=0.6, gt=0.0, le=1.0)
    #: Below this length an echo is just agreement, not parroting.
    min_echo_chars: int = Field(default=8, gt=0)
    #: How many previous YUI turns count as "just now".
    recent_question_window: int = Field(default=3, ge=1)


class ReplyPolicy(_Frozen):
    #: Phase 3 keeps silent when the guard rejects. Regeneration with an
    #: adjusted plan belongs to the Agency phase.
    on_rejection: Literal["suppress"] = "suppress"


class ConversationPolicy(_Frozen):
    policy_version: int = 1
    context: ContextPolicy = ContextPolicy()
    generation: GenerationPolicy = GenerationPolicy()
    reply: ReplyPolicy = ReplyPolicy()
    quality: QualityPolicy = QualityPolicy()

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
