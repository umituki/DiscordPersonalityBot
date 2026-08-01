"""INVARIANT: a single event never moves deep personality (spec 2.13, 12.3, 34.2-5).

Personality traits, values, attachment disposition and narrative identity are
Layer 4 state. They change only through consolidation, with accumulated
evidence, and only in small steps.
"""

from __future__ import annotations

import pytest

from app.state.arbitrator import RejectionReason
from app.state.dependency_graph import DEEP, domains_in_layer, is_deep, layer_of
from app.state.proposal import StateChangeProposal
from tests.unit.test_arbitrator import snapshot_with

pytestmark = pytest.mark.invariant

DEEP_DOMAINS = domains_in_layer(DEEP)


def test_deep_domains_are_declared() -> None:
    assert set(DEEP_DOMAINS) == {
        "personality",
        "values",
        "attachment_disposition",
        "narrative_identity",
    }
    for domain in DEEP_DOMAINS:
        assert is_deep(domain)
    assert layer_of("emotion") < layer_of("relationship") < layer_of("personality")


@pytest.mark.parametrize("domain", DEEP_DOMAINS)
def test_single_event_engine_cannot_touch_deep_state(arbitrator, domain: str) -> None:
    snapshot = snapshot_with(**{f"{domain}__openness": 0.5})
    proposal = StateChangeProposal.adjust(
        source_event_id="evt_ONE_COMPLIMENT",
        source_module="emotion_engine",
        target_domain=domain,
        target_key="openness",
        magnitude=0.2,
        evidence_ids=("ev_1", "ev_2", "ev_3"),
    )
    result = arbitrator.arbitrate([proposal], snapshot)

    assert result.accepted == ()
    assert result.rejected[0].reason_code in (
        RejectionReason.DEEP_UPDATE_REQUIRES_CONSOLIDATION,
        RejectionReason.NOT_STATE_OWNER,
    )


def test_consolidation_still_needs_accumulated_evidence(arbitrator) -> None:
    snapshot = snapshot_with(personality__openness=0.5)
    proposal = StateChangeProposal.adjust(
        source_event_id="evt_ONE_EVENT",
        source_module="growth_engine",
        target_domain="personality",
        target_key="openness",
        magnitude=0.005,
        evidence_ids=("ev_only_one",),
    )
    result = arbitrator.arbitrate([proposal], snapshot)

    assert result.accepted == ()
    assert result.rejected[0].reason_code == RejectionReason.INSUFFICIENT_EVIDENCE


def test_consolidation_change_is_small_and_bounded(arbitrator) -> None:
    snapshot = snapshot_with(personality__openness=0.5)
    evidence = ("ev_1", "ev_2", "ev_3", "ev_4")

    too_large = StateChangeProposal.adjust(
        source_event_id="evt_CONSOLIDATION",
        source_module="growth_engine",
        target_domain="personality",
        target_key="openness",
        magnitude=0.2,
        evidence_ids=evidence,
    )
    assert arbitrator.arbitrate([too_large], snapshot).rejected[0].reason_code == (
        RejectionReason.DELTA_EXCEEDS_POLICY
    )

    acceptable = StateChangeProposal.adjust(
        source_event_id="evt_CONSOLIDATION",
        source_module="growth_engine",
        target_domain="personality",
        target_key="openness",
        magnitude=0.008,
        evidence_ids=evidence,
    )
    accepted = arbitrator.arbitrate([acceptable], snapshot).accepted
    assert accepted[0].new_value == pytest.approx(0.508)


def test_deep_state_cannot_be_set_outright(arbitrator) -> None:
    """Spec 37: past simulation must not set the final personality directly."""
    snapshot = snapshot_with(personality__openness=0.5)
    proposal = StateChangeProposal.set_value(
        source_event_id="evt_GENESIS",
        source_module="past_simulation_engine",
        target_domain="personality",
        target_key="openness",
        value=0.95,
        evidence_ids=("ev_1", "ev_2", "ev_3"),
    )
    result = arbitrator.arbitrate([proposal], snapshot)
    assert result.accepted == ()
    assert result.rejected[0].reason_code in (
        RejectionReason.NOT_STATE_OWNER,
        RejectionReason.DEEP_UPDATE_REQUIRES_CONSOLIDATION,
    )


def test_many_small_deep_proposals_still_respect_the_per_event_limit(arbitrator) -> None:
    """Splitting a large deep change into pieces must not defeat the limit."""
    snapshot = snapshot_with(values__benevolence=0.5)
    proposals = [
        StateChangeProposal.adjust(
            source_event_id="evt_CONSOLIDATION",
            source_module="value_engine",
            target_domain="values",
            target_key="benevolence",
            magnitude=0.006,
            evidence_ids=("ev_1", "ev_2", "ev_3"),
        )
        for _ in range(5)
    ]
    result = arbitrator.arbitrate(proposals, snapshot)
    assert result.accepted == ()
    assert result.rejected[0].reason_code == RejectionReason.DELTA_EXCEEDS_POLICY
