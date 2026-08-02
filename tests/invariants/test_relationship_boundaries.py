"""Relationship and attachment (spec 13, 34.2-6, 34.2-7).

Two of the specification's named critical invariants live here:

6. ``message count だけで relationship を最大化しない``
7. ``apology だけで major trust damage を即全回復しない``
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.social.attachment import (
    ACTIVATION,
    FELT_SECURITY,
    PROXIMITY_DESIRE,
    REASSURANCE_NEED,
    AttachmentEngine,
)
from app.social.policy import RelationshipPolicy
from app.social.relationship import (
    CLOSENESS,
    CONFLICT_RESIDUE,
    FAMILIARITY,
    TRUST,
    RelationshipEngine,
)
from app.social.signals import signals_from
from tests.unit.test_psychology import GOOD_NEWS, view_with, with_appraisal

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def relationship_policy() -> RelationshipPolicy:
    return RelationshipPolicy.load(REPO_ROOT / "config" / "policies" / "relationship.yaml")


@pytest.fixture
def relationship_engine(relationship_policy, clock) -> RelationshipEngine:
    return RelationshipEngine(relationship_policy, clock=clock)


@pytest.fixture
def attachment_engine(relationship_policy, clock) -> AttachmentEngine:
    return AttachmentEngine(relationship_policy.attachment, clock=clock)


def values_of(result) -> dict[str, float]:
    return {proposal.target_key: proposal.value for proposal in result.proposals}


async def run_many(engine, make_event, clock, view_values: dict, *, times: int, text: str = "やあ"):
    """Feed the engine repeated ordinary contact, carrying state forward."""
    state = dict(view_values)
    for _ in range(times):
        view = with_appraisal(view_with(**state), GOOD_NEWS)
        result = await engine.handle(
            make_event(actor_type="user", payload=None, text=text), view
        )
        for key, value in values_of(result).items():
            state[f"relationship__{key}"] = value
    return state


# --- 34.2-6: chatter is not intimacy ---------------------------------------


async def test_talking_a_lot_only_raises_familiarity(
    relationship_engine, make_event, clock
) -> None:
    start = {
        "relationship__trust": 0.5,
        "relationship__familiarity": 0.1,
        "relationship__emotional_closeness": 0.3,
        "relationship__security": 0.5,
    }
    end = await run_many(relationship_engine, make_event, clock, start, times=200)

    assert end["relationship__familiarity"] > start["relationship__familiarity"]
    # Trust did not move at all: nothing in 200 messages was evidence of it.
    assert end["relationship__trust"] == pytest.approx(0.5)
    assert end["relationship__familiarity"] < 1.0


async def test_a_single_message_moves_almost_nothing(
    relationship_engine, make_event, clock, relationship_policy
) -> None:
    view = with_appraisal(view_with(relationship__familiarity=0.1), GOOD_NEWS)
    result = await relationship_engine.handle(make_event(actor_type="user"), view)

    for proposal in result.proposals:
        assert abs(proposal.value - 0.1) < 0.05 or proposal.target_key != FAMILIARITY
    assert values_of(result)[FAMILIARITY] - 0.1 <= (
        relationship_policy.dimensions.familiarity.max_per_event + 1e-9
    )


async def test_trust_needs_evidence_not_conversation(
    relationship_engine, make_event, clock
) -> None:
    plain = with_appraisal(view_with(relationship__trust=0.5), GOOD_NEWS)
    plain_result = await relationship_engine.handle(
        make_event(actor_type="user", text="こんばんは"), plain
    )
    assert TRUST not in values_of(plain_result)

    kept = with_appraisal(view_with(relationship__trust=0.5), GOOD_NEWS)
    kept_result = await relationship_engine.handle(
        make_event(actor_type="user", text="約束どおりやっておいたよ"), kept
    )
    assert values_of(kept_result)[TRUST] > 0.5


# --- 34.2-7: an apology is not a repair ------------------------------------


async def test_apology_alone_does_not_restore_trust(
    relationship_engine, make_event, clock
) -> None:
    damaged = with_appraisal(
        view_with(relationship__trust=0.2, relationship__conflict_residue=0.4), GOOD_NEWS
    )

    result = await relationship_engine.handle(
        make_event(actor_type="user", text="ごめん、ほんとうにごめん"), damaged
    )
    values = values_of(result)

    # It helps with the residue…
    assert values[CONFLICT_RESIDUE] < 0.4
    # …and does nothing for trust.
    assert TRUST not in values


async def test_trust_returns_only_after_repeated_consistency(
    relationship_engine, make_event, clock, relationship_policy
) -> None:
    state = {"relationship__trust": 0.2, "relationship__conflict_residue": 0.3}
    required = relationship_policy.repair.consistent_behaviour.required_events

    # Well short of the required consistent behaviour: still nothing.
    partial = await run_many(relationship_engine, make_event, clock, state, times=required - 1)
    assert partial["relationship__trust"] == pytest.approx(0.2)

    # Keep going, and trust finally starts to move — slowly.
    recovered = await run_many(
        relationship_engine, make_event, clock, partial, times=required + 5
    )
    assert recovered["relationship__trust"] > 0.2
    assert recovered["relationship__trust"] < 0.4  # nowhere near restored


async def test_severe_damage_leaves_residue_behind(
    relationship_engine, make_event, clock
) -> None:
    """Spec 13.3: unresolved hurt survives apology and emotional recovery."""
    hurt = 0.15
    residue = 0.4

    for _ in range(10):
        view = with_appraisal(
            view_with(
                relationship__conflict_residue=residue,
                relationship__unresolved_hurt=hurt,
            ),
            GOOD_NEWS,
        )
        result = await relationship_engine.handle(
            make_event(actor_type="user", text="ごめんね"), view
        )
        residue = values_of(result).get(CONFLICT_RESIDUE, residue)

    # Repeated apologies never take the relationship below what was broken.
    assert residue >= hurt


def test_conflict_kinds_are_distinct(relationship_policy) -> None:
    rules = relationship_policy.dimensions.conflict_residue
    assert rules.disagreement < rules.conflict < rules.transgression < rules.betrayal


def test_a_negative_message_is_at_most_a_disagreement(make_event) -> None:
    """Spec 13.3: conflict is not an automatic penalty, and not an escalation."""
    from app.psychology.models import Appraisal

    mildly_negative = Appraisal(
        self_relevance=0.6, goal_congruence=-0.5, social_meaning=-0.6, certainty=0.6
    )
    signals = signals_from(make_event(actor_type="user"), mildly_negative)
    assert signals.conflict_kind in ("disagreement", "conflict")
    assert not signals.is_severe


# --- attachment (spec 13.2) -------------------------------------------------


async def test_security_buys_tolerance_for_separation(
    attachment_engine, make_event, clock
) -> None:
    absence = timedelta(days=5)

    insecure = view_with(
        updated_at=clock.now() - absence,
        attachment__current_activation=0.2,
        attachment__felt_security=0.2,
    )
    secure = view_with(
        updated_at=clock.now() - absence,
        attachment__current_activation=0.2,
        attachment__felt_security=0.9,
    )

    insecure_result = values_of(
        await attachment_engine.handle(make_event(actor_type="user"), insecure)
    )
    secure_result = values_of(
        await attachment_engine.handle(make_event(actor_type="user"), secure)
    )

    # The same absence activates the insecure bond more.
    assert insecure_result[ACTIVATION] > secure_result.get(ACTIVATION, 0.0)
    assert secure_result[FELT_SECURITY] > insecure_result[FELT_SECURITY]


async def test_closeness_is_not_dependency(attachment_engine, make_event, clock) -> None:
    """Spec 13.2: becoming closer must not simply mean needing more."""
    anxious = view_with(
        attachment__current_activation=0.6, attachment__felt_security=0.2
    )
    settled = view_with(
        attachment__current_activation=0.6, attachment__felt_security=0.9
    )

    anxious_values = values_of(
        await attachment_engine.handle(make_event(actor_type="user"), anxious)
    )
    settled_values = values_of(
        await attachment_engine.handle(make_event(actor_type="user"), settled)
    )

    assert settled_values[REASSURANCE_NEED] < anxious_values[REASSURANCE_NEED]
    assert settled_values[PROXIMITY_DESIRE] <= anxious_values[PROXIMITY_DESIRE]


async def test_attachment_never_writes_the_disposition(
    attachment_engine, make_event, clock
) -> None:
    """Deep disposition is Layer 4 and belongs to consolidation (spec 13.2)."""
    result = await attachment_engine.handle(
        make_event(actor_type="user"), view_with(attachment__felt_security=0.5)
    )
    domains = {proposal.target_domain for proposal in result.proposals}
    assert domains == {"attachment"}
    assert "attachment_disposition" not in domains


async def test_relationship_and_attachment_do_not_write_each_other(
    relationship_engine, attachment_engine, make_event, clock
) -> None:
    view = with_appraisal(
        view_with(relationship__trust=0.5, attachment__felt_security=0.5), GOOD_NEWS
    )

    relationship_result = await relationship_engine.handle(make_event(actor_type="user"), view)
    attachment_result = await attachment_engine.handle(make_event(actor_type="user"), view)

    assert {p.target_domain for p in relationship_result.proposals} == {"relationship"}
    assert {p.target_domain for p in attachment_result.proposals} == {"attachment"}


async def test_every_relationship_change_carries_evidence(
    relationship_engine, make_event, clock
) -> None:
    """Spec 25: long-lived state is not created without provenance."""
    event = make_event(actor_type="user")
    result = await relationship_engine.handle(
        event, with_appraisal(view_with(relationship__trust=0.5), GOOD_NEWS)
    )

    assert result.proposals
    for proposal in result.proposals:
        assert proposal.evidence_ids == (event.event_id,)
        assert proposal.reason_codes
