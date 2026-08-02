"""Belief and self-model tuning values (spec 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class BeliefPolicyError(RuntimeError):
    """Raised when the belief/self policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BeliefRules(_Frozen):
    initial_confidence: float = Field(default=0.40, ge=0.0, le=1.0)
    #: Disconfirming evidence weighs slightly more than confirming evidence.
    contradiction_bias: float = Field(default=1.20, ge=0.0)
    #: Evidence mass at which the prior has been displaced halfway. Smaller
    #: means evidence convinces faster.
    evidence_half_weight: float = Field(default=0.50, gt=0.0)
    contested_ratio: float = Field(default=0.60, ge=0.0, le=1.0)
    abandon_confidence: float = Field(default=0.15, ge=0.0, le=1.0)
    max_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    weight_per_evidence: dict[str, float] = Field(default_factory=dict)

    def weight_for(self, source_type: str) -> float:
        return self.weight_per_evidence.get(source_type, 0.10)


class SelfSchemaRules(_Frozen):
    initial_strength: float = Field(default=0.50, ge=0.0, le=1.0)
    #: Spec 23.2: the self-concept lags behind the behaviour it describes.
    evidence_before_change: float = Field(default=4.0, gt=0)
    strength_step: float = Field(default=0.05, ge=0.0, le=1.0)
    max_change_per_update: float = Field(default=0.06, ge=0.0, le=1.0)
    clarity_from_consistency: float = Field(default=0.80, ge=0.0, le=1.0)
    clarity_floor: float = Field(default=0.10, ge=0.0, le=1.0)


class BeliefSelfPolicy(_Frozen):
    policy_version: int = 1
    belief: BeliefRules = BeliefRules()
    self_schema: SelfSchemaRules = SelfSchemaRules()

    @classmethod
    def load(cls, path: Path | str) -> BeliefSelfPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise BeliefPolicyError(f"belief/self policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise BeliefPolicyError(f"belief/self policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise BeliefPolicyError(f"invalid belief/self policy {policy_path}: {exc}") from exc
