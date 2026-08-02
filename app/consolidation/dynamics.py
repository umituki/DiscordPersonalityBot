"""Shared growth dynamics (spec 23.2).

One rule, used by every layer that can drift:

    extreme state ほど普通の同方向 evidence の追加影響を小さくする

A value already near an edge gains less from yet another observation pointing
the same way. Evidence pointing back toward the middle is never damped, so no
state can be driven somewhere it cannot return from.
"""

from __future__ import annotations

MIDPOINT = 0.5


def outward(value: float, direction: float) -> bool:
    """True when a step would push ``value`` further from the middle."""
    return (direction > 0 and value > MIDPOINT) or (direction < 0 and value < MIDPOINT)


def damping_factor(value: float, direction: float, damping: float) -> float:
    if not outward(value, direction):
        return 1.0
    extremity = min(1.0, abs(value - MIDPOINT) * 2.0)
    return max(0.0, 1.0 - damping * extremity)


def damped_step(*, value: float, step: float, damping: float, limit: float) -> float:
    """A bounded, extremity-damped step. Returns the signed delta."""
    if step == 0.0:
        return 0.0
    direction = 1.0 if step > 0 else -1.0
    scaled = step * damping_factor(value, direction, damping)
    return max(-limit, min(limit, scaled))
