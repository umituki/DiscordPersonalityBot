"""The deep update gate (spec 12.3, 2.13, 34.2-5).

    Personality update は以下を重視する。

    - repeated pattern
    - temporal persistence
    - cross-context evidence
    - meaningful outcome
    - temporary mood だけで説明できないこと

    単一 Event から Trait を直接変更してはならない。

All five conditions, or nothing moves. This module is pure: it inspects an
accumulated candidate and says whether it is allowed through, and if not, which
condition is still missing. The arbitration policy enforces the same rule
independently from the other side (``requires_consolidation`` plus a minimum
evidence count), so a bug here cannot by itself let a single event change a
trait.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.consolidation.models import DeepUpdateCandidate
from app.consolidation.policy import DeepGateRules

REPEATED_PATTERN = "repeated_pattern"
TEMPORAL_PERSISTENCE = "temporal_persistence"
CROSS_CONTEXT = "cross_context_evidence"
MEANINGFUL_OUTCOME = "meaningful_outcome"
MOOD_INDEPENDENCE = "not_explained_by_mood"

CONDITIONS: tuple[str, ...] = (
    REPEATED_PATTERN,
    TEMPORAL_PERSISTENCE,
    CROSS_CONTEXT,
    MEANINGFUL_OUTCOME,
    MOOD_INDEPENDENCE,
)


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Why a deep update may or may not happen."""

    passed: bool
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def reason(self) -> str:
        if self.passed:
            return "all deep update conditions satisfied"
        return "missing: " + ", ".join(self.missing)


class DeepUpdateGate:
    def __init__(self, policy: DeepGateRules) -> None:
        self._policy = policy

    def evaluate(self, candidate: DeepUpdateCandidate) -> GateDecision:
        checks = {
            REPEATED_PATTERN: candidate.pattern_count >= self._policy.min_pattern_count,
            TEMPORAL_PERSISTENCE: (
                candidate.persistence_days >= self._policy.min_persistence_days
            ),
            CROSS_CONTEXT: len(candidate.contexts) >= self._policy.min_contexts,
            MEANINGFUL_OUTCOME: (
                candidate.outcome_weight >= self._policy.min_outcome_weight
            ),
            MOOD_INDEPENDENCE: (
                candidate.mood_independent_count >= self._policy.min_mood_independent
            ),
        }
        satisfied = tuple(name for name in CONDITIONS if checks[name])
        missing = tuple(name for name in CONDITIONS if not checks[name])
        return GateDecision(passed=not missing, satisfied=satisfied, missing=missing)

    def is_expired(self, candidate: DeepUpdateCandidate, *, now_days_idle: float) -> bool:
        """Evidence that stopped recurring stops being evidence."""
        return now_days_idle > self._policy.candidate_expiry_days

    def mood_is_ordinary(self, valence: float | None, baseline: float) -> bool:
        """True when mood was near its baseline, so it explains nothing.

        Unknown mood is not treated as ordinary: spec 24 forbids reading an
        absent value as a convenient zero.
        """
        if valence is None:
            return False
        return abs(valence - baseline) <= self._policy.mood_neutral_band
