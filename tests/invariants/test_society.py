"""INVARIANT: the virtual society never contaminates the real relationship.

* USER and NPC are never mixed (spec 1.2, 2.10, 34.2-4). Different domains,
  different writers, different origins — in both directions.
* An NPC's objective profile and YUI's model of them are different things, and
  the model is allowed to be wrong (spec 20.2).
* Not every NPC is a full agent (spec 20.1).
* Losing contact and falling out are different (spec 20.4).
* Belonging to a group is a real source of relatedness, so the USER is not the
  only one (spec 20.3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import Application
from app.psychology.needs import NeedEngine
from app.social.policy import RelationshipPolicy
from app.social.relationship import RelationshipEngine
from app.society import lifecycle
from app.society.groups import GroupEngine, belonging_of
from app.society.models import BACKGROUND, RECURRING, SIGNIFICANT
from app.society.policy import SocietyPolicy
from app.society.relationships import NPCRelationshipEngine, state_key
from app.society.service import SocietyError, SocietyService
from app.state import dependency_graph, ownership
from app.storage.repositories.society import (
    GroupRepository,
    NPCInteractionRepository,
    NPCModelRepository,
    NPCRelationshipRepository,
    NPCRepository,
    SocialLinkRepository,
)
from tests.unit.test_psychology import psychology_policy, view_with  # noqa: F401

pytestmark = pytest.mark.invariant

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


# --- separation of USER and NPC (spec 2.10) --------------------------------
def test_the_user_relationship_and_npc_relationships_have_different_writers() -> None:
    assert ownership.owner_of("relationship") == RelationshipEngine.name
    assert ownership.owner_of("npc_relationships") == NPCRelationshipEngine.name
    assert not ownership.may_write("relationship", NPCRelationshipEngine.name)
    assert not ownership.may_write("npc_relationships", RelationshipEngine.name)


def test_society_domains_are_adaptive_not_deep() -> None:
    for domain in ("npc_relationships", "group_belonging"):
        assert dependency_graph.layer_of(domain) == dependency_graph.ADAPTIVE


async def test_an_npc_event_never_touches_the_user_relationship(
    society, npc_relationships, make_event
) -> None:
    npc = society.introduce(name="ナオ", tier=RECURRING)
    record = society.interact(npc.npc_id, valence=0.8, summary="よく話した")

    user_engine = RelationshipEngine(
        RelationshipPolicy.load(REPO_ROOT / "config" / "policies" / "relationship.yaml")
    )
    npc_result = await npc_relationships.handle(record.event, view_with())
    user_engine_result = await user_engine.handle(record.event, view_with())

    assert npc_result.proposals
    assert {p.target_domain for p in npc_result.proposals} == {"npc_relationships"}
    # The USER relationship engine simply does not answer to an NPC.
    assert user_engine_result.proposals == ()


async def test_a_user_message_never_touches_npc_state(npc_relationships, make_event) -> None:
    result = await npc_relationships.handle(make_event(actor_type="user"), view_with())
    assert result.proposals == ()
    assert result.events == ()


def test_an_npc_is_never_recorded_as_real_discord(society) -> None:
    npc = society.introduce(name="ナオ", tier=RECURRING)
    record = society.interact(npc.npc_id, valence=0.5)

    assert npc.origin == "virtual_life"
    assert record.interaction.origin == "virtual_life"
    assert record.event.origin == "virtual_life"
    assert record.event.actor_type == "npc"


async def test_the_full_pipeline_keeps_the_two_apart(temp_config, clock, make_event) -> None:
    """End to end: a real USER message and an NPC interaction in one process."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        await application.processor.process(make_event(actor_type="user"))
        npc = application.society.introduce(name="ナオ", tier=SIGNIFICANT)
        record = application.society.interact(npc.npc_id, valence=0.7, summary="散歩した")
        await application.processor.process(record.event)

        user_state = application.state.list_domain("relationship")
        npc_state = application.state.list_domain("npc_relationships")

        assert user_state, "the USER relationship must exist"
        assert npc_state, "the NPC relationship must exist"
        assert {value.key for value in user_state}.isdisjoint(
            {value.key for value in npc_state}
        )
        # The USER relationship was written by the USER message alone.
        for value in user_state:
            assert value.updated_by_event_id != record.event.event_id
    finally:
        application.db.close()


# --- tiers (spec 20.1) ------------------------------------------------------
def test_a_background_person_is_not_tracked(society, npc_relationships) -> None:
    npc = society.introduce(name="通りすがりの人", tier=BACKGROUND)
    record = society.interact(npc.npc_id, valence=0.9)
    outcome = npc_relationships.record_interaction(
        npc_id=npc.npc_id, valence=0.9, event=record.event
    )

    assert npc.keeps_relationship_state is False
    assert outcome.proposals == ()
    assert npc_relationships.relationship(npc.npc_id) is None


