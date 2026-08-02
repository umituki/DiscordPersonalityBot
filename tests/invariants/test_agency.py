"""INVARIANT: goals, habits, decisions and epistemic actions (spec 15, 16.1, 17.1).

Named rules defended here:

* a plan is never a completed experience (spec 2.15, 18.2),
* a habit is cue-triggered automaticity, not a streak: a missed day does not
  reset it, and a vanished context leaves a trace (spec 15.3),
* close candidates may waver, clearly better ones may not (spec 15.4),
* not knowing something does not trigger a search (spec 17.1),
* a failed search leaves an honest unresolved gap (spec 17.3).
"""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

import pytest

from app.agency.decision import DecisionEngine
from app.agency.goals import GoalEngine
from app.agency.habits import HabitEngine
from app.agency.models import ActionCandidate
from app.agency.policy import AgencyPolicy
from app.epistemics.actions import EpistemicActionSelector, KnowledgeGap, unresolved_gap
from app.storage.repositories.agency import (
    DecisionRepository,
    GoalRepository,
    HabitRepository,
    PlanRepository,
)
from tests.unit.test_psychology import view_with

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def agency_policy() -> AgencyPolicy:
    return AgencyPolicy.load(REPO_ROOT / "config" / "policies" / "agency.yaml")


@pytest.fixture
def goals(db, agency_policy, clock) -> GoalEngine:
    return GoalEngine(
        GoalRepository(db), PlanRepository(db), agency_policy.goals, clock=clock
    )


@pytest.fixture
def habits(db, agency_policy, clock) -> HabitEngine:
    return HabitEngine(HabitRepository(db), agency_policy.habits, clock=clock)


@pytest.fixture
def decisions(db, agency_policy, clock) -> DecisionEngine:
    return DecisionEngine(
        DecisionRepository(db), agency_policy.decision, clock=clock, rng=random.Random(7)
    )


@pytest.fixture
def epistemics(agency_policy, clock) -> EpistemicActionSelector:
    return EpistemicActionSelector(agency_policy.epistemic, clock=clock)


# --- goals and plans --------------------------------------------------------


def test_a_pressing_need_becomes_a_goal(goals, agency_policy) -> None:
    view = view_with(needs__loneliness=0.8, needs__connection_desire=0.7)

    activations = goals.derive_from_state(view)

    assert activations
    assert any("話したい" in item.goal.description for item in activations)
    assert all(item.goal.reason for item in activations)  # goals carry why


def test_a_mild_need_does_not(goals) -> None:
    view = view_with(needs__loneliness=0.2, needs__connection_desire=0.1)
    assert goals.derive_from_state(view) == []


def test_a_plan_is_not_an_experience(goals) -> None:
    """Spec 2.15: ``未実行 Plan を completed experience として保存しない``."""
    goal = goals.adopt(description="散歩の話をする", source="interest", reason="興味")
    plan = goals.plan(goal_id=goal.goal_id, description="今晩その話をする")

    assert plan.status == "planned"
    assert plan.has_happened is False
    assert goals.completed_experiences() == []
    assert goals.open_plans()[0].plan_id == plan.plan_id


def test_a_missed_plan_never_becomes_a_completion(goals) -> None:
    plan = goals.plan(goal_id=None, description="やろうと思っていたこと")
    missed = goals.miss_plan(plan.plan_id, reason="時間が過ぎた")

    assert missed.status == "missed"
    assert missed.has_happened is False
    assert goals.completed_experiences() == []


def test_only_completion_records_something_that_happened(goals) -> None:
    plan = goals.plan(goal_id=None, description="話す")
    goals.start_plan(plan.plan_id)
    assert goals.completed_experiences() == []

    completed = goals.complete_plan(plan.plan_id, outcome="話した")

    assert completed.has_happened
    assert completed.completed_at is not None
    assert [item.plan_id for item in goals.completed_experiences()] == [plan.plan_id]


# --- habits -----------------------------------------------------------------


