"""Virtual life tuning values (spec 18, 19, 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class WorldPolicyError(RuntimeError):
    """Raised when the world policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SleepPolicy(_Frozen):
    pressure_gain_per_awake_hour: float = Field(default=0.055, ge=0.0)
    pressure_recovery_per_sleep_hour: float = Field(default=0.140, ge=0.0)
    max_pressure: float = Field(default=1.0, gt=0.0)
    circadian_period_hours: float = Field(default=24.0, gt=0.0)
    circadian_low_hour: float = Field(default=3.0, ge=0.0, lt=24.0)
    circadian_amplitude: float = Field(default=0.35, ge=0.0, le=1.0)
    sleepiness_from_pressure: float = Field(default=0.65, ge=0.0, le=1.0)
    sleepiness_from_circadian: float = Field(default=0.35, ge=0.0, le=1.0)
    sleep_threshold: float = Field(default=0.62, ge=0.0, le=1.0)
    wake_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    inertia_minutes: float = Field(default=25.0, ge=0.0)
    typical_sleep_hours: float = Field(default=7.0, gt=0.0)
    goal_resistance: float = Field(default=0.20, ge=0.0, le=1.0)
    conversation_resistance: float = Field(default=0.35, ge=0.0, le=1.0)
    max_resistance: float = Field(default=0.45, ge=0.0, le=1.0)


class WorldRules(_Frozen):
    #: Offline recovery is compressed to meaningful transitions (spec 18.3).
    catch_up_max_transitions: int = Field(default=24, ge=1)
    catch_up_min_step_minutes: float = Field(default=30.0, gt=0.0)
    default_location: str = "自室"
    default_activity: str = "なにもしていない"


class SchedulerPolicy(_Frozen):
    max_opportunities_per_tick: int = Field(default=5, ge=1)
    window_grace_minutes: float = Field(default=15.0, ge=0.0)
    default_misfire_policy: str = "skip"
    #: Spec 21.1. How long the autonomous runtime sleeps when no source can say
    #: when it would next have something. Not the normal cadence — the floor
    #: under "nobody knows", so the loop never becomes a per-second poll.
    idle_wake_seconds: float = Field(default=300.0, ge=1.0)


class ProactivePolicy(_Frozen):
    min_hours_between_contacts: float = Field(default=6.0, ge=0.0)
    #: Spec 19: silence must reduce contact, never increase it.
    max_unanswered: int = Field(default=2, ge=0)
    unanswered_backoff_multiplier: float = Field(default=3.0, ge=1.0)
    quiet_hours_utc: tuple[int, int] = (15, 22)
    base_threshold: float = Field(default=0.55, ge=0.0, le=1.0)


class WorldPolicy(_Frozen):
    policy_version: int = 1
    sleep: SleepPolicy = SleepPolicy()
    world: WorldRules = WorldRules()
    scheduler: SchedulerPolicy = SchedulerPolicy()
    proactive: ProactivePolicy = ProactivePolicy()

    @classmethod
    def load(cls, path: Path | str) -> WorldPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise WorldPolicyError(f"world policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise WorldPolicyError(f"world policy must be a mapping: {policy_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise WorldPolicyError(f"invalid world policy {policy_path}: {exc}") from exc
