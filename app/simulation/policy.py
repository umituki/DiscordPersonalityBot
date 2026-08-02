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


class GenesisRules(_Frozen):
    """When consolidation happens during a simulated life (patch spec 14).

    ``Genesis終了時1回だけを禁止する``. One consolidation at the end cannot
    produce growth: the Deep Gate needs the same tendency to show up across
    several separated windows, and a single run has exactly one window. The
    fix is not to weaken the gate — it is to let the life actually pass
    through consolidation as it goes.
    """

    #: How much simulated time between routine consolidations. Tuning.
    consolidation_interval_simulated_days: float = Field(default=90.0, gt=0.0)
    consolidate_on_phase_boundary: bool = True
    consolidate_after_major_event: bool = True
    final_consolidation: bool = True
    #: Experience classes that count as "major" for the trigger above.
    major_classes: tuple[str, ...] = ("major", "turning_point")


class FirstBootRules(_Frozen):
    min_experiences: int = Field(default=20, ge=0)
    min_blocks: int = Field(default=12, ge=0)
    min_retained_knowledge: int = Field(default=0, ge=0)
    min_memories: int = Field(default=0, ge=0)

    # --- patch spec 17: the pipeline actually ran ---------------------------
    #: A run this long is a real life and is held to the multi-year thresholds
    #: below. Shorter runs exist for tests and smoke checks.
    multi_year_threshold_years: float = Field(default=2.0, gt=0.0)

    #: Patch spec 17. Each of these is evidence that one stage of the causal
    #: chain left something behind. Zero anywhere means the chain is broken,
    #: which is precisely what 238 experiences and no psychology looked like.
    min_appraised_simulated_events: int = Field(default=1, ge=0)
    min_state_effect_changes: int = Field(default=1, ge=0)
    min_encoding_attempts: int = Field(default=1, ge=0)
    #: ``periodic_consolidation_runs > 1`` — one run at the end is the failure.
    min_periodic_consolidations: int = Field(default=2, ge=0)
    min_knowledge_sources_or_candidates: int = Field(default=1, ge=0)
    min_knowledge_exposures: int = Field(default=1, ge=0)

    # --- 17.1 growth health -------------------------------------------------
    #: Not "personality changed" — a life that left someone the same is a
    #: possible life. This is whether the machinery ran at all.
    min_growth_evidence: int = Field(default=1, ge=0)
    require_deep_gate_evaluated: bool = True

    # --- 17.2 / 17.3 multi-year floors --------------------------------------
    #: Patch spec prohibition 5 forbids leaving these at zero and calling the
    #: audit done. They apply to runs past the multi-year threshold.
    min_memories_multi_year: int = Field(default=1, ge=0)
    min_retained_knowledge_multi_year: int = Field(default=1, ge=0)
    min_encoding_attempts_multi_year: int = Field(default=2, ge=0)

    # --- 18 block audit -----------------------------------------------------
    #: A block that produced no events is a block that did not happen.
    allow_zero_event_count_blocks: bool = False
    #: Every ordinary day described with the same sentence is not a life.
    min_distinct_block_summaries: int = Field(default=2, ge=1)

    def multi_year(self, years: float) -> bool:
        return years >= self.multi_year_threshold_years

    def memories_floor(self, years: float) -> int:
        return max(
            self.min_memories,
            self.min_memories_multi_year if self.multi_year(years) else 0,
        )

    def retained_knowledge_floor(self, years: float) -> int:
        return max(
            self.min_retained_knowledge,
            self.min_retained_knowledge_multi_year if self.multi_year(years) else 0,
        )

    def encoding_attempts_floor(self, years: float) -> int:
        return max(
            self.min_encoding_attempts,
            self.min_encoding_attempts_multi_year if self.multi_year(years) else 0,
        )


class SimulationPolicy(_Frozen):
    policy_version: int = 1
    seed: SeedRules = SeedRules()
    blocks: BlockRules = BlockRules()
    experience: ExperienceRules = ExperienceRules()
    knowledge: KnowledgeExposureRules = KnowledgeExposureRules()
    genesis: GenesisRules = GenesisRules()
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
