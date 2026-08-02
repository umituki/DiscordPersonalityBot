"""Forgetting and retrieval practice (spec 10.5).

Three rules from the specification are encoded here:

* Forgetting lowers **accessibility**; it does not delete the memory.
* Accessibility and importance are different things. Importance slows decay but
  is never changed by it.
* Retrieval practice raises accessibility with **diminishing returns**, so
  rumination cannot pump a memory to permanence.
"""

from __future__ import annotations

import math
from datetime import datetime

from app.memory.policy import ForgettingPolicy, PracticePolicy


def decayed_accessibility(
    *,
    accessibility: float,
    importance: float,
    elapsed_days: float,
    policy: ForgettingPolicy,
) -> float:
    """Exponential decay, slowed by importance, floored above zero."""
    if elapsed_days <= 0:
        return accessibility
    protection = 1.0 - (policy.importance_protection * max(0.0, min(1.0, importance)))
    rate = policy.daily_decay_rate * protection
    decayed = accessibility * math.exp(-rate * elapsed_days)
    return max(policy.min_accessibility, min(1.0, decayed))


def practised_accessibility(
    *,
    accessibility: float,
    recent_practice_count: int,
    policy: PracticePolicy,
) -> float:
    """Boost from a recall, halving for each recent recall in the window."""
    boost = policy.boost * (policy.diminishing_factor ** max(0, recent_practice_count))
    return min(policy.max_accessibility, accessibility + boost)


def elapsed_days(since: datetime | None, now: datetime) -> float:
    if since is None:
        return 0.0
    seconds = (now - since).total_seconds()
    return max(0.0, seconds / 86400.0)
