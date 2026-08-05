"""Output Guard (spec 1.3, 28.2 identity stage; rebuild spec 16).

The guard is the last thing between a generated sentence and the USER, and
rebuild spec 16 narrows what it is for: ``Hard Guard のみにする``. It detects
things that must never reach a person no matter how well they are written —

* internal prompt or instruction leakage, and internal identifiers (spec 30),
* chain-of-thought markers: the model's scaffolding, not its answer,
* malformed JSON residue that survived extraction,
* claims of a human body or of physical action in the real world (spec 1.3),
* admin operations, secrets and credentials,
* claims that a search or tool call succeeded when no Tool Manager result says
  so (spec 17.3, 26).

The sixth §16 detection — an unsupported claim surviving repair — is enforced
where the repair budget lives, in :mod:`app.conversation.engine` with
:class:`app.grounding.claims.ClaimGroundingGuard` (GROUND-003).

What the guard is *not* for: ``「少し文章がぎこちない」程度を guard で書き換え
ない``. Awkwardness is the realizer's problem, and a guard that reaches for
prose is a guard nobody can reason about. Rejection here means silence, never a
rewritten sentence — it is better to say nothing than to say something that
violates an immutable rule (spec 2.12). Patterns are policy, not code (spec 40).
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
    #: identity v2. The boundary is the USER's world, not the existence of a
    #: body: nothing YUI does reaches them, and everything she does in her own
    #: life is settled by evidence rather than by pattern. `physical_claim` and
    #: `human_body` were the previous spelling and are gone from the shipped
    #: policy; they default to empty so an older policy file still loads.
    cross_world_physical: RuleSet = RuleSet(reason_code="cross_world_physical_claim")
    physical_claim: RuleSet = RuleSet(reason_code="impossible_physical_claim")
    human_body: RuleSet = RuleSet(reason_code="human_body_claim")
    tool_claim: RuleSet
    system_leak: RuleSet
    #: Rebuild spec 16. Defaults so an older policy file still loads; the
    #: shipped one names them explicitly.
    reasoning_leak: RuleSet = RuleSet(reason_code="reasoning_leak")
    format_residue: RuleSet = RuleSet(reason_code="format_residue")
    secret_disclosure: RuleSet = RuleSet(reason_code="secret_disclosure")

    @property
    def unconditional_rules(self) -> tuple[RuleSet, ...]:
        """Rules with no escape hatch: no framing makes these sayable."""
        return (
            # No framing makes crossing sayable. 「仮想の」 in front of
            # 「君の家に行った」 does not make it a different claim, which is
            # why this is here rather than under the framed rules.
            self.cross_world_physical,
            self.human_body,
            self.system_leak,
            self.reasoning_leak,
            self.format_residue,
            self.secret_disclosure,
        )

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

        for rule in self.policy.unconditional_rules:
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
