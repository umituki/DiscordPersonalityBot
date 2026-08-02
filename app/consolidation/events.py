"""Consolidation events (spec 8.1, 9.5, 12.3).

Deep state is processed by a consolidation job, not by the run that handled the
event (spec 9.5). These events carry that job through the *normal* pipeline, so
a personality change is committed by the same arbitrator and the same
transaction as everything else — there is no second, privileged write path.

They are ``system`` category on purpose: consolidation is maintenance, not an
experience, and psychology subscribers must never receive it (spec 8.1).
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

DEEP_CONSOLIDATION_REVIEW = "DEEP_CONSOLIDATION_REVIEW"
DEEP_UPDATE_CANDIDATE_RAISED = "DEEP_UPDATE_CANDIDATE_RAISED"
DEEP_UPDATE_APPLIED = "DEEP_UPDATE_APPLIED"
DRIFT_ANOMALY_DETECTED = "DRIFT_ANOMALY_DETECTED"


@register_payload(DEEP_CONSOLIDATION_REVIEW)
class ConsolidationReviewPayload(EventPayload):
    """Asks the growth writers to look at what has accumulated."""

    consolidation_id: str
    kind: str = "routine"
    changes_read: int = 0
    adaptations_moved: int = 0


@register_payload(DEEP_UPDATE_CANDIDATE_RAISED)
class CandidateRaisedPayload(EventPayload):
    candidate_id: str
    target_domain: str
    target_key: str
    direction: int
    pattern_count: int = 0
    missing_conditions: tuple[str, ...] = ()


@register_payload(DEEP_UPDATE_APPLIED)
class DeepUpdateAppliedPayload(EventPayload):
    candidate_id: str
    target_domain: str
    target_key: str
    previous_value: float | None = None
    new_value: float = 0.0
    baseline_after: float | None = None
    satisfied_conditions: tuple[str, ...] = ()


@register_payload(DRIFT_ANOMALY_DETECTED)
class DriftAnomalyPayload(EventPayload):
    """Recorded for the owner. Spec 23.3: only INVALID is a rollback candidate."""

    metric: str
    value: float
    expected_max: float
    classification: str
    window_days: float = 0.0
