"""Historical knowledge tuning values (spec 21, 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class KnowledgePolicyError(RuntimeError):
    """Raised when the knowledge policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BuilderRules(_Frozen):
    require_available_from: bool = True
    max_candidates_per_job: int = Field(default=200, ge=1)
    default_truth_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    interest_salience_bonus: float = Field(default=0.25, ge=0.0, le=1.0)


class ExposureRules(_Frozen):
    reach_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    attention_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    attention_from_salience: float = Field(default=0.45, ge=0.0, le=1.0)
    attention_from_interest: float = Field(default=0.55, ge=0.0, le=1.0)
    curiosity_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    curiosity_from_interest: float = Field(default=0.60, ge=0.0, le=1.0)
    comprehension_threshold: float = Field(default=0.40, ge=0.0, le=1.0)
    comprehension_from_interest: float = Field(default=0.20, ge=0.0, le=1.0)
    comprehension_penalty_from_complexity: float = Field(default=0.55, ge=0.0, le=1.0)
    encoding_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    encoding_from_attention: float = Field(default=0.40, ge=0.0, le=1.0)
    encoding_from_comprehension: float = Field(default=0.40, ge=0.0, le=1.0)
    encoding_from_novelty: float = Field(default=0.20, ge=0.0, le=1.0)
    retention_threshold: float = Field(default=0.35, ge=0.0, le=1.0)


class RetentionRules(_Frozen):
    fade_per_year_stable: float = Field(default=0.02, ge=0.0)
    fade_per_year_changeable: float = Field(default=0.08, ge=0.0)
    fade_per_year_volatile: float = Field(default=0.20, ge=0.0)
    forgotten_below: float = Field(default=0.15, ge=0.0, le=1.0)

    def fade_for(self, stability: str) -> float:
        return {
            "STABLE": self.fade_per_year_stable,
            "CHANGEABLE": self.fade_per_year_changeable,
            "VOLATILE": self.fade_per_year_volatile,
        }.get(stability, self.fade_per_year_changeable)


class KnowledgePolicy(_Frozen):
    policy_version: int = 1
    builder: BuilderRules = BuilderRules()
    exposure: ExposureRules = ExposureRules()
    retention: RetentionRules = RetentionRules()

    @classmethod
    def load(cls, path: Path | str) -> KnowledgePolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise KnowledgePolicyError(f"knowledge policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise KnowledgePolicyError(f"knowledge policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise KnowledgePolicyError(
                f"invalid knowledge policy {policy_path}: {exc}"
            ) from exc
