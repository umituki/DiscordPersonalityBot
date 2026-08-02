"""Past simulation tuning values (spec 22, 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SimulationPolicyError(RuntimeError):
    """Raised when the simulation policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeedRules(_Frozen):
    expected_question_count: int = Field(default=7, ge=1)
    max_temperament_shift: float = Field(default=0.25, ge=0.0, le=0.5)
    neutral: float = Field(default=0.5, ge=0.0, le=1.0)


class BlockRules(_Frozen):
    compressed_block_days: float = Field(default=30.0, gt=0.0)
    expanded_block_days: float = Field(default=7.0, gt=0.0)
    max_blocks: int = Field(default=400, ge=1)
    expansion_threshold: float = Field(default=0.55, ge=0.0, le=1.0)


class ExperienceRules(_Frozen):
    routine_share: float = Field(default=0.72, ge=0.0, le=1.0)
    minor_share: float = Field(default=0.20, ge=0.0, le=1.0)
    meaningful_share: float = Field(default=0.065, ge=0.0, le=1.0)
    major_share: float = Field(default=0.013, ge=0.0, le=1.0)
    turning_point_share: float = Field(default=0.002, ge=0.0, le=1.0)
    max_major_per_year: float = Field(default=1.0, ge=0.0)
    max_turning_points_total: int = Field(default=3, ge=0)
    max_negative_major_share: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _shares_sum_to_one(self):
        total = (
            self.routine_share
            + self.minor_share
            + self.meaningful_share
            + self.major_share
            + self.turning_point_share
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"experience class shares must sum to 1.0, got {total}")
        return self

    @property
    def cumulative(self) -> tuple[tuple[str, float], ...]:
        """Class boundaries, cheapest first, for sampling."""
        running = 0.0
        boundaries = []
        for name, share in (
            ("routine", self.routine_share),
            ("minor", self.minor_share),
            ("meaningful", self.meaningful_share),
            ("major", self.major_share),
            ("turning_point", self.turning_point_share),
        ):
            running += share
            boundaries.append((name, running))
        return tuple(boundaries)


class KnowledgeExposureRules(_Frozen):
    exposures_per_block: int = Field(default=4, ge=0)
    base_curiosity: float = Field(default=0.5, ge=0.0, le=1.0)


class FirstBootRules(_Frozen):
    min_experiences: int = Field(default=20, ge=0)
    min_blocks: int = Field(default=12, ge=0)
    min_retained_knowledge: int = Field(default=0, ge=0)
    min_memories: int = Field(default=0, ge=0)


class SimulationPolicy(_Frozen):
    policy_version: int = 1
    seed: SeedRules = SeedRules()
    blocks: BlockRules = BlockRules()
    experience: ExperienceRules = ExperienceRules()
    knowledge: KnowledgeExposureRules = KnowledgeExposureRules()
    first_boot: FirstBootRules = FirstBootRules()

    @classmethod
    def load(cls, path: Path | str) -> SimulationPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise SimulationPolicyError(f"simulation policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise SimulationPolicyError(f"simulation policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise SimulationPolicyError(
                f"invalid simulation policy {policy_path}: {exc}"
            ) from exc
