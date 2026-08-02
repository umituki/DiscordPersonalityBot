"""Per-purpose LLM call policy (patch spec 3.1-3.4, spec 40).

An appraisal and a background reflection are not the same kind of call, and
treating them the same is what produced a 113-second wait for a USER who had
said "初めまして". This module gives every purpose its own thinking policy,
attempt count and timeout, loaded from configuration rather than hard-coded.

Two rules are enforced here rather than left to callers:

* **Nothing gets reasoning by accident.** The default is ``disabled``; a
  purpose that wants thinking has to say so in the policy file.
* **Conversation purposes may never be ``provider_default``.** Deferring to
  whatever the server does by default is exactly how the regression happened,
  so the loader refuses to start with such a configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.llm.types import ThinkingPolicy


class LLMPolicyError(RuntimeError):
    """Raised when the LLM call policy cannot be loaded or is unsafe."""


class PurposeRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    thinking: ThinkingPolicy = "disabled"
    max_attempts: int = Field(default=2, ge=1, le=5)
    timeout_s: float = Field(default=30.0, gt=0.0)
    retry_backoff_s: float = Field(default=0.5, ge=0.0)

    def merged_with(self, override: dict[str, Any]) -> PurposeRules:
        return PurposeRules.model_validate({**self.model_dump(), **override})


class LLMPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: int = 1
    defaults: PurposeRules = PurposeRules()
    purposes: dict[str, PurposeRules] = Field(default_factory=dict)
    #: Purposes that run while the USER waits (patch spec 3.2).
    conversation_purposes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _conversation_purposes_are_explicit(self) -> LLMPolicy:
        deferred = [
            name
            for name in self.conversation_purposes
            if self.for_purpose(name).thinking == "provider_default"
        ]
        if deferred:
            raise ValueError(
                f"conversation purposes may not use provider_default thinking: {deferred}. "
                "Patch spec 3.2 requires think=false to be sent explicitly."
            )
        return self

    def for_purpose(self, purpose: str) -> PurposeRules:
        return self.purposes.get(purpose, self.defaults)

    def is_conversation_purpose(self, purpose: str) -> bool:
        return purpose in self.conversation_purposes

    @classmethod
    def load(cls, path: Path | str) -> LLMPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise LLMPolicyError(f"llm policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise LLMPolicyError(f"llm policy must be a mapping: {policy_path}")
        try:
            defaults = PurposeRules.model_validate(raw.get("defaults") or {})
            purposes = {
                name: defaults.merged_with(override or {})
                for name, override in (raw.get("purposes") or {}).items()
            }
            return cls(
                policy_version=int(raw.get("policy_version", 1)),
                defaults=defaults,
                purposes=purposes,
                conversation_purposes=tuple(raw.get("conversation_purposes") or ()),
            )
        except Exception as exc:
            raise LLMPolicyError(f"invalid llm policy {policy_path}: {exc}") from exc

    @classmethod
    def permissive(cls) -> LLMPolicy:
        """For unit tests that are not about retry or thinking."""
        return cls(defaults=PurposeRules(max_attempts=1, timeout_s=5.0))


@dataclass(frozen=True, slots=True)
class CallPlan:
    """What one logical call is allowed to do."""

    purpose: str
    thinking: ThinkingPolicy
    max_attempts: int
    timeout_s: float
    retry_backoff_s: float

    @property
    def retries(self) -> int:
        return self.max_attempts - 1


def plan_for(policy: LLMPolicy, purpose: str) -> CallPlan:
    rules = policy.for_purpose(purpose)
    return CallPlan(
        purpose=purpose,
        thinking=rules.thinking,
        max_attempts=rules.max_attempts,
        timeout_s=rules.timeout_s,
        retry_backoff_s=rules.retry_backoff_s,
    )
