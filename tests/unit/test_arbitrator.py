"""Arbitration rules (spec 9.3, 9.4, 12.3, 24, 40)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.state.arbitrator import RejectionReason, StateArbitrator
from app.state.policy import ArbitrationPolicy
from app.state.proposal import StateChangeProposal
from app.state.snapshot import StateSnapshot
from app.state.value import StateValue

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


def snapshot_with(**values: float) -> StateSnapshot:
    entries = {}
    for target, value in values.items():
        domain, key = target.split("__", 1)
        entries[(domain, key)] = StateValue(
            domain=domain, key=key, value=value, version=1, created_at=NOW, updated_at=NOW
        )
    return StateSnapshot(snapshot_id="snap_test", created_at=NOW, values=entries)


def adjust(domain: str, key: str, magnitude: float, module: str, **kwargs) -> StateChangeProposal:
    return StateChangeProposal.adjust(
        source_event_id="evt_TEST",
        source_module=module,
        target_domain=domain,
        target_key=key,
        magnitude=magnitude,
        **kwargs,
    )


def reasons(result) -> set[str]:
    return {rejection.reason_code for rejection in result.rejected}


def test_owner_adjust_is_accepted(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(relationship__trust=0.5)
    result = arbitrator.arbitrate(
        [adjust("relationship", "trust", 0.03, "relationship_engine", evidence_ids=("ev_1",))],
        snapshot,
    )
    assert result.rejected == ()
    change = result.accepted[0]
    assert change.target == "relationship.trust"
    assert change.previous_value == 0.5
    assert change.new_value == pytest.approx(0.53)
    assert change.expected_version == 1


def test_non_owner_is_rejected(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(relationship__trust=0.5)
    result = arbitrator.arbitrate(
        [adjust("relationship", "trust", 0.01, "emotion_engine", evidence_ids=("ev_1",))], snapshot
    )
    assert result.accepted == ()
    assert reasons(result) == {RejectionReason.NOT_STATE_OWNER}


def test_unknown_domain_is_rejected(arbitrator: StateArbitrator) -> None:
    result = arbitrator.arbitrate(
        [adjust("astrology", "mercury", 0.01, "emotion_engine")], snapshot_with()
    )
    assert reasons(result) == {RejectionReason.UNKNOWN_DOMAIN}


def test_adjust_without_baseline_is_rejected(arbitrator: StateArbitrator) -> None:
    """Spec 24: unknown is not zero, so an adjust cannot invent a baseline."""
    result = arbitrator.arbitrate(
        [adjust("emotion", "joy", 0.2, "emotion_engine")], snapshot_with()
    )
    assert reasons(result) == {RejectionReason.UNINITIALIZED_TARGET}


def test_delta_beyond_policy_is_rejected_not_clamped(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(relationship__trust=0.5)
    result = arbitrator.arbitrate(
        [adjust("relationship", "trust", 0.9, "relationship_engine", evidence_ids=("ev_1",))],
        snapshot,
    )
    assert result.accepted == ()
    assert reasons(result) == {RejectionReason.DELTA_EXCEEDS_POLICY}


def test_merged_deltas_are_bounded_together(arbitrator: StateArbitrator) -> None:
    """Several small proposals cannot add up past the per-event limit."""
    snapshot = snapshot_with(relationship__trust=0.5)
    proposals = [
        adjust("relationship", "trust", 0.04, "relationship_engine", evidence_ids=("ev_1",))
        for _ in range(3)
    ]
    result = arbitrator.arbitrate(proposals, snapshot)
    assert result.accepted == ()
    assert reasons(result) == {RejectionReason.DELTA_EXCEEDS_POLICY}


def test_merged_deltas_within_limit_are_summed(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(relationship__trust=0.5)
    proposals = [
        adjust("relationship", "trust", 0.01, "relationship_engine", evidence_ids=("ev_a",)),
        adjust("relationship", "trust", 0.02, "relationship_engine", evidence_ids=("ev_b",)),
    ]
    result = arbitrator.arbitrate(proposals, snapshot)
    change = result.accepted[0]
    assert change.delta == pytest.approx(0.03)
    assert change.new_value == pytest.approx(0.53)
    assert change.evidence_ids == ("ev_a", "ev_b")
    assert len(change.merged_proposal_ids) == 2


def test_out_of_bounds_result_is_rejected(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(emotion__joy=0.95)
    result = arbitrator.arbitrate(
        [adjust("emotion", "joy", 0.4, "emotion_engine")], snapshot
    )
    assert reasons(result) == {RejectionReason.VALUE_OUT_OF_BOUNDS}


def test_conflicting_operations_reject_the_whole_key(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(mood__valence=0.4)
    proposals = [
        adjust("mood", "valence", 0.05, "mood_engine"),
        StateChangeProposal.set_value(
            source_event_id="evt_TEST",
            source_module="mood_engine",
            target_domain="mood",
            target_key="valence",
            value=0.9,
        ),
    ]
    result = arbitrator.arbitrate(proposals, snapshot)
    assert result.accepted == ()
    assert reasons(result) == {RejectionReason.CONFLICTING_OPERATIONS}


def test_conflicting_set_values_are_rejected(arbitrator: StateArbitrator) -> None:
    proposals = [
        StateChangeProposal.set_value(
            source_event_id="evt_TEST", source_module="world_service",
            target_domain="world", target_key="current_activity", value=activity,
        )
        for activity in ("reading", "sleeping")
    ]
    result = arbitrator.arbitrate(proposals, snapshot_with())
    assert reasons(result) == {RejectionReason.CONFLICTING_SET_VALUES}


def test_identical_set_values_merge(arbitrator: StateArbitrator) -> None:
    proposals = [
        StateChangeProposal.set_value(
            source_event_id="evt_TEST", source_module="world_service",
            target_domain="world", target_key="current_activity", value="reading",
        )
        for _ in range(2)
    ]
    result = arbitrator.arbitrate(proposals, snapshot_with())
    assert len(result.accepted) == 1
    assert result.accepted[0].new_value == "reading"


def test_non_numeric_target_cannot_be_adjusted(arbitrator: StateArbitrator) -> None:
    values = {
        ("world", "current_activity"): StateValue(
            domain="world", key="current_activity", value="reading",
            version=1, created_at=NOW, updated_at=NOW,
        )
    }
    snapshot = StateSnapshot(snapshot_id="snap_x", created_at=NOW, values=values)
    result = arbitrator.arbitrate(
        [adjust("world", "current_activity", 0.1, "world_service")], snapshot
    )
    assert reasons(result) == {RejectionReason.NON_NUMERIC_TARGET}


def test_non_finite_magnitude_is_rejected(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(emotion__joy=0.5)
    result = arbitrator.arbitrate(
        [adjust("emotion", "joy", float("inf"), "emotion_engine")], snapshot
    )
    assert reasons(result) == {RejectionReason.INVALID_MAGNITUDE}


def test_accepted_changes_are_ordered_shallow_to_deep(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(
        relationship__trust=0.5, emotion__joy=0.4, world__energy=0.5
    )
    proposals = [
        adjust("relationship", "trust", 0.01, "relationship_engine", evidence_ids=("ev_1",)),
        adjust("emotion", "joy", 0.1, "emotion_engine"),
        adjust("world", "energy", 0.1, "world_service"),
    ]
    result = arbitrator.arbitrate(proposals, snapshot)
    assert [change.domain for change in result.accepted] == ["world", "emotion", "relationship"]


def test_evidence_requirement_is_enforced(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(relationship__trust=0.5)
    result = arbitrator.arbitrate(
        [adjust("relationship", "trust", 0.01, "relationship_engine")], snapshot
    )
    assert reasons(result) == {RejectionReason.INSUFFICIENT_EVIDENCE}


def test_permissive_policy_is_available_for_focused_tests() -> None:
    arbitrator = StateArbitrator(ArbitrationPolicy.permissive())
    snapshot = snapshot_with(relationship__trust=0.5)
    result = arbitrator.arbitrate(
        [adjust("relationship", "trust", 5.0, "relationship_engine")], snapshot
    )
    assert result.accepted[0].new_value == pytest.approx(5.5)


def test_invalidate_marks_a_value_unknown(arbitrator: StateArbitrator) -> None:
    snapshot = snapshot_with(beliefs__user_likes_rain=0.7)
    proposal = StateChangeProposal(
        source_event_id="evt_TEST",
        source_module="belief_engine",
        target_domain="beliefs",
        target_key="user_likes_rain",
        operation="invalidate",
        reason_codes=("contradicted",),
    )
    result = arbitrator.arbitrate([proposal], snapshot)
    assert result.accepted[0].new_value is None


def test_invalidate_of_unknown_key_is_rejected(arbitrator: StateArbitrator) -> None:
    proposal = StateChangeProposal(
        source_event_id="evt_TEST",
        source_module="belief_engine",
        target_domain="beliefs",
        target_key="never_written",
        operation="invalidate",
    )
    result = arbitrator.arbitrate([proposal], snapshot_with())
    assert reasons(result) == {RejectionReason.UNINITIALIZED_TARGET}


def test_proposal_shape_is_validated() -> None:
    with pytest.raises(ValueError):
        StateChangeProposal(
            source_event_id="evt_TEST",
            source_module="emotion_engine",
            target_domain="emotion",
            target_key="joy",
            operation="adjust",
        )
    with pytest.raises(ValueError):
        StateChangeProposal(
            source_event_id="evt_TEST",
            source_module="emotion_engine",
            target_domain="emotion",
            target_key="joy",
            operation="set",
            value=0.5,
            magnitude=0.1,
        )
