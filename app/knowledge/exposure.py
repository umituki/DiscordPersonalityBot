"""Exposure funnel (spec 21.5).

::

    World information existed
    → Exposure Opportunity
    → reached YUI?
    → attention?
    → curiosity?
    → comprehension?
    → encoded?
    → forgotten / retained?

    情報が有名だっただけで「知っている」としない。

Every arrow is a place where it stops, and the default at every stage is that
it stops. A famous fact with no channel to reach her, no attention paid, or no
way to understand it produces an opportunity and nothing else.

Pure functions: signals in, a stage and a reason out. Persisting the outcome
and deciding what it means is the service's job.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.knowledge.models import STAGES, ExposureStage
from app.knowledge.policy import ExposureRules


@dataclass(frozen=True, slots=True)
class ExposureSignals:
    """Everything the funnel is allowed to consider."""

    #: How prominent the information was at the time.
    salience: float
    #: How likely YUI's environment was to carry it at all.
    reach: float
    #: How hard it is to take in.
    complexity: float
    #: How much YUI cares about this topic right now.
    interest: float
    #: General openness to new things (a characteristic adaptation).
    curiosity: float
    #: What she can currently make sense of.
    comprehension_capacity: float = 0.6
    #: How new this is relative to what she already knows.
    novelty: float = 0.5


@dataclass(frozen=True, slots=True)
class ExposureOutcome:
    stage_reached: ExposureStage
    acquired: bool
    reason: str
    comprehension: float = 0.0
    retention: float = 0.0

    @property
    def stage_index(self) -> int:
        return STAGES.index(self.stage_reached)

    def reached(self, stage: ExposureStage) -> bool:
        return self.stage_index >= STAGES.index(stage)


class ExposureFunnel:
    def __init__(self, policy: ExposureRules) -> None:
        self._policy = policy

    def evaluate(self, signals: ExposureSignals) -> ExposureOutcome:
        rules = self._policy

        # --- did it reach her at all? ---------------------------------------
        reach_score = signals.reach * (0.5 + 0.5 * signals.salience)
        if reach_score < rules.reach_threshold:
            return ExposureOutcome(
                "opportunity", False, "it never reached her environment"
            )

        # --- was she paying attention? --------------------------------------
        attention = (
            rules.attention_from_salience * signals.salience
            + rules.attention_from_interest * signals.interest
        )
        if attention < rules.attention_threshold:
            return ExposureOutcome("reached", False, "it was there and she did not notice")

        # --- did she care enough to engage? ---------------------------------
        pull = rules.curiosity_from_interest * signals.interest + (
            1.0 - rules.curiosity_from_interest
        ) * signals.curiosity
        if pull < rules.curiosity_threshold:
            return ExposureOutcome("attended", False, "noticed, not pursued")

        # --- could she make sense of it? ------------------------------------
        comprehension = max(
            0.0,
            min(
                1.0,
                signals.comprehension_capacity
                + rules.comprehension_from_interest * signals.interest
                - rules.comprehension_penalty_from_complexity * signals.complexity,
            ),
        )
        if comprehension < rules.comprehension_threshold:
            return ExposureOutcome(
                "curious", False, "engaged but could not make sense of it",
                comprehension=round(comprehension, 6),
            )

        # --- did any of it stick? -------------------------------------------
        encoding = (
            rules.encoding_from_attention * attention
            + rules.encoding_from_comprehension * comprehension
            + rules.encoding_from_novelty * signals.novelty
        )
        if encoding < rules.encoding_threshold:
            return ExposureOutcome(
                "comprehended", False, "understood in the moment, not encoded",
                comprehension=round(comprehension, 6),
            )

        retention = max(0.0, min(1.0, encoding * (0.5 + 0.5 * comprehension)))
        if retention < rules.retention_threshold:
            return ExposureOutcome(
                "encoded", False, "encoded and then lost",
                comprehension=round(comprehension, 6),
                retention=round(retention, 6),
            )

        return ExposureOutcome(
            "retained",
            True,
            "learned",
            comprehension=round(comprehension, 6),
            retention=round(retention, 6),
        )
