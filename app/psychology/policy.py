"""Immediate psychology tuning values (spec 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.psychology.models import Appraisal


class PsychologyPolicyError(RuntimeError):
    """Raised when the psychology policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AppraisalDefaults(_Frozen):
    self_relevance: float = 0.5
    goal_congruence: float = 0.0
    novelty: float = 0.2
    certainty: float = 0.5
    control: float = 0.5
    agency: float = 0.5
    social_meaning: float = 0.3
    expectation_violation: float = 0.0

    def as_appraisal(self, *, source: str = "default") -> Appraisal:
        return Appraisal(**self.model_dump(), source=source)  # type: ignore[arg-type]


class HeuristicAppraisalRules(_Frozen):
    """How Python reads an event that does not justify a model call.

    Patch spec 12.3. Every number here is tuning: what matters structurally is
    that a real appraisal reaches the engines downstream, not the exact
    coefficients.
    """

    #: Experience classes appraised in Python rather than by the model.
    classes: tuple[str, ...] = ("routine", "minor")
    #: How much weight each class carries. Higher means more novel and more
    #: expectation-violating for the same valence.
    class_weight: dict[str, float] = Field(
        default_factory=lambda: {"routine": 0.25, "minor": 0.5}
    )
    default_class_weight: float = Field(default=0.4, ge=0.0, le=1.0)

    base_self_relevance: float = Field(default=0.30, ge=0.0, le=1.0)
    significance_gain: float = Field(default=0.55, ge=0.0, le=2.0)
    base_novelty: float = Field(default=0.10, ge=0.0, le=1.0)
    class_novelty_gain: float = Field(default=0.50, ge=0.0, le=2.0)
    valence_gain: float = Field(default=0.80, ge=0.0, le=2.0)
    social_valence_gain: float = Field(default=0.60, ge=0.0, le=2.0)
    base_social_meaning: float = Field(default=0.15, ge=-1.0, le=1.0)
    expectation_gain: float = Field(default=0.70, ge=0.0, le=2.0)
    base_certainty: float = Field(default=0.65, ge=0.0, le=1.0)
    base_control: float = Field(default=0.50, ge=0.0, le=1.0)
    base_agency: float = Field(default=0.45, ge=0.0, le=1.0)
    agency_gain: float = Field(default=0.10, ge=0.0, le=1.0)
    #: Never as trusted as a considered reading, and capped like one (spec 24).
    confidence: float = Field(default=0.45, ge=0.0, le=1.0)

    def covers(self, experience_class: str) -> bool:
        return experience_class in self.classes


class AppraisalPolicy(_Frozen):
    defaults: AppraisalDefaults = AppraisalDefaults()
    #: Spec 24: a model's self-reported confidence is capped, not trusted whole.
    max_trusted_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    heuristic: HeuristicAppraisalRules = HeuristicAppraisalRules()


class EmotionPolicy(_Frozen):
    #: emotion name -> {appraisal dimension: weight}
    dimensions: dict[str, dict[str, float]] = Field(default_factory=dict)
    activation_threshold: float = Field(default=0.12, ge=0.0, le=1.0)
    max_intensity_change: float = Field(default=0.55, ge=0.0, le=1.0)
    half_life_minutes: float = Field(default=45.0, gt=0)
    max_concurrent: int = Field(default=3, ge=1)


class MoodPolicy(_Frozen):
    inertia: float = Field(default=0.90, ge=0.0, le=1.0)
    max_change_per_event: float = Field(default=0.06, ge=0.0, le=1.0)
    recent_emotion_window_minutes: float = Field(default=180.0, gt=0)
    baseline_valence: float = Field(default=0.55, ge=0.0, le=1.0)
    baseline_arousal: float = Field(default=0.40, ge=0.0, le=1.0)
    return_rate_per_hour: float = Field(default=0.02, ge=0.0, le=1.0)


class NeedsPolicy(_Frozen):
    relatedness_gain_per_interaction: float = 0.06
    relatedness_decay_per_hour: float = 0.010
    competence_gain_on_success: float = 0.04
    autonomy_gain_on_self_initiated: float = 0.03
    loneliness_gain_per_idle_hour: float = 0.012
    loneliness_relief_per_interaction: float = 0.10
    connection_desire_from_loneliness: float = 0.50
    solitude_desire_gain_per_interaction: float = 0.02
    solitude_desire_decay_per_hour: float = 0.03
    #: Spec 20.3: belonging to a group holds relatedness above a floor, so the
    #: USER is not the only place connection can come from. Below 1.0 on
    #: purpose — friends are a real source, not a replacement.
    relatedness_floor_from_belonging: float = Field(default=0.45, ge=0.0, le=1.0)
    max_change_per_event: float = Field(default=0.20, ge=0.0, le=1.0)


class PsychologyPolicy(_Frozen):
    policy_version: int = 1
    appraisal: AppraisalPolicy = AppraisalPolicy()
    emotion: EmotionPolicy = EmotionPolicy()
    mood: MoodPolicy = MoodPolicy()
    needs: NeedsPolicy = NeedsPolicy()

    @classmethod
    def load(cls, path: Path | str) -> PsychologyPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise PsychologyPolicyError(f"psychology policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise PsychologyPolicyError(f"psychology policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise PsychologyPolicyError(
                f"invalid psychology policy {policy_path}: {exc}"
            ) from exc
