"""INVARIANT: every state domain has exactly one writer (spec 9.3, 37).

A subsystem that wants another domain to change emits a proposal. The forbidden
shortcuts of spec 37 — emotion writing relationship, memory writing personality
— are rejected at arbitration, and no partial effect reaches the database.
"""

from __future__ import annotations

import pytest

from app.state import ownership
from app.state.arbitrator import RejectionReason
from app.state.dependency_graph import validate_registry
from app.state.proposal import StateChangeProposal
from tests.unit.test_arbitrator import snapshot_with

pytestmark = pytest.mark.invariant

FORBIDDEN_SHORTCUTS = [
    # (writer, domain it must not write) — spec 37
    ("emotion_engine", "relationship"),
    ("memory_engine", "personality"),
    ("memory_engine", "values"),
    ("social_cognition_engine", "relationship"),
    ("world_service", "emotion"),
    ("goal_engine", "beliefs"),
    ("relationship_engine", "attachment"),
]


def test_each_domain_has_exactly_one_owner() -> None:
    for domain in ownership.known_domains():
        owner = ownership.owner_of(domain)
        assert owner
        assert ownership.may_write(domain, owner)


def test_registry_is_structurally_consistent() -> None:
    """Every owned domain is layered and every layered domain is owned."""
    validate_registry()


@pytest.mark.parametrize("module,domain", FORBIDDEN_SHORTCUTS)
def test_cross_domain_write_is_rejected(arbitrator, module: str, domain: str) -> None:
    snapshot = snapshot_with(**{f"{domain}__probe": 0.5})
    proposal = StateChangeProposal.adjust(
        source_event_id="evt_TEST",
        source_module=module,
        target_domain=domain,
        target_key="probe",
        magnitude=0.01,
        evidence_ids=("ev_1", "ev_2", "ev_3"),
    )
    result = arbitrator.arbitrate([proposal], snapshot)

    assert result.accepted == ()
    assert result.rejected[0].reason_code in (
        RejectionReason.NOT_STATE_OWNER,
        RejectionReason.DEEP_UPDATE_REQUIRES_CONSOLIDATION,
    )


def test_ownership_helper_raises_for_non_owner() -> None:
    with pytest.raises(ownership.OwnershipError):
        ownership.assert_may_write("relationship", "emotion_engine")
    ownership.assert_may_write("relationship", "relationship_engine")


def test_rejected_cross_domain_write_leaves_no_trace(
    arbitrator, committer, snapshots, state_repo, event_store, runs, failures, make_event, clock
) -> None:
    state_repo.write_value(
        domain="relationship", key="trust", value=0.5, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    event = make_event()
    event_store.append(event)
    runs.start(
        run_id="run_X", root_event_id=event.event_id, snapshot_id=None, manifest_id=None,
        mode="test", priority=event.priority, now=clock.now(),
    )
    snapshot = snapshots.capture(root_event_id=event.event_id, run_id="run_X")

    result = arbitrator.arbitrate(
        [
            StateChangeProposal.adjust(
                source_event_id=event.event_id,
                source_module="emotion_engine",
                target_domain="relationship",
                target_key="trust",
                magnitude=0.04,
                evidence_ids=("ev_1",),
            )
        ],
        snapshot,
    )
    committer.commit(run_id="run_X", root_event_id=event.event_id, result=result)

    stored = state_repo.get("relationship", "trust")
    assert stored.value == 0.5
    assert stored.version == 1
    assert state_repo.change_count() == 0
    assert failures.for_run("run_X")[0]["reason_code"] == RejectionReason.NOT_STATE_OWNER


def test_unknown_module_cannot_write_anything(arbitrator) -> None:
    snapshot = snapshot_with(emotion__joy=0.5)
    result = arbitrator.arbitrate(
        [
            StateChangeProposal.adjust(
                source_event_id="evt_TEST",
                source_module="discord_interface",
                target_domain="emotion",
                target_key="joy",
                magnitude=0.1,
            )
        ],
        snapshot,
    )
    assert result.rejected[0].reason_code == RejectionReason.NOT_STATE_OWNER
