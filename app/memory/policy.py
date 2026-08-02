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


class SegmentationRules(_Frozen):
    max_gap_minutes: float = Field(default=30.0, gt=0)
    max_events: int = Field(default=40, gt=0)
    max_duration_minutes: float = Field(default=180.0, gt=0)
    min_events_to_encode: int = Field(default=2, ge=1)


class SegmentationPolicy(SegmentationRules):
    """Boundaries, with per-origin overrides (patch spec 15.1).

    A silence of thirty minutes ends a conversation. It does not end a stretch
    of a simulated life, where consecutive experiences are a month apart by
    construction — under the conversation rules every simulated experience
    became its own one-event episode, every one of them fell under
    ``min_events_to_encode``, and nineteen years produced nothing.

    The rules are the same rules; only the scale of "one continuous stretch"
    differs between kinds of life.
    """

    by_origin: dict[str, SegmentationRules] = Field(default_factory=dict)

    def for_origin(self, origin: str | None) -> SegmentationRules:
        if origin is not None and origin in self.by_origin:
            return self.by_origin[origin]
        return SegmentationRules(
            max_gap_minutes=self.max_gap_minutes,
            max_events=self.max_events,
            max_duration_minutes=self.max_duration_minutes,
            min_events_to_encode=self.min_events_to_encode,
        )


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
    #: Phase 2 §2M. Practice alone must never make a memory permanently and
    #: perfectly available: the production database ended with memories pinned
    #: at 1.0 that had never been the answer to anything. The exact number is
    #: tuning; that there *is* a ceiling below 1.0 is not.
    max_accessibility: float = Field(default=0.90, ge=0.0, le=1.0)


class ModeCandidateRules(_Frozen):
    """How wide Stage 1 casts for one recall mode (Phase 2 §2B, §2D)."""

    #: How many rows each way in may return before merging.
    pool: int = Field(default=20, gt=0)
    #: How many candidates reach Stage 2. The spec's 10-20.
    max_candidates: int = Field(default=16, gt=0)
    #: Whether plain recency is a way in. Ordinary conversation says no: a
    #: recent memory is not thereby a relevant one.
    include_recent: bool = False
    #: Whether importance is a way in. 「一番印象に残っているのは」 is literally a
    #: question about importance; 「そうだね」 is not.
    include_important: bool = False


class CandidatePolicy(_Frozen):
    """Stage 1 tuning. No accessibility appears here, on purpose (§2B)."""

    min_query_chars: int = Field(default=3, ge=1)
    min_importance: float = Field(default=0.45, ge=0.0, le=1.0)
    default: ModeCandidateRules = ModeCandidateRules()
    by_mode: dict[str, ModeCandidateRules] = Field(default_factory=dict)

    def for_mode(self, mode: str) -> ModeCandidateRules:
        return self.by_mode.get(mode, self.default)


class ModeRelevanceRules(_Frozen):
    """How strict the gate is, and how much may surface, per mode (§2D)."""

    #: Phase 2 §2E: ``weak`` is rejected by default. A mode may accept it when
    #: a direct question has been asked and a partial answer beats silence.
    accept_weak: bool = False
    #: Stage 3's output cap. Zero is always allowed (§2I).
    max_recalled: int = Field(default=3, ge=0)
    #: How reachable a relevant memory must be to actually come to mind.
    min_availability: float = Field(default=0.35, ge=0.0, le=1.0)


class SelectionWeights(_Frozen):
    """Stage 3 only. Every one of these is about *reachability*, not aboutness."""

    accessibility: float = 0.45
    emotional_salience: float = 0.20
    importance: float = 0.20
    recency: float = 0.15


class RetrievalPolicy(_Frozen):
    #: Kept for the legacy single-shot path and as the default cap.
    limit: int = Field(default=4, ge=0)
    min_query_chars: int = Field(default=3, ge=1)
    recency_half_life_days: float = Field(default=14.0, gt=0)
    candidates: CandidatePolicy = CandidatePolicy()
    #: A ``strong`` judgement is worth more availability than a ``relevant``
    #: one: being clearly on topic is itself a retrieval cue.
    strong_relevance_bonus: float = Field(default=0.15, ge=0.0, le=1.0)
    weights: SelectionWeights = SelectionWeights()
    default_relevance: ModeRelevanceRules = ModeRelevanceRules()
    by_mode: dict[str, ModeRelevanceRules] = Field(default_factory=dict)
    #: How many candidates one rerank call may judge.
    rerank_batch: int = Field(default=16, gt=0)

    def relevance_for(self, mode: str) -> ModeRelevanceRules:
        return self.by_mode.get(mode, self.default_relevance)


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
