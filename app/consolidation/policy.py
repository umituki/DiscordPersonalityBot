"""Growth / consolidation tuning values (spec 12, 23, 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class GrowthPolicyError(RuntimeError):
    """Raised when the growth policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


#: ``[name, weight]`` pairs in YAML become this.
Mapping2 = tuple[str, float]


class ConsolidationRules(_Frozen):
    min_hours_between_runs: float = Field(default=6.0, ge=0.0)
    lookback_days: float = Field(default=30.0, gt=0.0)
    max_changes_per_run: int = Field(default=500, ge=1)
    semantic_min_supporting_memories: int = Field(default=3, ge=1)
    max_semantic_per_run: int = Field(default=5, ge=0)


class AdaptationRules(_Frozen):
    initial_value: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_before_change: float = Field(default=3.0, gt=0.0)
    step: float = Field(default=0.04, ge=0.0)
    max_change_per_run: float = Field(default=0.05, ge=0.0)
    extremity_damping: float = Field(default=0.80, ge=0.0, le=1.0)
    min_source_delta: float = Field(default=0.005, ge=0.0)
    sources: dict[str, Mapping2] = Field(default_factory=dict)

    def source_for(self, domain: str, key: str) -> Mapping2 | None:
        """Exact ``domain.key`` first, then a ``domain.*`` wildcard."""
        exact = self.sources.get(f"{domain}.{key}")
        if exact is not None:
            return exact
        return self.sources.get(f"{domain}.*")


class DeepGateRules(_Frozen):
    """The five conditions of spec 12.3. All of them, or nothing moves."""

    min_pattern_count: int = Field(default=4, ge=1)
    min_persistence_days: float = Field(default=14.0, ge=0.0)
    min_contexts: int = Field(default=2, ge=1)
    min_outcome_weight: float = Field(default=1.0, ge=0.0)
    min_mood_independent: int = Field(default=2, ge=0)
    mood_neutral_band: float = Field(default=0.25, ge=0.0, le=1.0)
    candidate_expiry_days: float = Field(default=120.0, gt=0.0)


class PersonalityRules(_Frozen):
    temperament: dict[str, float] = Field(default_factory=dict)
    max_step: float = Field(default=0.01, ge=0.0)
    extremity_damping: float = Field(default=0.80, ge=0.0, le=1.0)
    baseline_follow_rate: float = Field(default=0.20, ge=0.0, le=1.0)
    adaptation_to_trait: dict[str, Mapping2] = Field(default_factory=dict)


class ValueRules(_Frozen):
    priorities: dict[str, float] = Field(default_factory=dict)
    max_step: float = Field(default=0.01, ge=0.0)
    adaptation_to_value: dict[str, Mapping2] = Field(default_factory=dict)


class DispositionRules(_Frozen):
    """Attachment disposition: the general tendency, not the current state."""

    max_step: float = Field(default=0.01, ge=0.0)
    extremity_damping: float = Field(default=0.80, ge=0.0, le=1.0)
    adaptation_to_disposition: dict[str, Mapping2] = Field(default_factory=dict)


class NarrativeRules(_Frozen):
    min_supporting_memories: int = Field(default=3, ge=1)
    strength_step: float = Field(default=0.05, ge=0.0)
    max_strength: float = Field(default=0.95, ge=0.0, le=1.0)
    max_themes: int = Field(default=12, ge=1)
    established_strength: float = Field(default=0.50, ge=0.0, le=1.0)


class DriftRules(_Frozen):
    window_days: float = Field(default=30.0, gt=0.0)
    expected_per_window: dict[str, float] = Field(default_factory=dict)
    suspicious_multiplier: float = Field(default=2.0, ge=1.0)
    invalid_multiplier: float = Field(default=4.0, ge=1.0)

    def expected_for(self, metric: str) -> float | None:
        return self.expected_per_window.get(metric)


class GrowthPolicy(_Frozen):
    policy_version: int = 1
    consolidation: ConsolidationRules = ConsolidationRules()
    adaptation: AdaptationRules = AdaptationRules()
    deep_gate: DeepGateRules = DeepGateRules()
    personality: PersonalityRules = PersonalityRules()
    values: ValueRules = ValueRules()
    disposition: DispositionRules = DispositionRules()
    narrative: NarrativeRules = NarrativeRules()
    drift: DriftRules = DriftRules()

    @classmethod
    def load(cls, path: Path | str) -> GrowthPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise GrowthPolicyError(f"growth policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise GrowthPolicyError(f"growth policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise GrowthPolicyError(f"invalid growth policy {policy_path}: {exc}") from exc
