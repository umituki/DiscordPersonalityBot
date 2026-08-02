"""Agency tuning values (spec 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class AgencyPolicyError(RuntimeError):
    """Raised when the agency policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GoalPolicy(_Frozen):
    activation_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    max_active: int = Field(default=5, ge=1)
    importance_from_need: float = Field(default=0.60, ge=0.0, le=1.0)
    importance_from_value: float = Field(default=0.40, ge=0.0, le=1.0)
    progress_step: float = Field(default=0.10, ge=0.0, le=1.0)
    abandon_after_idle_days: float = Field(default=30.0, gt=0)


class HabitPolicy(_Frozen):
    automaticity_gain: float = Field(default=0.08, ge=0.0, le=1.0)
    automaticity_ceiling: float = Field(default=0.95, ge=0.0, le=1.0)
    #: Disuse, not a streak reset (spec 15.3).
    decay_per_idle_day: float = Field(default=0.01, ge=0.0, le=1.0)
    #: A habit whose context vanished keeps at least this much trace.
    trace_floor: float = Field(default=0.15, ge=0.0, le=1.0)
    established_at: float = Field(default=0.60, ge=0.0, le=1.0)
    automatic_trigger: float = Field(default=0.50, ge=0.0, le=1.0)


class DecisionPolicy(_Frozen):
    #: Candidates within this distance count as "close enough to waver".
    tie_threshold: float = Field(default=0.08, ge=0.0, le=1.0)
    exploration_noise: float = Field(default=0.04, ge=0.0, le=1.0)
    min_candidates: int = Field(default=2, ge=1)
    learning_rate: float = Field(default=0.20, ge=0.0, le=1.0)


class EpistemicPolicy(_Frozen):
    curiosity_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    search_cost: float = Field(default=0.35, ge=0.0, le=1.0)
    ask_user_cost: float = Field(default=0.20, ge=0.0, le=1.0)
    prefer_ask_when_present: bool = True
    retry_after_failure_hours: float = Field(default=6.0, gt=0)


class AgencyPolicy(_Frozen):
    policy_version: int = 1
    goals: GoalPolicy = GoalPolicy()
    habits: HabitPolicy = HabitPolicy()
    decision: DecisionPolicy = DecisionPolicy()
    epistemic: EpistemicPolicy = EpistemicPolicy()

    @classmethod
    def load(cls, path: Path | str) -> AgencyPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise AgencyPolicyError(f"agency policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise AgencyPolicyError(f"agency policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise AgencyPolicyError(f"invalid agency policy {policy_path}: {exc}") from exc
