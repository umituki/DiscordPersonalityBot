"""Sleep model (spec 18.3).

``人間生理を擬似精密化しない。functional two-process inspired model とする``::

    Process S: wake history / sleep pressure
    Process C: circadian phase
    + goals / activity / emotion → sleep decision

Pure functions. They compute; the World Service decides and writes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.world.policy import SleepPolicy


@dataclass(frozen=True, slots=True)
class SleepSignals:
    """Everything that argues for or against sleeping right now."""

    sleep_pressure: float
    circadian_sleepiness: float
    sleepiness: float
    #: Reasons to stay up: an active goal, a live conversation.
    resistance: float

    @property
    def net_sleepiness(self) -> float:
        return max(0.0, self.sleepiness - self.resistance)


def circadian_sleepiness(now: datetime, policy: SleepPolicy) -> float:
    """A smooth daily curve peaking in the small hours (spec 18.3 Process C)."""
    hours = now.hour + now.minute / 60.0
    phase = (
        (hours - policy.circadian_low_hour) / policy.circadian_period_hours
    ) * 2.0 * math.pi
    # 1.0 at the circadian low point, 0.0 twelve hours later.
    value = 0.5 + 0.5 * math.cos(phase)
    return round(max(0.0, min(1.0, 0.5 + policy.circadian_amplitude * (value - 0.5) * 2)), 6)


def accumulated_pressure(
    *, current: float, awake_hours: float, policy: SleepPolicy
) -> float:
    """Process S while awake: pressure builds with time spent up."""
    if awake_hours <= 0:
        return current
    return round(
        min(policy.max_pressure, current + policy.pressure_gain_per_awake_hour * awake_hours),
        6,
    )


def recovered_pressure(*, current: float, slept_hours: float, policy: SleepPolicy) -> float:
    """Process S while asleep: pressure discharges."""
    if slept_hours <= 0:
        return current
    return round(
        max(0.0, current - policy.pressure_recovery_per_sleep_hour * slept_hours), 6
    )


def signals(
    *,
    now: datetime,
    sleep_pressure: float,
    policy: SleepPolicy,
    has_active_goal: bool = False,
    in_conversation: bool = False,
) -> SleepSignals:
    circadian = circadian_sleepiness(now, policy)
    sleepiness = (
        policy.sleepiness_from_pressure * sleep_pressure
        + policy.sleepiness_from_circadian * circadian
    )
    resistance = 0.0
    if has_active_goal:
        resistance += policy.goal_resistance
    if in_conversation:
        resistance += policy.conversation_resistance
    resistance = min(policy.max_resistance, resistance)

    # Staying up delays sleep; it does not abolish it. As sleepiness passes the
    # threshold, resistance loses its grip, so an exhausted YUI eventually
    # sleeps even mid-conversation (spec 18.3: a decision from combined
    # signals, not an override).
    if sleepiness > policy.sleep_threshold:
        headroom = max(1e-9, 1.0 - policy.sleep_threshold)
        overwhelm = min(1.0, (sleepiness - policy.sleep_threshold) / headroom)
        resistance *= 1.0 - overwhelm

    return SleepSignals(
        sleep_pressure=round(sleep_pressure, 6),
        circadian_sleepiness=circadian,
        sleepiness=round(max(0.0, min(1.0, sleepiness)), 6),
        resistance=round(max(0.0, resistance), 6),
    )


def should_sleep(signals_now: SleepSignals, policy: SleepPolicy) -> bool:
    """Sleep is a decision from combined signals, never a clock rule."""
    return signals_now.net_sleepiness >= policy.sleep_threshold


def should_wake(
    *, signals_now: SleepSignals, slept_hours: float, policy: SleepPolicy
) -> bool:
    if slept_hours >= policy.typical_sleep_hours:
        return True
    return signals_now.sleepiness <= policy.wake_threshold


def sleep_inertia(*, minutes_since_waking: float, policy: SleepPolicy) -> float:
    """Grogginess right after waking, fading over the inertia window."""
    if minutes_since_waking >= policy.inertia_minutes:
        return 0.0
    remaining = 1.0 - (minutes_since_waking / policy.inertia_minutes)
    return round(max(0.0, min(1.0, remaining)), 6)
