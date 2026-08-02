"""Society mechanics: groups, the network and the passage of time (spec 20)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.society.groups import GroupEngine
from app.society.models import RECURRING, SIGNIFICANT
from app.society.policy import SocietyPolicy
from app.society.relationships import NPCRelationshipEngine
from app.society.service import SocietyService
from app.storage.repositories.society import (
    GroupRepository,
    NPCInteractionRepository,
    NPCModelRepository,
    NPCRelationshipRepository,
    NPCRepository,
    SocialLinkRepository,
)
from tests.unit.test_psychology import view_with

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def society_policy() -> SocietyPolicy:
    return SocietyPolicy.load(REPO_ROOT / "config" / "policies" / "society.yaml")


@pytest.fixture
def society(db, society_policy, clock) -> SocietyService:
    return SocietyService(
        npcs=NPCRepository(db),
        relationships=NPCRelationshipRepository(db),
        groups=GroupRepository(db),
        links=SocialLinkRepository(db),
        interactions=NPCInteractionRepository(db),
        policy=society_policy,
        clock=clock,
    )


@pytest.fixture
def npc_relationships(db, society_policy, clock) -> NPCRelationshipEngine:
    return NPCRelationshipEngine(
        npcs=NPCRepository(db),
        relationships=NPCRelationshipRepository(db),
        models=NPCModelRepository(db),
        policy=society_policy,
        clock=clock,
    )


@pytest.fixture
def groups(db, society_policy, clock) -> GroupEngine:
    return GroupEngine(GroupRepository(db), society_policy, clock=clock)


# --- people -----------------------------------------------------------------
def test_introducing_the_same_person_twice_is_idempotent(society) -> None:
    first = society.introduce(name="ナオ", tier=RECURRING)
    second = society.introduce(name="ナオ", tier=SIGNIFICANT)

    assert first.npc_id == second.npc_id
    assert len(society.people()) == 1


def test_interactions_are_recorded_per_person(society) -> None:
    a = society.introduce(name="ナオ", tier=RECURRING)
    b = society.introduce(name="ミオ", tier=RECURRING)
    society.interact(a.npc_id, valence=0.3)
    society.interact(a.npc_id, valence=0.1)
    society.interact(b.npc_id, valence=-0.4)

    assert len(society.interactions_with(a.npc_id)) == 2
    assert len(society.interactions_with(b.npc_id)) == 1
    assert len(society.recent_interactions()) == 3


def test_only_tracked_people_are_listed_as_tracked(society) -> None:
    society.introduce(name="通りすがりの人", tier=0)
    society.introduce(name="ナオ", tier=RECURRING)

    assert len(society.people()) == 2
    assert [npc.name for npc in society.tracked_people()] == ["ナオ"]


# --- relationships over time -----------------------------------------------
def test_positive_contact_raises_closeness_and_eases_conflict(
    society, npc_relationships
) -> None:
    npc = society.introduce(name="ナオ", tier=RECURRING)
    bad = society.interact(npc.npc_id, valence=-0.9)
    npc_relationships.record_interaction(npc_id=npc.npc_id, valence=-0.9, event=bad.event)
    after_conflict = npc_relationships.relationship(npc.npc_id).conflict

    good = society.interact(npc.npc_id, valence=0.9)
    npc_relationships.record_interaction(npc_id=npc.npc_id, valence=0.9, event=good.event)
    relationship = npc_relationships.relationship(npc.npc_id)

    assert after_conflict > 0.0
    assert relationship.conflict < after_conflict
    assert relationship.closeness > 0.0


def test_a_stage_change_produces_an_event(society, npc_relationships) -> None:
    npc = society.introduce(name="ナオ", tier=RECURRING)
    record = society.interact(npc.npc_id, valence=0.4)
    outcome = npc_relationships.record_interaction(
        npc_id=npc.npc_id, valence=0.4, event=record.event
    )

    assert outcome.previous_stage == "unmet"
    assert outcome.relationship.stage == "acquaintance"
    assert outcome.stage_changed is True
    assert [event.event_type for event in outcome.events] == [
        "NPC_RELATIONSHIP_STAGE_CHANGED"
    ]


def test_review_stages_walks_distant_then_dormant(
    society, npc_relationships, society_policy, clock
) -> None:
    npc = society.introduce(name="ナオ", tier=RECURRING)
    record = society.interact(npc.npc_id, valence=0.5)
    npc_relationships.record_interaction(npc_id=npc.npc_id, valence=0.5, event=record.event)

    clock.advance(days=society_policy.lifecycle.distant_days + 1)
    npc_relationships.review_stages()
    assert npc_relationships.relationship(npc.npc_id).stage == "distant"

    clock.advance(days=society_policy.lifecycle.dormant_days)
    npc_relationships.review_stages()
    assert npc_relationships.relationship(npc.npc_id).stage == "dormant"


def test_review_stages_writes_no_psychology(society, npc_relationships, clock) -> None:
    npc = society.introduce(name="ナオ", tier=RECURRING)
    record = society.interact(npc.npc_id, valence=0.5)
    npc_relationships.record_interaction(npc_id=npc.npc_id, valence=0.5, event=record.event)

    clock.advance(days=400)
    changed = npc_relationships.review_stages()

    assert changed
    for outcome in changed:
        assert outcome.proposals == ()
        assert outcome.events == ()


def test_reconnecting_reopens_an_ended_relationship(society, npc_relationships) -> None:
    npc = society.introduce(name="ナオ", tier=RECURRING)
    record = society.interact(npc.npc_id, valence=0.5)
    npc_relationships.record_interaction(npc_id=npc.npc_id, valence=0.5, event=record.event)
    npc_relationships.end_relationship(npc.npc_id, reason="離れた")

    reopened = npc_relationships.reconnect(npc.npc_id)
    assert reopened.stage == "reconnected"
    assert reopened.ended_at is None


# --- groups -----------------------------------------------------------------
async def test_group_activity_deepens_belonging(society, groups, society_policy) -> None:
    group = society.found_group(name="読書会")
    society.join_group(group.group_id)
    activity = society.group_activity(group.group_id, activity="読書", valence=0.5)

    view = view_with(**{f"group_belonging__{group.group_id}": 0.2})
    result = await groups.handle(activity, view)

    assert result.proposals[0].value > 0.2
    assert result.proposals[0].value <= society_policy.group.max_belonging


async def test_belonging_cannot_exceed_the_ceiling(society, groups, society_policy) -> None:
    group = society.found_group(name="読書会")
    society.join_group(group.group_id)
    activity = society.group_activity(group.group_id, activity="読書", valence=1.0)

    view = view_with(**{f"group_belonging__{group.group_id}": 0.89})
    result = await groups.handle(activity, view)

    assert result.proposals[0].value == pytest.approx(society_policy.group.max_belonging)


async def test_leaving_a_group_drops_belonging_to_zero(society, groups) -> None:
    group = society.found_group(name="読書会")
    society.join_group(group.group_id)
    event = society.leave_group(group.group_id, reason="合わなかった")

    result = await groups.handle(event, view_with())
    assert result.proposals[0].value == pytest.approx(0.0)


async def test_belonging_fades_when_yui_stops_turning_up(
    society, groups, clock, make_event
) -> None:
    group = society.found_group(name="読書会")
    view = view_with(**{f"group_belonging__{group.group_id}": 0.6})
    clock.advance(days=30)

    result = groups.decay(view=view, event=make_event(), now=clock.now())
    assert result.proposals
    assert result.proposals[0].value < 0.6


async def test_an_unrelated_event_leaves_groups_alone(groups, make_event) -> None:
    assert (await groups.handle(make_event(actor_type="user"), view_with())).proposals == ()


def test_yui_membership_is_listed_separately_from_npc_membership(society) -> None:
    group = society.found_group(name="読書会")
    npc = society.introduce(name="ナオ", tier=RECURRING)
    society.join_group(group.group_id)
    society.join_group(group.group_id, npc_id=npc.npc_id)

    members = society.members(group.group_id)
    assert {member.member_type for member in members} == {"yui", "npc"}
    assert [g.name for g in society.groups_of_yui()] == ["読書会"]