def test_repetition_without_its_cue_builds_nothing(habits) -> None:
    """Spec 15.3: automaticity comes from context × repetition."""
    for _ in range(10):
        habits.observe(
            name="greet_back", cue="user_says_hello", action="greet",
            cue_present=False, performed=True,
        )

    habit = habits.habit("greet_back", "user_says_hello")
    assert habit.repetitions == 10
    assert habit.automaticity == pytest.approx(0.0)
    assert habit.status == "forming"


def test_cue_paired_repetition_builds_automaticity(habits, agency_policy) -> None:
    for _ in range(15):
        habits.observe(
            name="greet_back", cue="user_says_hello", action="greet",
            cue_present=True, performed=True,
        )

    habit = habits.habit("greet_back", "user_says_hello")
    assert habit.automaticity > agency_policy.habits.established_at
    assert habit.status == "established"


def test_a_missed_day_does_not_reset_a_habit(habits, clock) -> None:
    """Spec 15.3: ``missed day で reset しない``."""
    for _ in range(15):
        habits.observe(
            name="greet_back", cue="user_says_hello", action="greet",
            cue_present=True, performed=True,
        )
    strong = habits.habit("greet_back", "user_says_hello").automaticity

    habits.apply_disuse(now=clock.now() + timedelta(days=1))
    after_a_day = habits.habit("greet_back", "user_says_hello").automaticity

    assert after_a_day < strong          # disuse costs a little
    assert after_a_day > strong - 0.05   # and only a little
    assert after_a_day > 0.5             # nowhere near a reset


def test_a_vanished_context_leaves_a_trace(habits, agency_policy) -> None:
    """Spec 15.3: ``context が消えると発現しなくても habit trace は残り得る``."""
    for _ in range(15):
        habits.observe(
            name="evening_check", cue="evening", action="look_outside",
            cue_present=True, performed=True,
        )
    habit = habits.habit("evening_check", "evening")

    habits.set_context_available(habit.habit_id, False)
    dormant = habits.habit("evening_check", "evening")

    assert dormant.status == "dormant"
    assert dormant.automaticity >= agency_policy.habits.trace_floor
    # It no longer fires…
    assert all(not outcome.fired for outcome in habits.cue_encountered("evening"))

    # …and comes straight back when the context returns.
    habits.set_context_available(habit.habit_id, True)
    assert any(outcome.fired for outcome in habits.cue_encountered("evening"))


def test_an_established_habit_fires_without_a_goal(habits) -> None:
    """Spec 15.3: the habitual route exists alongside the goal-directed one."""
    for _ in range(15):
        habits.observe(
            name="greet_back", cue="user_says_hello", action="greet",
            cue_present=True, performed=True,
        )

    fired = [outcome for outcome in habits.cue_encountered("user_says_hello") if outcome.fired]
    assert fired
    assert fired[0].reason == "automatic"


def test_a_weak_habit_does_not_fire(habits) -> None:
    habits.observe(
        name="new_thing", cue="morning", action="stretch", cue_present=True, performed=True
    )
    assert all(not outcome.fired for outcome in habits.cue_encountered("morning"))


# --- decision ---------------------------------------------------------------


def test_a_clearly_better_option_is_always_chosen(decisions) -> None:
    """Spec 15.4: ``大差候補を純 RNG にしない``."""
    candidates = [
        ActionCandidate(action="reply", route="goal_directed", expected_value=0.9),
        ActionCandidate(action="stay_quiet", route="reactive", expected_value=0.2),
    ]

    chosen = {decisions.choose(candidates, record=False).chosen.action for _ in range(50)}

    assert chosen == {"reply"}


def test_close_options_may_waver(decisions, agency_policy) -> None:
    """Spec 15.4: ``近い候補では小さい stochasticity を許す``."""
    candidates = [
        ActionCandidate(action="ask", route="goal_directed", expected_value=0.60),
        ActionCandidate(action="share", route="goal_directed", expected_value=0.58),
    ]

    chosen = {decisions.choose(candidates, record=False).chosen.action for _ in range(100)}

    assert chosen == {"ask", "share"}
    assert decisions.choose(candidates, record=False).was_close


