"""Output Guard (spec 1.3, 28.2 identity stage, `.claude/rules/llm.md`).

The guard is the last thing between a generated sentence and the USER. It runs
at the IDENTITY stage of the validation pipeline and rejects:

* claims of a human body or of physical action in the real world,
* claims that a search or tool call succeeded when no Tool Manager result says
  so (spec 17.3, 26),
* leaked internal identifiers or prompt scaffolding (spec 30).

Rejection means silence, not a repaired sentence: it is better to say nothing
than to say something that violates an immutable rule (spec 2.12). Patterns are
policy, not code (spec 40).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.llm.validation import Stage, ValidationContext, ValidationFailure

logger = logging.getLogger(__name__)

#: Extras key through which the Tool Manager reports genuinely succeeded calls.
TOOL_SUCCESS_KEY = "tool_success_ids"


class GuardPolicyError(RuntimeError):
    """Raised when the guard policy file is missing or malformed."""


class RuleSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason_code: str
    patterns: tuple[str, ...] = ()
    virtual_framing: tuple[str, ...] = ()

    def compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(pattern) for pattern in self.patterns)


class GuardLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_reply_chars: int = Field(default=900, gt=0)
    min_reply_chars: int = Field(default=1, ge=0)


class OutputGuardPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_version: int = 1
    limits: GuardLimits = GuardLimits()
    physical_claim: RuleSet
    human_body: RuleSet
    tool_claim: RuleSet
    system_leak: RuleSet

    @classmethod
    def load(cls, path: Path | str) -> OutputGuardPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise GuardPolicyError(f"output guard policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise GuardPolicyError(f"output guard policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise GuardPolicyError(f"invalid output guard policy {policy_path}: {exc}") from exc


@dataclass(frozen=True)
class OutputGuard:
    """Identity-stage validator over a generated reply."""

    policy: OutputGuardPolicy
    field_name: str = "text"
    name: str = "output_guard"
    stage: Stage = Stage.IDENTITY

    def check(self, candidate: Any, context: ValidationContext) -> ValidationFailure | None:
        text = getattr(candidate, self.field_name, None)
        if not isinstance(text, str) or not text.strip():
            return self._fail("empty_reply", "the generated reply is empty")

        stripped = text.strip()
        limits = self.policy.limits
        if len(stripped) > limits.max_reply_chars:
            return self._fail(
                "output_too_long",
                f"reply is {len(stripped)} characters, limit {limits.max_reply_chars}",
            )
        if len(stripped) < limits.min_reply_chars:
            return self._fail("output_too_short", "reply is shorter than the minimum")

        for rule in (self.policy.human_body, self.policy.system_leak):
            hit = _first_match(stripped, rule)
            if hit is not None:
                return self._fail(rule.reason_code, f"matched {hit!r}")

        physical = self.policy.physical_claim
        hit = _first_match(stripped, physical)
        if hit is not None and not _framed_as_virtual(stripped, physical.virtual_framing):
            return self._fail(
                physical.reason_code,
                f"matched {hit!r} without virtual framing (spec 1.3)",
            )

        tool_rule = self.policy.tool_claim
        hit = _first_match(stripped, tool_rule)
        if hit is not None and not _tool_success_reported(context):
            return self._fail(
                tool_rule.reason_code,
                f"matched {hit!r} but the Tool Manager reported no successful call",
            )

        return None

    def _fail(self, reason_code: str, detail: str) -> ValidationFailure:
        logger.warning("output guard rejected reply reason=%s detail=%s", reason_code, detail)
        return ValidationFailure(
            stage=self.stage, reason_code=reason_code, detail=detail, validator=self.name
        )


def _first_match(text: str, rule: RuleSet) -> str | None:
    for pattern in rule.compiled():
        found = pattern.search(text)
        if found is not None:
            return found.group(0)
    return None


def _framed_as_virtual(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


def _tool_success_reported(context: ValidationContext) -> bool:
    """Spec 26: only the Tool Manager can make a tool call true."""
    reported = context.extras.get(TOOL_SUCCESS_KEY)
    return bool(reported)