def test_only_a_significant_npc_gets_a_model(society, npc_relationships) -> None:
    recurring = society.introduce(name="ナオ", tier=RECURRING)
    significant = society.introduce(name="ミオ", tier=SIGNIFICANT)

    for npc in (recurring, significant):
        record = society.interact(npc.npc_id, valence=0.6)
        npc_relationships.record_interaction(
            npc_id=npc.npc_id, valence=0.6, event=record.event
        )

    assert npc_relationships.model_of(recurring.npc_id) is None
    assert npc_relationships.model_of(significant.npc_id) is not None


def test_a_background_person_can_become_someone(society) -> None:
    npc = society.introduce(name="ナオ", tier=BACKGROUND)
    promoted = society.promote(npc.npc_id, tier=SIGNIFICANT)

    assert promoted.tier == SIGNIFICANT
    assert promoted.keeps_relationship_state is True


# --- profile vs model (spec 20.2) ------------------------------------------
def test_yuis_model_of_an_npc_may_disagree_with_the_truth(society, npc_relationships) -> None:
    npc = society.introduce(name="ミオ", tier=SIGNIFICANT, warmth=0.9)
    for _ in range(3):
        record = society.interact(npc.npc_id, valence=-0.8, summary="そっけなかった")
        npc_relationships.record_interaction(
            npc_id=npc.npc_id, valence=-0.8, event=record.event
        )

    model = npc_relationships.model_of(npc.npc_id)
    assert society.npc(npc.npc_id).warmth == pytest.approx(0.9)
    assert model.perceived_warmth < 0.5
    assert model.observation_count == 3


def test_the_model_grows_confident_but_never_certain(society, npc_relationships) -> None:
    npc = society.introduce(name="ミオ", tier=SIGNIFICANT)
    for _ in range(40):
        record = society.interact(npc.npc_id, valence=0.5)
        npc_relationships.record_interaction(
            npc_id=npc.npc_id, valence=0.5, event=record.event
        )

    assert npc_relationships.model_of(npc.npc_id).confidence < 1.0


# --- lifecycle (spec 20.4) --------------------------------------------------
def test_silence_makes_a_relationship_distant_not_strained(society_policy) -> None:
    rules = society_policy.lifecycle
    stage = lifecycle.stage_for(
        previous_stage="close",
        familiarity=0.8,
        closeness=0.7,
        conflict=0.0,
        days_since_contact=rules.distant_days + 1,
        interaction_count=20,
        policy=rules,
    )
    assert stage == "distant"
    assert lifecycle.is_absence(stage)
    assert not lifecycle.is_trouble(stage)


def test_long_silence_becomes_dormant_and_never_ended(society_policy) -> None:
    rules = society_policy.lifecycle
    stage = lifecycle.stage_for(
        previous_stage="distant",
        familiarity=0.8,
        closeness=0.7,
        conflict=0.0,
        days_since_contact=rules.dormant_days * 5,
        interaction_count=20,
        policy=rules,
    )
    assert stage == "dormant"
    assert stage != "ended"


def test_conflict_strains_a_relationship_even_with_daily_contact(society_policy) -> None:
    stage = lifecycle.stage_for(
        previous_stage="close",
        familiarity=0.9,
        closeness=0.8,
        conflict=society_policy.lifecycle.strained_conflict + 0.05,
        days_since_contact=0.0,
        interaction_count=50,
        policy=society_policy.lifecycle,
    )
    assert stage == "strained"
    assert lifecycle.is_trouble(stage)


def test_ending_is_a_decision_never_a_consequence_of_time(
    society, npc_relationships, society_policy, clock
) -> None:
    npc = society.introduce(name="ナオ", tier=RECURRING)
    record = society.interact(npc.npc_id, valence=0.5)
    npc_relationships.record_interaction(npc_id=npc.npc_id, valence=0.5, event=record.event)

    clock.advance(days=society_policy.lifecycle.dormant_days * 10)
    npc_relationships.review_stages()
    assert npc_relationships.relationship(npc.npc_id).stage == "dormant"

    ended = npc_relationships.end_relationship(npc.npc_id, reason="決めたこと")
    assert ended.stage == "ended"
    assert ended.ended_reason == "決めたこと"

    # And once ended, time does not quietly reopen it either.
    clock.advance(days=30)
    npc_relationships.review_stages()
    assert npc_relationships.relationship(npc.npc_id).stage == "ended"