def test_a_decision_is_recorded_with_what_was_considered(decisions, db) -> None:
    candidates = [
        ActionCandidate(action="reply", route="goal_directed", expected_value=0.8),
        ActionCandidate(action="wait", route="reactive", expected_value=0.3),
    ]
    decision = decisions.choose(candidates, run_id="run_1", event_id="evt_1")

    row = DecisionRepository(db).get(decision.decision_id)
    assert row["chosen_action"] == "reply"
    assert row["route"] == "goal_directed"
    assert "wait" in row["candidates_json"]


def test_outcomes_produce_prediction_error_and_learning(decisions) -> None:
    """Spec 15.4: outcome → prediction error → learning."""
    candidate = ActionCandidate(action="reply", route="goal_directed", expected_value=0.8)
    decision = decisions.choose([candidate], record=True)

    outcome = decisions.resolve(decision, achieved_value=0.3)

    assert outcome.prediction_error == pytest.approx(-0.5)
    revised = decisions.updated_expectation(0.8, outcome)
    assert revised < 0.8
    assert revised > 0.3  # learning is a step, not a jump


# --- epistemic actions ------------------------------------------------------


def test_not_knowing_something_does_not_trigger_a_search(epistemics) -> None:
    """Spec 17.1: ``未知 = 自動 Web Search にしてはならない``."""
    idle_curiosity = KnowledgeGap(
        topic="遠い国の祭り", unknown_part="すべて", relevance=0.1, curiosity=0.1
    )

    decision = epistemics.select(idle_curiosity, user_present=True)

    assert decision.searches is False
    assert decision.action in ("ignore", "defer")


def test_something_already_known_is_recalled_not_searched(epistemics) -> None:
    gap = KnowledgeGap(
        topic="前に聞いた話", relevance=0.9, curiosity=0.9, likely_in_memory=True
    )
    assert epistemics.select(gap, user_present=True).action == "recall"


def test_asking_the_person_beats_searching_when_they_are_here(epistemics) -> None:
    gap = KnowledgeGap(
        topic="相手の好きな食べ物", unknown_part="すべて", relevance=0.9, curiosity=0.8
    )

    present = epistemics.select(gap, user_present=True)
    absent = epistemics.select(gap, user_present=False)

    assert present.action == "ask_user"
    assert absent.action == "web_search"


def test_a_recent_search_failure_is_not_retried_immediately(epistemics, clock) -> None:
    """Spec 17.3: a failed search stays failed for a while."""
    gap = KnowledgeGap(topic="明日の天気", unknown_part="すべて", relevance=0.9, curiosity=0.9)

    decision = epistemics.select(
        gap,
        user_present=False,
        last_search_failure_at=clock.now() - timedelta(minutes=5),
    )

    assert decision.searches is False


def test_a_knowledge_gap_is_not_a_boolean(epistemics) -> None:
    """Spec 17.2: known part, unknown part and uncertainty are separate."""
    partial = KnowledgeGap(
        topic="相手の仕事",
        known_part="忙しいらしい",
        unknown_part="何の仕事か",
        uncertainty=0.3,
        relevance=0.4,
        curiosity=0.3,
    )

    decision = epistemics.select(partial, user_present=True)

    assert partial.is_total is False
    assert decision.action == "infer"  # something can be reasoned from what is known


def test_a_failed_attempt_leaves_an_honest_gap() -> None:
    gap = KnowledgeGap(topic="明日の天気", unknown_part="", relevance=0.9, curiosity=0.9)

    still_open = unresolved_gap(gap, attempted="web_search")

    assert still_open.unknown_part
    assert still_open.uncertainty > gap.uncertainty
    assert still_open.likely_in_memory is False
