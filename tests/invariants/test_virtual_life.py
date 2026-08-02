"""INVARIANT: virtual life stays honest about time and about contact.

* Routine, plan, current activity and completed event are four different things
  (spec 18.2, 2.15).
* Sleep follows combined signals, not a clock rule, and offline recovery is
  compressed into meaningful transitions (spec 18.3).
* The scheduler produces opportunities and never acts (spec 19).
* Silence must widen the interval between messages, never narrow it (spec 19).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.jobs.proactive import ProactiveEngine
from app.jobs.scheduler import Scheduler
from app.storage.repositories.world import (
    ActivityRepository,
    JobRepository,
    ProactiveRepository,
    SleepRepository,
    WorldHistoryRepository,
)
from app.world import sleep as sleep_model
from app.world.models import Opportunity
from app.world.policy import WorldPolicy
from app.world.service import WorldService
from tests.unit.test_psychology import view_with

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def world_policy() -> WorldPolicy:
    return WorldPolicy.load(REPO_ROOT / "config" / "policies" / "world.yaml")


@pytest.fixture
def world(db, world_policy, clock) -> WorldService:
    return WorldService(
        activities=ActivityRepository(db),
        sleeps=SleepRepository(db),
        history=WorldHistoryRepository(db),
        policy=world_policy,
        clock=clock,
    )


@pytest.fixture
def scheduler(db, world_policy, clock) -> Scheduler:
    return Scheduler(JobRepository(db), world_policy.scheduler, clock=clock)


@pytest.fixture
def proactive(db, world_policy, clock) -> ProactiveEngine:
    return ProactiveEngine(ProactiveRepository(db), world_policy.proactive, clock=clock)


@pytest.fixture
def always_available(db, world_policy, clock) -> ProactiveEngine:
    """Proactive engine with quiet hours disabled.

    Availability has its own test; these cases are about the interval rule and
    should not depend on where the fixed clock happens to land.
    """
    policy = world_policy.proactive.model_copy(update={"quiet_hours_utc": (0, 0)})
    return ProactiveEngine(ProactiveRepository(db), policy, clock=clock)


def opportunity(clock, *, kind: str = "proactive_contact", urgency: float = 0.5):
    return Opportunity(kind=kind, urgency=urgency, created_at=clock.now())


# --- activities (spec 18.2) -------------------------------------------------


def test_an_ongoing_activity_is_not_an_experience(world) -> None:
    activity, _ = world.start_activity(name="音楽を聴く", kind="leisure")

    assert activity.is_ongoing
    assert activity.has_happened is False
    assert world.completed_activities() == []
    assert world.current_activity().activity_id == activity.activity_id


def test_only_finishing_makes_it_something_that_happened(world, clock) -> None:
    activity, _ = world.start_activity(name="音楽を聴く", kind="leisure")
    clock.advance(seconds=1800)

    finished, _ = world.finish_activity(activity.activity_id, outcome="落ち着いた")

    assert finished.has_happened
    assert finished.ended_at is not None
    assert [item.activity_id for item in world.completed_activities()] == [
        activity.activity_id
    ]


def test_an_abandoned_activity_never_becomes_completed(world, clock) -> None:
    first, _ = world.start_activity(name="読書", kind="leisure")
    clock.advance(seconds=600)
    world.start_activity(name="音楽を聴く", kind="leisure")  # supersedes the first

    assert world.completed_activities() == []
    assert world.current_activity().name == "音楽を聴く"


def test_activity_events_exist_only_for_real_transitions(world, make_event, clock) -> None:
    parent = make_event()
    activity, started = world.start_activity(
        name="散歩", kind="leisure", parent_event=parent
    )
    clock.advance(seconds=900)
    _, finished = world.finish_activity(
        activity.activity_id, outcome="のんびりした", parent_event=parent
    )

    assert started.event_type == "ACTIVITY_STARTED"
    assert finished.event_type == "ACTIVITY_FINISHED"
    assert finished.payload.duration_minutes == pytest.approx(15.0, abs=0.1)
    assert started.category == "world"


# --- sleep (spec 18.3) ------------------------------------------------------


def test_sleep_pressure_builds_while_awake_and_discharges_asleep(world_policy) -> None:
    awake = sleep_model.accumulated_pressure(
        current=0.2, awake_hours=8.0, policy=world_policy.sleep
    )
    assert awake > 0.2

    rested = sleep_model.recovered_pressure(
        current=awake, slept_hours=7.0, policy=world_policy.sleep
    )
    assert rested < awake


def test_sleep_is_not_a_clock_rule(world_policy, clock) -> None:
    """Spec 18.3: the decision comes from combined signals."""
    at_3am = clock.now().replace(hour=3, minute=0)

    rested = sleep_model.signals(
        now=at_3am, sleep_pressure=0.05, policy=world_policy.sleep
    )
    exhausted = sleep_model.signals(
        now=at_3am, sleep_pressure=0.95, policy=world_policy.sleep
    )

    assert sleep_model.should_sleep(rested, world_policy.sleep) is False
    assert sleep_model.should_sleep(exhausted, world_policy.sleep) is True


def test_a_live_conversation_delays_sleep_but_cannot_hold_it_off_forever(
    world_policy, clock
) -> None:
    at_3am = clock.now().replace(hour=3, minute=0)

    tired_alone = sleep_model.signals(
        now=at_3am, sleep_pressure=0.7, policy=world_policy.sleep
    )
    tired_talking = sleep_model.signals(
        now=at_3am, sleep_pressure=0.7, policy=world_policy.sleep, in_conversation=True
    )
    exhausted_talking = sleep_model.signals(
        now=at_3am, sleep_pressure=1.0, policy=world_policy.sleep, in_conversation=True
    )

    assert tired_talking.net_sleepiness < tired_alone.net_sleepiness
    assert tired_talking.resistance <= world_policy.sleep.max_resistance
    assert sleep_model.should_sleep(exhausted_talking, world_policy.sleep) is True


def test_sleeping_and_waking_are_recorded_as_episodes(world, clock) -> None:
    signals = world.consider_sleep(sleep_pressure=0.9)
    episode, _ = world.fall_asleep(signals=signals)
    assert episode.is_asleep

    clock.advance(seconds=7 * 3600)
    finished, slept = world.wake_up()

    assert finished.is_asleep is False
    assert slept == pytest.approx(7.0, abs=0.01)
    assert finished.quality == pytest.approx(1.0, abs=0.01)


def test_waking_early_records_a_lower_quality(world, clock) -> None:
    signals = world.consider_sleep(sleep_pressure=0.9)
    world.fall_asleep(signals=signals)
    clock.advance(seconds=2 * 3600)

    finished, slept = world.wake_up(interrupted=True)

    assert finished.interrupted is True
    assert finished.quality < 0.5
    assert slept == pytest.approx(2.0, abs=0.01)


def test_offline_catch_up_is_compressed(world, world_policy, clock) -> None:
    """Spec 18.3: no minute-by-minute replay of an absence."""
    since = clock.now() - timedelta(days=3)

    result = world.catch_up(since=since, sleep_pressure=0.3)

    assert result.elapsed_hours == pytest.approx(72.0, abs=0.1)
    assert result.compressed
    assert len(result.transitions) <= world_policy.world.catch_up_max_transitions
    # Three days should produce a handful of sleep/wake transitions, not 4320.
    assert len(result.transitions) < 20


def test_catch_up_over_a_short_gap_does_nothing_dramatic(world, clock) -> None:
    result = world.catch_up(since=clock.now() - timedelta(minutes=10), sleep_pressure=0.2)
    assert result.elapsed_hours < 1.0
    assert len(result.transitions) <= 1


# --- scheduler (spec 19) ----------------------------------------------------


def test_the_scheduler_produces_opportunities_not_actions(scheduler, clock) -> None:
    scheduler.schedule_fixed(
        job_type="evening_reflection", due_at=clock.now() - timedelta(minutes=1)
    )

    produced = scheduler.tick()

    assert len(produced) == 1
    assert isinstance(produced[0], Opportunity)
    # An opportunity carries no instruction to do anything.
    assert not hasattr(produced[0], "execute")


def test_a_job_that_is_not_due_yet_produces_nothing(scheduler, clock) -> None:
    scheduler.schedule_fixed(
        job_type="later", due_at=clock.now() + timedelta(hours=2)
    )
    assert scheduler.tick() == []


def test_a_closed_window_does_not_fire(scheduler, clock) -> None:
    scheduler.schedule_window(
        job_type="morning_greeting",
        opens_at=clock.now() - timedelta(hours=3),
        closes_at=clock.now() - timedelta(hours=1),
    )

    assert scheduler.tick() == []
    assert scheduler.pending() == []  # it was retired, not left dangling


def test_a_condition_job_waits_for_its_condition(scheduler, clock) -> None:
    ready = False
    scheduler.register_condition("when_ready", lambda _now: ready)
    scheduler.schedule_condition(job_type="when_ready")

    assert scheduler.tick() == []

    ready = True
    assert [item.kind for item in scheduler.tick()] == ["when_ready"]


def test_restart_retires_jobs_whose_moment_passed(scheduler, clock) -> None:
    scheduler.schedule_window(
        job_type="missed_while_offline",
        opens_at=clock.now() - timedelta(days=2),
        closes_at=clock.now() - timedelta(days=1),
    )

    retired = scheduler.restore()

    assert retired == 1
    assert scheduler.pending() == []


# --- proactive contact (spec 19) --------------------------------------------


def test_silence_widens_the_interval_instead_of_narrowing_it(
    always_available, world_policy, clock
) -> None:
    """Spec 19: ``USER が反応しないほど送信頻度が増える`` is forbidden."""
    base = world_policy.proactive.min_hours_between_contacts
    lonely = view_with(
        needs__loneliness=0.9, needs__connection_desire=0.9,
        relationship__emotional_closeness=0.7,
    )

    first = always_available.assess(opportunity(clock), lonely)
    assert first.allowed
    always_available.record_sent(opportunity(clock))

    # Immediately after, nothing may be sent.
    assert always_available.assess(opportunity(clock), lonely).allowed is False

    # Even after the *base* interval it is still too soon, because one message
    # went unanswered and the interval widened.
    clock.advance(seconds=int(base * 3600) + 60)
    second = always_available.assess(opportunity(clock), lonely)
    assert second.allowed is False
    assert second.required_wait_hours > base

    # Only after the widened interval may another message go out.
    clock.advance(seconds=int(second.required_wait_hours * 3600))
    assert always_available.assess(opportunity(clock), lonely).allowed


def test_contact_stops_entirely_after_too_many_unanswered(
    proactive, world_policy, clock
) -> None:
    lonely = view_with(needs__loneliness=1.0, needs__connection_desire=1.0)

    for _ in range(world_policy.proactive.max_unanswered):
        proactive.record_sent(opportunity(clock))
        clock.advance(seconds=100 * 3600)

    blocked = proactive.assess(opportunity(clock), lonely)

    assert blocked.allowed is False
    assert blocked.unanswered >= world_policy.proactive.max_unanswered


def test_a_reply_clears_the_backlog(always_available, world_policy, clock) -> None:
    lonely = view_with(needs__loneliness=1.0, needs__connection_desire=1.0)
    for _ in range(world_policy.proactive.max_unanswered):
        always_available.record_sent(opportunity(clock))
    assert always_available.assess(opportunity(clock), lonely).allowed is False

    always_available.note_user_replied()
    clock.advance(seconds=int(world_policy.proactive.min_hours_between_contacts * 3600) + 60)

    assert always_available.unanswered() == 0
    assert always_available.assess(opportunity(clock), lonely).allowed


def test_wanting_to_be_alone_suppresses_contact(proactive, clock) -> None:
    """Solitude desire is a real counterweight, not decoration (spec 15.1)."""
    withdrawn = view_with(
        needs__loneliness=0.6, needs__connection_desire=0.6, needs__solitude_desire=0.9
    )

    assessment = proactive.assess(opportunity(clock), withdrawn)

    assert assessment.allowed is False
    assert "pressing" in assessment.reason


def test_the_engine_only_proposes_a_candidate(proactive, clock) -> None:
    """Spec 19: the Decision Engine still has to choose it."""
    lonely = view_with(needs__loneliness=0.9, needs__connection_desire=0.9)

    assessment = proactive.assess(opportunity(clock), lonely)
    candidate = assessment.candidate

    assert candidate is not None
    assert candidate.action == "proactive_contact"
    assert 0.0 <= candidate.expected_value <= 1.0
