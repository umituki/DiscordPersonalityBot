"""INVARIANT: goals, habits and other people are actually driven
(rebuild spec 29, 30, 31 — Phase 8).

§31 is one sentence — 既存 engine を Runtime に接続する — and it names the exact
failure §0 opens with. `GoalEngine`, `HabitEngine`, `SocietyService` and
`GroupEngine` have existed, been constructed at startup and passed their unit
tests for a long time, and nothing ever drove them. So the tests that matter
here are not "does the engine work" — that was never in doubt — but "does
anything reach it".

The other rule under test is §30's:

    USER だけを唯一の relatedness source にしない。

If the USER is the only person who can meet a social need, a quiet week is a
deficit and every conversation is relief. That is a dependency, not a life.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.agency.events import GOAL_PURSUED, HABIT_PERFORMED
from app.bootstrap import Application
from app.runtime.agency import (
    DO_HABIT,
    GOAL_STEP,
    HABIT_CUE,
    PURSUE_GOAL,
    AgencyCandidates,
    GoalSource,
    HabitSource,
    present_cues,
)
from app.runtime.social import (
    ATTEND_GROUP,
    CONTACTABLE_TIER,
    CONTACT_NPC,
    GROUP_ACTIVITY_DUE,
    NPC_CONTACT,
    GroupSource,
    NPCSource,
)
from app.society.events import GROUP_ACTIVITY, NPC_INTERACTION
from tests.support import live_shadow, use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


@pytest.fixture
def application(temp_config, clock):
    owned = temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )
    # Phase 14 ships these capabilities in SHADOW; this file is about the
    # mechanism, so it says which mode it means.
    built = Application.build(live_shadow(owned), clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


def _a_goal(application, *, importance: float = 0.9, autonomy: float = 0.9):
    return application.goals.adopt(
        description="日本語をもっと覚える",
        source="interest",
        reason="話せるようになりたい",
        importance=importance,
        autonomy=autonomy,
        value_alignment=0.9,
    )


def _an_established_habit(application, cue: str = "手持ち無沙汰"):
    """Repeat something until it is automatic. Spec 31.2: not a fixed count of
    days — the automaticity is tracked, and that is what makes it fire."""
    habit = None
    for _ in range(40):
        outcome = application.habits.observe(
            name="お茶をいれる",
            cue=cue,
            action="お茶をいれる",
            cue_present=True,
            performed=True,
        )
        habit = outcome.habit
    return habit


# --- §31.3: goals and habits are always candidate sources --------------------


def test_both_are_registered_as_sources(application) -> None:
    """必ず候補源にする. Not "when the loop thinks of it"."""
    names = {source.name for source in application.runtime._registry.sources}
    assert {"goals", "habits"} <= names


def test_every_agency_kind_has_a_builder_and_a_handler(application) -> None:
    registry = application.runtime._registry
    for kind in (GOAL_STEP, HABIT_CUE, NPC_CONTACT, GROUP_ACTIVITY_DUE):
        assert registry.builder_for(kind) is not None, kind
    for action in (PURSUE_GOAL, DO_HABIT, CONTACT_NPC, ATTEND_GROUP):
        assert registry.handler_for(action) is not None, action


async def test_an_active_goal_is_offered(application, clock) -> None:
    goal = _a_goal(application)
    source = GoalSource(application.goals, application.world, clock=clock)

    offered = list(source.collect(clock.now()))

    assert [item.detail for item in offered] == [goal.goal_id]


async def test_a_goal_just_worked_on_is_left_alone(application, clock) -> None:
    """Otherwise she pursues the same goal every wake-up, which is not
    diligence."""
    goal = _a_goal(application)
    application.goals.touch(goal.goal_id)
    source = GoalSource(application.goals, application.world, clock=clock)

    assert list(source.collect(clock.now())) == []

    clock.advance(hours=7)
    assert len(list(source.collect(clock.now()))) == 1


# --- the engines are actually reached ----------------------------------------


async def test_a_goal_step_starts_real_work(application, clock) -> None:
    goal = _a_goal(application)

    tick = await application.runtime.tick()

    assert tick.chosen_action == PURSUE_GOAL
    assert tick.executed is True

    # Real work is under way: an activity, tied to a plan, tied to the goal.
    activity = application.world.current_activity()
    assert activity is not None
    assert activity.plan_id is not None
    assert application.goals.goal(goal.goal_id).last_pursued_at is not None

    pursued = next(
        event
        for event in application.event_store.recent(limit=10)
        if event.event_type == GOAL_PURSUED
    )
    assert pursued.payload.goal_id == goal.goal_id
    assert pursued.payload.plan_id == activity.plan_id
    assert pursued.payload.activity_id == activity.activity_id


async def test_starting_work_does_not_advance_the_goal(application, clock) -> None:
    """ACT-001 and spec 2.15 together: a plan counted as an experience is the
    exact failure both rules exist to prevent."""
    goal = _a_goal(application)

    await application.runtime.tick()

    after = application.goals.goal(goal.goal_id)
    assert after.progress == goal.progress
    assert after.last_pursued_at is not None  # she did work on it


async def test_finishing_the_work_is_still_the_activity_lifecycle(
    application, clock
) -> None:
    """One completion path, not a private one for goal work."""
    _a_goal(application)
    await application.runtime.tick()
    started = application.world.current_activity()

    clock.advance(minutes=60)
    await application.runtime.tick()

    completed = application.world.completed_activities()
    assert [item.activity_id for item in completed] == [started.activity_id]


async def test_an_established_habit_fires(application, clock) -> None:
    habit = _an_established_habit(application)
    assert habit.automaticity > 0
    clock.advance(hours=12)  # past the per-habit cooldown

    source = HabitSource(
        application.habits,
        application.world,
        policy=application.agency_policy.habits,
        clock=clock,
    )
    offered = list(source.collect(clock.now()))

    assert [item.detail for item in offered] == [habit.habit_id]


async def test_performing_a_habit_strengthens_it_through_the_engine(
    application, clock
) -> None:
    habit = _an_established_habit(application)
    before = habit.automaticity
    clock.advance(hours=12)

    tick = await application.runtime.tick()

    assert tick.executed is True
    after = application.habits.get(habit.habit_id)
    # The engine counted a real pairing: the cue was there and she acted on it.
    assert after.repetitions == habit.repetitions + 1
    assert after.cue_encounters == habit.cue_encounters + 1
    assert after.last_performed_at is not None
    # Automaticity is not asserted to have risen: disuse decay runs over the
    # twelve hours as well, and pretending otherwise would test the fixture
    # rather than the engine. What matters is that the pairing was recorded.
    assert after.status == "established"
    types = {event.event_type for event in application.event_store.recent(limit=10)}
    assert HABIT_PERFORMED in types


def test_looking_for_a_cue_is_not_encountering_one(application, clock) -> None:
    """The source reads; only the handler tells the engine anything happened.

    ``cue_encountered`` increments the counter, so a source that used it would
    strengthen every habit merely by being run.
    """
    habit = _an_established_habit(application)
    clock.advance(hours=12)
    source = HabitSource(
        application.habits,
        application.world,
        policy=application.agency_policy.habits,
        clock=clock,
    )

    for _ in range(5):
        source.collect(clock.now())

    after = application.habits.get(habit.habit_id)
    assert after.cue_encounters == habit.cue_encounters
    assert after.automaticity == habit.automaticity


def test_the_cue_vocabulary_is_declared(clock) -> None:
    """A habit system whose cues are a fuzzy match against the whole world
    state stops being explicable."""
    cues = present_cues(clock.now().replace(hour=15), None)

    assert cues
    assert "手持ち無沙汰" in cues
    assert all(isinstance(cue, str) for cue in cues)


# --- §29: people, and the tiering that keeps them affordable -----------------


async def test_background_people_are_not_contacted(application, clock) -> None:
    """§29.1: 全 NPC を完全 Agent にしない. Scenery does not get phoned."""
    application.society.introduce(name="通行人", tier=0)
    source = NPCSource(application.society, application.world, clock=clock)

    assert list(source.collect(clock.now())) == []


async def test_someone_who_recurs_can_be_contacted(application, clock) -> None:
    npc = application.society.introduce(name="ミカ", tier=CONTACTABLE_TIER, availability=0.8)
    source = NPCSource(application.society, application.world, clock=clock)

    offered = list(source.collect(clock.now()))

    assert [item.detail for item in offered] == [npc.npc_id]


async def test_contacting_someone_goes_through_the_society_service(
    application, clock
) -> None:
    """§29.5: Decision / SocietyService が commit した時だけ本当に起きる."""
    npc = application.society.introduce(name="ミカ", tier=1, availability=0.9, warmth=0.8)

    tick = await application.runtime.tick()

    assert tick.chosen_action == CONTACT_NPC
    assert tick.executed is True
    history = application.society.interactions_with(npc.npc_id)
    assert len(history) == 1
    types = {event.event_type for event in application.event_store.recent(limit=10)}
    assert NPC_INTERACTION in types


async def test_returning_to_someone_promotes_them(application, clock) -> None:
    """§29.2/29.3: 繰り返し関われば promote. Counted from the interaction rows,
    so it survives a restart and can be argued with."""
    npc = application.society.introduce(name="ミカ", tier=1, availability=0.9, warmth=0.9)
    application.state.write_value(
        domain="needs", key="relatedness", value=0.05, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=None,
    )

    # Live several days. Each pass gives her time to finish whatever she is
    # doing and for the contact cooldown to lapse.
    for _ in range(40):
        await application.runtime.tick()
        clock.advance(hours=6)
        if application.society.npc(npc.npc_id).tier == 2:
            break

    after = application.society.npc(npc.npc_id)
    assert after.tier == 2, "someone she keeps returning to is not background"
    assert len(application.society.interactions_with(npc.npc_id)) >= 5


async def test_a_person_is_not_contacted_twice_in_a_row(application, clock) -> None:
    application.society.introduce(name="ミカ", tier=1, availability=0.9)
    source = NPCSource(application.society, application.world, clock=clock)
    await application.runtime.tick()

    assert list(source.collect(clock.now())) == []


# --- §30: the USER is not the only source of relatedness ---------------------


async def test_a_group_meeting_is_an_opportunity(application, clock) -> None:
    group = application.society.found_group(name="読書会", activity_type="読書")
    application.society.join_group(group.group_id)
    source = GroupSource(application.society, application.world, clock=clock)

    offered = list(source.collect(clock.now()))

    assert [item.detail for item in offered] == [group.group_id]


async def test_company_matters_more_when_she_is_lonely(application, clock) -> None:
    """The mechanism §30 asks for. Without this the alternative sources exist
    on paper and never win."""
    from app.runtime.social import SocialCandidates
    from app.world.models import Opportunity

    application.society.introduce(name="ミカ", tier=1, warmth=0.8)
    npc = application.society.by_name("ミカ")
    candidates = SocialCandidates(application.society, application.state)
    opportunity = Opportunity(kind=NPC_CONTACT, detail=npc.npc_id, created_at=clock.now())

    application.state.write_value(
        domain="needs", key="relatedness", value=0.9, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=None,
    )
    satisfied = candidates.npc_contact(opportunity, clock.now())

    entry = application.state.get("needs", "relatedness")
    application.state.write_value(
        domain="needs", key="relatedness", value=0.1, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=entry.version,
    )
    lonely = candidates.npc_contact(opportunity, clock.now())

    assert lonely.expected_value > satisfied.expected_value


async def test_attending_a_group_produces_a_group_event(application, clock) -> None:
    group = application.society.found_group(name="読書会", activity_type="読書")
    application.society.join_group(group.group_id)
    member = application.society.introduce(name="リン", tier=0)
    application.society.join_group(group.group_id, npc_id=member.npc_id)

    # Make company the best thing available.
    application.state.write_value(
        domain="needs", key="relatedness", value=0.05, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=None,
    )

    for _ in range(3):
        tick = await application.runtime.tick()
        if tick.chosen_action == ATTEND_GROUP:
            break
        clock.advance(hours=3)
    else:  # pragma: no cover - the loop above is the assertion
        pytest.fail("a group she belongs to was never attended")

    types = {event.event_type for event in application.event_store.recent(limit=20)}
    assert GROUP_ACTIVITY in types
    assert application.world.current_activity() is not None


# --- the loop's rules still hold ---------------------------------------------


async def test_nothing_new_is_unclaimed(application, clock) -> None:
    _a_goal(application)
    application.society.introduce(name="ミカ", tier=1)

    await application.runtime.tick()

    assert application.runtime_ticks.unclaimed_kinds() == []


async def test_a_user_turn_still_defers_everything(application, clock) -> None:
    _a_goal(application)

    with application.runtime.user_turn():
        tick = await application.runtime.tick()

    assert tick.candidates > 0
    assert tick.outcome == "deferred"
    assert application.goals.open_plans() == []


async def test_none_of_this_runs_while_she_is_asleep(application, clock) -> None:
    """Spec 24.2 and common sense: she is not phoning anybody at four a.m."""
    _a_goal(application)
    application.society.introduce(name="ミカ", tier=1, availability=0.9)
    clock.set(clock.now().replace(hour=3, minute=0))
    application.state.write_value(
        domain="world", key="sleep_pressure", value=1.0, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=None,
    )
    await application.runtime.tick()
    assert application.world.current_sleep() is not None

    tick = await application.runtime.tick()

    assert tick.opportunities == 0 or all(
        kind == "wake_candidate" for kind in tick.opportunity_kinds
    )


def test_the_runtime_modules_still_write_no_psychological_state() -> None:
    """RUNTIME-001 across everything Phase 8 added."""
    import ast
    from pathlib import Path

    for module in ("agency.py", "social.py"):
        tree = ast.parse((Path("app/runtime") / module).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        for forbidden in ("app.psychology", "app.state", "app.consolidation"):
            assert not any(name.startswith(forbidden) for name in imported), (
                module,
                forbidden,
            )
