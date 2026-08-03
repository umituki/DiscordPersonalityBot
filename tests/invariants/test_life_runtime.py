"""INVARIANT: her life happens whether or not anyone speaks to her
(rebuild spec 24, 25 — Phase 7).

SLEEP-004 is the sentence that matters here:

    Sleep transition を Event が来た時だけ計算する状態から、due action として
    実際に駆動する。

The previous build computed the transition only when an event arrived, which
means she could fall asleep only if someone spoke to her first. Everything in
this file is about that difference — the loop fires it, and the rows are there
afterwards with nobody having said a word.

The rest is the separation the activity spec insists on: starting is not doing,
and only a completed record lets her say she did anything.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.bootstrap import Application
from app.runtime.life import (
    ACTIVITY_DUE,
    ACTIVITY_FINISH,
    FINISH_ACTIVITY,
    GO_TO_SLEEP,
    SLEEP_CANDIDATE,
    START_ACTIVITY,
    WAKE_CANDIDATE,
    ActivitySource,
    LifeCandidates,
    SleepSource,
)
from app.world import sleep as sleep_model
from app.world.events import (
    ACTIVITY_FINISHED,
    ACTIVITY_STARTED,
    WENT_TO_SLEEP,
    WOKE_UP,
)
from app.world.policy import SleepPolicy
from tests.support import use_offline_model

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
    built = Application.build(owned, clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


def _make_exhausted(application, clock) -> None:
    """A long day: full sleep pressure, and the small hours.

    Both matter. Pressure alone at noon is not enough to be exhausted, and the
    hour alone is not either — which is the two-process model behaving, not a
    fixture being fussy.
    """
    clock.set(clock.now().replace(hour=3, minute=0))
    existing = application.state.get("world", "sleep_pressure")
    application.state.write_value(
        domain="world",
        key="sleep_pressure",
        value=1.0,
        confidence=None,
        now=clock.now(),
        run_id=None,
        event_id=None,
        expected_version=None if existing is None else existing.version,
    )


# --- SLEEP-001: staying up is not indefinitely available ---------------------


def test_resistance_runs_out(clock) -> None:
    """``LLM の気分だけで「寝ない」を無限継続させない``.

    The two-process model already lets resistance decay as sleepiness climbs.
    This pins the ceiling: past it, the question is not being asked any more.
    """
    policy = SleepPolicy()
    exhausted = sleep_model.signals(
        now=clock.now().replace(hour=3, minute=0),
        sleep_pressure=1.0,
        policy=policy,
        has_active_goal=True,
        in_conversation=True,
    )

    assert sleep_model.resistance_is_exhausted(exhausted, policy)
    # And with every reason to stay up, she still sleeps.
    assert sleep_model.should_sleep(exhausted, policy)


def test_an_ordinary_evening_is_not_forced(clock) -> None:
    policy = SleepPolicy()
    ordinary = sleep_model.signals(
        now=clock.now().replace(hour=12, minute=0), sleep_pressure=0.5, policy=policy
    )

    assert not sleep_model.resistance_is_exhausted(ordinary, policy)


def test_an_exhausted_candidate_cannot_be_outbid(clock) -> None:
    """The ceiling has to survive the Decision Engine, not only the model."""
    from app.world.models import Opportunity

    candidates = LifeCandidates(policy=SleepPolicy())
    forced = candidates.sleep(
        Opportunity(kind=SLEEP_CANDIDATE, detail="exhausted", created_at=clock.now()),
        clock.now(),
    )
    merely_sleepy = candidates.sleep(
        Opportunity(kind=SLEEP_CANDIDATE, detail="sleepy", created_at=clock.now()),
        clock.now(),
    )

    assert forced.expected_value == 1.0
    assert merely_sleepy.expected_value < 1.0


# --- SLEEP-002: a nap is not a night -----------------------------------------


def test_a_doze_outside_the_window_is_a_nap(clock) -> None:
    policy = SleepPolicy()
    noon = clock.now().replace(hour=12, minute=0)
    light = sleep_model.signals(now=noon, sleep_pressure=0.3, policy=policy)

    assert sleep_model.classify_sleep(noon, light, policy) == "nap"


def test_real_pressure_outside_the_window_is_still_a_night(clock) -> None:
    """Falling asleep at two in the morning after a long day is not a nap."""
    policy = SleepPolicy()
    small_hours = clock.now().replace(hour=2, minute=0)
    heavy = sleep_model.signals(now=small_hours, sleep_pressure=0.95, policy=policy)

    assert sleep_model.classify_sleep(small_hours, heavy, policy) == "main"


async def test_the_kind_is_recorded_when_she_lies_down(application, clock) -> None:
    """Not inferred from the duration afterwards: a main sleep cut short by a
    message is a main sleep that went wrong, not a nap."""
    _make_exhausted(application, clock)
    source = SleepSource(
        application.world, application.state, policy=application.world_policy.sleep,
        clock=clock,
    )

    application.world.fall_asleep(signals=source.signals(clock.now()), kind="nap")

    episode = application.world.current_sleep()
    assert episode is not None
    assert episode.kind == "nap"
    assert episode.is_nap
    # A nap plans a short wake time, not a night's.
    assert episode.planned_wake_at is not None
    hours = (episode.planned_wake_at - episode.started_at).total_seconds() / 3600
    assert hours == pytest.approx(application.world_policy.sleep.nap_hours)


# --- SLEEP-003/004: the runtime fires the transition -------------------------


async def test_she_falls_asleep_with_nobody_speaking_to_her(
    application, clock
) -> None:
    """The whole phase in one test. No message, no event arriving — the loop
    notices, decides, and the transition happens."""
    _make_exhausted(application, clock)
    before = application.event_store.count()

    tick = await application.runtime.tick()

    # Two moments were available — she is sleepy, and she has nothing on. The
    # Decision Engine picked between them; the loop expressed no preference.
    assert set(tick.opportunity_kinds) == {SLEEP_CANDIDATE, ACTIVITY_DUE}
    assert tick.candidates == 2
    assert tick.chosen_action == GO_TO_SLEEP
    assert tick.executed is True
    assert tick.outcome == "acted"

    # And the world actually moved.
    episode = application.world.current_sleep()
    assert episode is not None
    assert application.event_store.count() > before
    types = {event.event_type for event in application.event_store.recent(limit=10)}
    assert WENT_TO_SLEEP in types


async def test_she_wakes_up_on_her_own(application, clock) -> None:
    _make_exhausted(application, clock)
    await application.runtime.tick()
    assert application.world.current_sleep() is not None

    clock.advance(hours=9)
    tick = await application.runtime.tick()

    assert tick.opportunity_kinds == (WAKE_CANDIDATE,)
    assert tick.executed is True
    assert application.world.current_sleep() is None
    types = {event.event_type for event in application.event_store.recent(limit=10)}
    assert WOKE_UP in types


async def test_the_sleep_event_is_hers_and_not_a_reply(application, clock) -> None:
    """A root event, not a child of anything. Nothing prompted this."""
    _make_exhausted(application, clock)

    await application.runtime.tick()

    event = next(
        item
        for item in application.event_store.recent(limit=10)
        if item.event_type == WENT_TO_SLEEP
    )
    assert event.actor_type == "yui"
    assert event.origin == "virtual_life"
    assert event.parent_event_id is None


async def test_nothing_happens_when_she_is_not_sleepy(application, clock) -> None:
    """Most moments are not moments to do anything, and that is not a failure."""
    clock.set(clock.now().replace(hour=12, minute=0))
    application.state.write_value(
        domain="world",
        key="sleep_pressure",
        value=0.05,
        confidence=None,
        now=clock.now(),
        run_id=None,
        event_id=None,
        expected_version=None,
    )
    source = SleepSource(
        application.world, application.state, policy=application.world_policy.sleep,
        clock=clock,
    )

    assert list(source.collect(clock.now())) == []


async def test_the_next_wake_is_the_planned_one_while_asleep(
    application, clock
) -> None:
    """21.1: she does not wake every fifteen minutes to check whether she is
    still asleep."""
    _make_exhausted(application, clock)
    await application.runtime.tick()

    episode = application.world.current_sleep()
    source = SleepSource(
        application.world, application.state, policy=application.world_policy.sleep,
        clock=clock,
    )

    assert source.next_due(clock.now()) == episode.planned_wake_at


# --- ACT-001/002: starting is not doing --------------------------------------


async def test_starting_an_activity_is_not_an_experience(application, clock) -> None:
    """ACT-001. The row says ongoing, and nothing says she did anything."""
    tick = await application.runtime.tick()

    assert tick.chosen_action == START_ACTIVITY
    assert tick.executed is True

    current = application.world.current_activity()
    assert current is not None
    assert current.status == "ongoing"
    assert current.ended_at is None
    # ACT-002: nothing completed, so there is nothing she can claim.
    assert application.world.completed_activities() == []


async def test_only_finishing_makes_it_something_she_did(application, clock) -> None:
    """ACT-002: 今日は○○した と言えるのは completed record がある場合だけ."""
    await application.runtime.tick()
    started = application.world.current_activity()
    assert started is not None
    assert started.expected_end_at is not None

    clock.advance(minutes=90)
    tick = await application.runtime.tick()

    assert tick.opportunity_kinds == (ACTIVITY_FINISH,)
    assert tick.chosen_action == FINISH_ACTIVITY
    completed = application.world.completed_activities()
    assert [item.activity_id for item in completed] == [started.activity_id]
    assert completed[0].outcome


async def test_both_events_exist_and_are_different(application, clock) -> None:
    await application.runtime.tick()
    clock.advance(minutes=90)
    await application.runtime.tick()

    types = [event.event_type for event in application.event_store.recent(limit=20)]
    assert ACTIVITY_STARTED in types
    assert ACTIVITY_FINISHED in types


async def test_an_activity_is_not_due_before_its_time(application, clock) -> None:
    await application.runtime.tick()
    clock.advance(minutes=5)

    source = ActivitySource(application.world, clock=clock)

    assert list(source.collect(clock.now())) == []


async def test_the_completed_fact_belongs_to_the_world_service(application, clock) -> None:
    """ACT-003. The outcome text may be proposed; the completion is not."""
    import inspect

    from app.runtime import life

    source = inspect.getsource(life.LifeActions)
    # No repository write from the handler — it goes through the service.
    for direct in ("_activities.finish", "_activities.start", "INSERT", "UPDATE"):
        assert direct not in source, direct
    assert "self._world.finish_activity" in source


async def test_nothing_starts_while_she_is_asleep(application, clock) -> None:
    _make_exhausted(application, clock)
    await application.runtime.tick()
    assert application.world.current_sleep() is not None

    source = ActivitySource(application.world, clock=clock)

    assert list(source.collect(clock.now())) == []


# --- the loop is still the loop ----------------------------------------------


async def test_a_user_turn_still_defers_her_own_life(application, clock) -> None:
    """RUNTIME-003 has to keep holding now that there is something to defer."""
    _make_exhausted(application, clock)

    with application.runtime.user_turn():
        tick = await application.runtime.tick()

    assert tick.outcome == "deferred"
    assert application.world.current_sleep() is None


async def test_no_kind_is_unclaimed_any_more(application, clock) -> None:
    """§4.5 in the other direction: Phase 7 claims what it schedules."""
    _make_exhausted(application, clock)

    await application.runtime.tick()

    assert application.runtime_ticks.unclaimed_kinds() == []


def test_every_kind_this_phase_raises_has_a_builder(application) -> None:
    registry = application.runtime._registry
    for kind in (SLEEP_CANDIDATE, WAKE_CANDIDATE, ACTIVITY_DUE, ACTIVITY_FINISH):
        assert registry.builder_for(kind) is not None, kind


def test_every_action_this_phase_chooses_has_a_handler(application) -> None:
    registry = application.runtime._registry
    for action in (GO_TO_SLEEP, "wake_up", START_ACTIVITY, FINISH_ACTIVITY):
        assert registry.handler_for(action) is not None, action
