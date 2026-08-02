"""Virtual society tuning values (spec 20, 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SocietyPolicyError(RuntimeError):
    """Raised when the society policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NPCRules(_Frozen):
    min_tier_for_relationship: int = Field(default=1, ge=0, le=2)
    min_tier_for_model: int = Field(default=2, ge=0, le=2)
    model_step: float = Field(default=0.08, ge=0.0, le=1.0)
    model_confidence_per_observation: float = Field(default=0.05, ge=0.0, le=1.0)
    max_model_confidence: float = Field(default=0.85, ge=0.0, le=1.0)


class NPCRelationshipRules(_Frozen):
    familiarity_per_interaction: float = Field(default=0.06, ge=0.0)
    closeness_per_positive: float = Field(default=0.05, ge=0.0)
    conflict_per_negative: float = Field(default=0.10, ge=0.0)
    conflict_relief_per_positive: float = Field(default=0.04, ge=0.0)
    positive_valence_threshold: float = Field(default=0.15)
    negative_valence_threshold: float = Field(default=-0.15)
    max_change_per_interaction: float = Field(default=0.10, ge=0.0)


class LifecycleRules(_Frozen):
    familiar_familiarity: float = Field(default=0.35, ge=0.0, le=1.0)
    close_closeness: float = Field(default=0.55, ge=0.0, le=1.0)
    strained_conflict: float = Field(default=0.45, ge=0.0, le=1.0)
    distant_days: float = Field(default=30.0, gt=0.0)
    dormant_days: float = Field(default=120.0, gt=0.0)
    reconnected_within_days: float = Field(default=7.0, gt=0.0)


class GroupRules(_Frozen):
    belonging_per_activity: float = Field(default=0.05, ge=0.0)
    belonging_decay_per_idle_day: float = Field(default=0.004, ge=0.0)
    initial_belonging: float = Field(default=0.20, ge=0.0, le=1.0)
    max_belonging: float = Field(default=0.90, ge=0.0, le=1.0)
    relatedness_contribution: float = Field(default=0.35, ge=0.0, le=1.0)


class SocietyPolicy(_Frozen):
    policy_version: int = 1
    npc: NPCRules = NPCRules()
    relationship: NPCRelationshipRules = NPCRelationshipRules()
    lifecycle: LifecycleRules = LifecycleRules()
    group: GroupRules = GroupRules()

    @classmethod
    def load(cls, path: Path | str) -> SocietyPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise SocietyPolicyError(f"society policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise SocietyPolicyError(f"society policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise SocietyPolicyError(f"invalid society policy {policy_path}: {exc}") from exc