def test_coming_back_after_a_gap_is_its_own_stage(society_policy) -> None:
    stage = lifecycle.stage_for(
        previous_stage="dormant",
        familiarity=0.6,
        closeness=0.5,
        conflict=0.0,
        days_since_contact=0.0,
        interaction_count=10,
        policy=society_policy.lifecycle,
    )
    assert stage == "reconnected"


def test_an_unmet_person_has_no_stage_of_their_own(society_policy) -> None:
    assert (
        lifecycle.stage_for(
            previous_stage="",
            familiarity=0.0,
            closeness=0.0,
            conflict=0.0,
            days_since_contact=None,
            interaction_count=0,
            policy=society_policy.lifecycle,
        )
        == "unmet"
    )


def test_contact_alone_is_familiarity_not_closeness(society, npc_relationships) -> None:
    """Spec 13.1 applies to NPCs too: showing up is not intimacy."""
    npc = society.introduce(name="ナオ", tier=RECURRING)
    for _ in range(6):
        record = society.interact(npc.npc_id, valence=0.0, summary="事務的な話")
        npc_relationships.record_interaction(
            npc_id=npc.npc_id, valence=0.0, event=record.event
        )

    relationship = npc_relationships.relationship(npc.npc_id)
    assert relationship.familiarity > 0.0
    assert relationship.closeness == pytest.approx(0.0)


# --- groups (spec 20.3) -----------------------------------------------------
def test_group_belonging_has_its_own_writer() -> None:
    assert ownership.owner_of("group_belonging") == GroupEngine.name
    assert not ownership.may_write("needs", GroupEngine.name)
    assert not ownership.may_write("group_belonging", NeedEngine.name)


async def test_joining_a_group_creates_belonging(society, groups) -> None:
    group = society.found_group(name="読書会", activity_type="reading")
    event = society.join_group(group.group_id)

    result = await groups.handle(event, view_with())
    assert [p.target_key for p in result.proposals] == [group.group_id]
    assert result.proposals[0].target_domain == "group_belonging"


async def test_taking_part_in_a_group_you_never_joined_is_not_belonging(
    society, groups
) -> None:
    group = society.found_group(name="読書会")
    event = society.group_activity(group.group_id, activity="読書", valence=0.5)

    result = await groups.handle(event, view_with())
    assert result.proposals == ()


async def test_belonging_keeps_relatedness_from_collapsing(
    psychology_policy, clock, make_event  # noqa: F811
) -> None:
    """Spec 20.3: the USER must not be the only source of relatedness."""
    engine = NeedEngine(psychology_policy.needs, clock=clock)
    lonely = view_with(needs__relatedness_satisfaction=0.05)
    with_friends = view_with(
        needs__relatedness_satisfaction=0.05, group_belonging__grp_reading=0.8
    )

    alone = await engine.handle(make_event(actor_type="yui"), lonely)
    among_others = await engine.handle(make_event(actor_type="yui"), with_friends)

    def relatedness(result, default):
        for proposal in result.proposals:
            if proposal.target_key == "relatedness_satisfaction":
                return proposal.value
        return default

    assert relatedness(among_others, 0.05) > relatedness(alone, 0.05)


def test_belonging_reads_the_strongest_tie_not_the_sum() -> None:
    view = view_with(group_belonging__a=0.4, group_belonging__b=0.5)
    assert belonging_of(view) == pytest.approx(0.5)
    assert belonging_of(view_with()) == 0.0


# --- the network (spec 20) --------------------------------------------------
def test_npcs_know_each_other_without_yui(society) -> None:
    a = society.introduce(name="ナオ", tier=RECURRING)
    b = society.introduce(name="ミオ", tier=RECURRING)
    link = society.connect(a.npc_id, b.npc_id, kind="friend", strength=0.6)

    assert link.strength == pytest.approx(0.6)
    assert len(society.neighbours(a.npc_id)) == 1
    assert len(society.neighbours(b.npc_id)) == 1


def test_a_link_needs_two_real_people(society) -> None:
    a = society.introduce(name="ナオ", tier=RECURRING)

    with pytest.raises(SocietyError):
        society.connect(a.npc_id, a.npc_id)
    with pytest.raises(SocietyError):
        society.connect(a.npc_id, "npc_does_not_exist")


def test_an_npc_needs_a_name(society) -> None:
    with pytest.raises(SocietyError):
        society.introduce(name="   ")


def test_npc_state_keys_stay_separated_per_person(society) -> None:
    a = society.introduce(name="ナオ", tier=RECURRING)
    b = society.introduce(name="ミオ", tier=RECURRING)

    assert state_key(a.npc_id, "closeness") != state_key(b.npc_id, "closeness")
