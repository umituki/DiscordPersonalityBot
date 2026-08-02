"""Drift Monitor (spec 23.3).

    分類: EXPECTED / SUSPICIOUS / INVALID
    INVALID のみ自動 rollback/reprocess 候補。正常な人生変化を clamp で消さない。

The monitor measures and classifies. It has no state domain, proposes nothing
and clamps nothing — a person who changed a lot this month is not a bug, and
silently pulling them back toward last month's numbers would destroy exactly
the long-term change this system exists to have. Its output is a record and,
for INVALID, an event the owner can act on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.clock import Clock, SystemClock
from app.consolidation.models import DriftClassification, DriftObservation
from app.consolidation.policy import DriftRules
from app.storage.repositories.growth import DriftRepository
from app.storage.repositories.state import StateRepository

logger = logging.getLogger(__name__)

MODULE = "drift_monitor"

EXPECTED: DriftClassification = "EXPECTED"
SUSPICIOUS: DriftClassification = "SUSPICIOUS"
INVALID: DriftClassification = "INVALID"

#: Which state domains feed which metric (spec 23.3 monitoring candidates).
METRIC_DOMAINS: dict[str, tuple[str, ...]] = {
    "trait_velocity": ("personality",),
    "value_velocity": ("values",),
    "relationship_velocity": ("relationship", "attachment"),
    "adaptation_velocity": ("characteristic_adaptations",),
}


@dataclass(frozen=True, slots=True)
class DriftReport:
    observations: tuple[DriftObservation, ...]
    window_start: datetime
    window_end: datetime

    @property
    def anomalies(self) -> tuple[DriftObservation, ...]:
        return tuple(item for item in self.observations if item.classification != EXPECTED)

    @property
    def rollback_candidates(self) -> tuple[DriftObservation, ...]:
        return tuple(item for item in self.observations if item.classification == INVALID)


class DriftMonitor:
    name = MODULE

    def __init__(
        self,
        *,
        state: StateRepository,
        drifts: DriftRepository,
        policy: DriftRules,
        clock: Clock | None = None,
    ) -> None:
        self._state = state
        self._drifts = drifts
        self._policy = policy
        self._clock = clock or SystemClock()

    def classify(self, metric: str, value: float) -> tuple[DriftClassification, float]:
        """Ordinary → worth a look → not a life, a bug.

        An unmonitored metric is never an anomaly: spec 24 forbids inventing a
        judgement where no expectation was declared.
        """
        expected = self._policy.expected_for(metric)
        if expected is None:
            return EXPECTED, float("inf")
        if value <= expected * self._policy.suspicious_multiplier:
            return EXPECTED, expected
        if value <= expected * self._policy.invalid_multiplier:
            return SUSPICIOUS, expected
        return INVALID, expected

    def measure(
        self, *, now: datetime | None = None, extra: dict[str, float] | None = None
    ) -> DriftReport:
        """Measure one window and record what it found. Changes no state."""
        window_end = now or self._clock.now()
        window_start = window_end - timedelta(days=self._policy.window_days)

        measurements: dict[str, tuple[float, dict[str, float]]] = {}
        for metric, domains in METRIC_DOMAINS.items():
            measurements[metric] = self._velocity(domains, since=window_start)
        for metric, value in (extra or {}).items():
            measurements[metric] = (value, {})

        observations = []
        for metric in sorted(measurements):
            value, detail = measurements[metric]
            classification, expected = self.classify(metric, value)
            observations.append(
                self._drifts.record(
                    metric=metric,
                    window_start=window_start,
                    window_end=window_end,
                    value=round(value, 6),
                    expected_max=expected if expected != float("inf") else -1.0,
                    classification=classification,
                    detail=detail,
                    now=window_end,
                )
            )
            if classification != EXPECTED:
                logger.warning(
                    "drift %s metric=%s value=%.4f expected_max=%.4f",
                    classification,
                    metric,
                    value,
                    expected,
                )
        return DriftReport(tuple(observations), window_start, window_end)

    def _velocity(
        self, domains: tuple[str, ...], *, since: datetime
    ) -> tuple[float, dict[str, float]]:
        """Total absolute movement per key in the window, worst key wins."""
        rows = self._state.changes_since(since, limit=5000, domains=list(domains))
        per_key: dict[str, float] = {}
        for row in rows:
            delta = row["delta"]
            if delta is None:
                continue
            target = f"{row['domain']}.{row['key']}"
            per_key[target] = per_key.get(target, 0.0) + abs(float(delta))
        if not per_key:
            return 0.0, {}
        worst = max(per_key.values())
        return worst, {key: round(value, 6) for key, value in per_key.items()}

    # --- reads --------------------------------------------------------------
    def recent(self, *, limit: int = 20) -> list[DriftObservation]:
        return self._drifts.recent(limit=limit)

    def open_anomalies(self, *, limit: int = 20) -> list[DriftObservation]:
        return self._drifts.by_classification(INVALID, limit=limit)
