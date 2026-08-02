"""Which phrasings count as factual claims (rebuild spec 15.2).

Patterns are policy, not code (spec 40): the shapes that carry a claim are a
language question and will be retuned against real conversations, while the
rule that an unsupported claim is not sent is not tuneable at all.

A false positive here costs a suppressed reply, which is a real cost. That is
why severity exists: only ``hard`` rules — a sentence that flatly asserts
something happened — can hold a send. Anything the pattern cannot distinguish
from ordinary talk is ``soft``: recorded, visible in the trace, not a gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.grounding.models import CLAIM_KINDS


class GroundingPolicyError(RuntimeError):
    """Raised when the grounding policy is missing or malformed."""


class ClaimRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    patterns: tuple[str, ...] = ()
    severity: str = "hard"

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in CLAIM_KINDS:
            raise ValueError(f"unknown claim kind {value!r}; expected one of {list(CLAIM_KINDS)}")
        return value

    @field_validator("severity")
    @classmethod
    def _known_severity(cls, value: str) -> str:
        if value not in ("hard", "soft"):
            raise ValueError(f"severity must be hard or soft, not {value!r}")
        return value

    def compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(pattern) for pattern in self.patterns)


class GroundingLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_claims: int = Field(default=12, gt=0)


class GroundingPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_version: int = 1
    limits: GroundingLimits = GroundingLimits()
    rules: tuple[ClaimRule, ...] = ()

    @classmethod
    def load(cls, path: Path | str) -> GroundingPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise GroundingPolicyError(f"grounding policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise GroundingPolicyError(f"grounding policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise GroundingPolicyError(f"invalid grounding policy {policy_path}: {exc}") from exc


__all__ = [
    "ClaimRule",
    "GroundingLimits",
    "GroundingPolicy",
    "GroundingPolicyError",
]
