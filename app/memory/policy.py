"""Memory tuning values (spec 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class MemoryPolicyError(RuntimeError):
    """Raised when the memory policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SegmentationPolicy(_Frozen):
    max_gap_minutes: float = Field(default=30.0, gt=0)
    max_events: int = Field(default=40, gt=0)
    max_duration_minutes: float = Field(default=180.0, gt=0)
    min_events_to_encode: int = Field(default=2, ge=1)


class EncodingWeights(_Frozen):
    novelty: float = 0.25
    emotional_intensity: float = 0.30
    prediction_error: float = 0.20
    user_directed: float = 0.15
    substance: float = 0.10


class EncodingPolicy(_Frozen):
    base_importance: float = Field(default=0.25, ge=0.0, le=1.0)
    weights: EncodingWeights = EncodingWeights()
    encode_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    initial_accessibility: float = Field(default=0.80, ge=0.0, le=1.0)
    initial_content_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    initial_source_confidence: float = Field(default=0.90, ge=0.0, le=1.0)
    initial_temporal_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    summary_max_chars: int = Field(default=400, gt=0)


class ForgettingPolicy(_Frozen):
    daily_decay_rate: float = Field(default=0.12, ge=0.0)
    min_accessibility: float = Field(default=0.02, ge=0.0, le=1.0)
    importance_protection: float = Field(default=0.60, ge=0.0, le=1.0)
    #: Forgetting is a loss of access, not a delete (spec 10.5).
    hard_delete: bool = False


class PracticePolicy(_Frozen):
    boost: float = Field(default=0.15, ge=0.0, le=1.0)
    diminishing_factor: float = Field(default=0.5, ge=0.0, le=1.0)
    window_hours: float = Field(default=24.0, gt=0)
    max_accessibility: float = Field(default=1.0, ge=0.0, le=1.0)


class RetrievalWeights(_Frozen):
    relevance: float = 0.40
    accessibility: float = 0.20
    recency: float = 0.15
    emotional_salience: float = 0.15
    importance: float = 0.10


class RetrievalPolicy(_Frozen):
    limit: int = Field(default=4, ge=0)
    min_score: float = Field(default=0.18, ge=0.0, le=1.0)
    candidate_pool: int = Field(default=40, gt=0)
    min_query_chars: int = Field(default=3, ge=1)
    recency_half_life_days: float = Field(default=14.0, gt=0)
    weights: RetrievalWeights = RetrievalWeights()


class SemanticPolicy(_Frozen):
    promotion_support_count: int = Field(default=3, ge=1)
    initial_confidence: float = Field(default=0.45, ge=0.0, le=1.0)
    confidence_per_support: float = Field(default=0.10, ge=0.0, le=1.0)
    max_confidence: float = Field(default=0.90, ge=0.0, le=1.0)


class MemoryPolicy(_Frozen):
    policy_version: int = 1
    segmentation: SegmentationPolicy = SegmentationPolicy()
    encoding: EncodingPolicy = EncodingPolicy()
    forgetting: ForgettingPolicy = ForgettingPolicy()
    practice: PracticePolicy = PracticePolicy()
    retrieval: RetrievalPolicy = RetrievalPolicy()
    semantic: SemanticPolicy = SemanticPolicy()

    @classmethod
    def load(cls, path: Path | str) -> MemoryPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise MemoryPolicyError(f"memory policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise MemoryPolicyError(f"memory policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise MemoryPolicyError(f"invalid memory policy {policy_path}: {exc}") from exc
