"""INVARIANT: her life runs on its own without the loop becoming the place
where every domain's judgement lives (rebuild spec 21, Phase 6).

RUNTIME-001 the runtime writes no psychological state
RUNTIME-002 a scheduler opportunity is never an action
RUNTIME-003 a USER message outranks anything the loop wanted to do
RUNTIME-004 start and stop belong to the application lifecycle

The loop is driven with an explicit ``tick()`` and a synthetic clock. Nothing
here sleeps for real: a test that waits five minutes to prove the runtime waits
five minutes has proved only that the test is slow.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.agency.models import ActionCandidate
from app.runtime.autonomous import MIN_SLEEP_SECONDS, AutonomousRuntime
from app.runtime.models import RuntimeTick
from app.runtime.sources import Registry, SchedulerSource
from app.storage.repositories.runtime import RuntimeTickRepository
from app.world.models import Opportunity

pytestmark = pytest.mark.invariant


class FixedSource:
    """A source that offers what it is told to, and knows its own next time."""

    def __init__(self, name: str = "fixture", kinds: tuple[str, ...] = ()) -> None:
        self.name = name
        self._kinds = kinds
        self.due = None
        self.collected = 0

    def collect(self, now):
        self.collected += 1
        return [
            Opportunity(kind=kind, urgency=0.5, created_at=now) for kind in self._kinds
        ]

    def next_due(self, now):
        return self.due


class BrokenSource:
    name = "broken"

    def collect(self, now):
        raise RuntimeError("the source is gone")

    def next_due(self, now):
        raise RuntimeError("the source is gone")


class RecordingDecisions:
    """Stands in for the Decision Engine, so the test can see what it was asked."""

    def __init__(self, *, choose_first: bool = True) -> None:
        self.asked: list[tuple[ActionCandidate, ...]] = []
        self._choose_first = choose_first

    def choose(self, candidates, **kwargs):
        self.asked.append(tuple(candidates))
        if not candidates or not self._choose_first:
            return None
        return type(
            "Decision",
            (),
            {"decision_id": "dec_test", "chosen": candidates[0], "was_close": False},
        )()


def _builder(action: str, value: float = 0.8):
    def build(opportunity, now):
        return ActionCandidate(
            action=action, route="goal_directed", expected_value=value
        )

    return build


@pytest.fixture
def registry() -> Registry:
    return Registry()


@pytest.fixture
def decisions() -> RecordingDecisions:
    return RecordingDecisions()


@pytest.fixture
def runtime(registry, decisions, clock) -> AutonomousRuntime:
    return AutonomousRuntime(registry, decisions=decisions, clock=clock)


# --- RUNTIME-001 -------------------------------------------------------------


def test_the_runtime_holds_nothing_that_can_write_state() -> None:
    """Structural, like the debug service. State moves through the processor,
    the proposals and the arbitrator — never from inside the loop."""
    import inspect

    source = inspect.getsource(AutonomousRuntime)
    for writer in (
        "StateCommitter",
        "StateArbitrator",
        "StateChangeProposal",
        "committer",
        "write_value",
        "record_change",
        "EmotionEngine",
        "MoodEngine",
        "NeedEngine",
        "RelationshipEngine",
    ):
        assert writer not in source, writer


def test_the_runtime_module_imports_no_psychology() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/runtime/autonomous.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in ("app.psychology", "app.social", "app.state", "app.consolidation"):
        assert not any(name.startswith(forbidden) for name in imported), forbidden


# --- RUNTIME-002 -------------------------------------------------------------


async def test_an_opportunity_is_not_an_action(runtime, registry, decisions, clock) -> None:
    """Spec 22. Collecting one is not deciding, and deciding is not doing."""
    registry.add_source(FixedSource(kinds=("activity_due",)))
    # No builder: nobody has claimed this kind yet.

    tick = await runtime.tick()

    assert tick.opportunities == 1
    assert tick.candidates == 0
    assert decisions.asked == []  # the decision engine was not even consulted
    assert tick.executed is False
    assert tick.outcome == "idle"


async def test_an_unclaimed_kind_is_recorded_not_swallowed(
    runtime, registry, clock
) -> None:
    """§4.5. "The scheduler has fired this for a week and nothing picked it up"
    has to be a row, not a silence."""
    registry.add_source(FixedSource(kinds=("activity_due", "habit_cue")))

    tick = await runtime.tick()

    assert tick.unclaimed_kinds == ("activity_due", "habit_cue")


async def test_the_decision_engine_chooses_not_the_loop(
    runtime, registry, decisions, clock
) -> None:
    registry.add_source(FixedSource(kinds=("activity_due", "habit_cue")))
    registry.add_builder("activity_due", _builder("start_activity", 0.4))
    registry.add_builder("habit_cue", _builder("do_habit", 0.9))
    ran: list[str] = []
    registry.add_handler("start_activity", lambda candidate, now: ran.append("activity") or True)
    registry.add_handler("do_habit", lambda candidate, now: ran.append("habit") or True)

    tick = await runtime.tick()

    # Both were offered to the decision engine; the loop expressed no preference.
    assert len(decisions.asked[0]) == 2
    assert tick.candidates == 2
    # Exactly one action ran, whichever was chosen.
    assert len(ran) == 1


async def test_a_builder_may_decline(runtime, registry, decisions, clock) -> None:
    """A habit cue encountered while she is asleep is not a candidate."""
    registry.add_source(FixedSource(kinds=("habit_cue",)))
    registry.add_builder("habit_cue", lambda opportunity, now: None)

    tick = await runtime.tick()

    assert tick.opportunities == 1
    assert tick.candidates == 0
    assert tick.unclaimed_kinds == ()  # declined is not unclaimed
    assert decisions.asked == []


async def test_a_decision_with_no_handler_is_visible(
    runtime, registry, decisions, clock
) -> None:
    """The §0 shape: something decided and nothing wired to carry it out."""
    registry.add_source(FixedSource(kinds=("activity_due",)))
    registry.add_builder("activity_due", _builder("start_activity"))
    # No handler registered.

    tick = await runtime.tick()

    assert tick.chosen_action == "start_activity"
    assert tick.executed is False
    assert tick.outcome == "unexecutable"


async def test_a_failing_action_does_not_end_the_life(
    runtime, registry, clock
) -> None:
    registry.add_source(FixedSource(kinds=("activity_due",)))
    registry.add_builder("activity_due", _builder("start_activity"))

    def explode(candidate, now):
        raise RuntimeError("the activity engine is gone")

    registry.add_handler("start_activity", explode)

    tick = await runtime.tick()

    assert tick.outcome == "failed"
    assert tick.executed is False
    # And the loop can still tick afterwards.
    assert (await runtime.tick()).outcome == "failed"


async def test_a_broken_source_does_not_stop_the_others(
    runtime, registry, clock
) -> None:
    registry.add_source(BrokenSource())
    registry.add_source(FixedSource(kinds=("habit_cue",)))

    tick = await runtime.tick()

    assert tick.opportunities == 1


# --- RUNTIME-003 -------------------------------------------------------------


async def test_a_user_turn_defers_background_action(
    runtime, registry, decisions, clock
) -> None:
    """She is talking to someone. Whatever the loop wanted can wait."""
    registry.add_source(FixedSource(kinds=("activity_due",)))
    registry.add_builder("activity_due", _builder("start_activity"))
    ran: list[str] = []
    registry.add_handler("start_activity", lambda candidate, now: ran.append("x") or True)

    with runtime.user_turn():
        tick = await runtime.tick()

    assert ran == []
    assert tick.outcome == "deferred"
    assert tick.deferred_reason == "user_turn_in_flight"
    # Deferred, not lost: what she nearly did is on the row.
    assert tick.candidates == 1
    assert decisions.asked == []


async def test_the_loop_acts_again_once_the_turn_is_over(
    runtime, registry, clock
) -> None:
    registry.add_source(FixedSource(kinds=("activity_due",)))
    registry.add_builder("activity_due", _builder("start_activity"))
    ran: list[str] = []
    registry.add_handler("start_activity", lambda candidate, now: ran.append("x") or True)

    with runtime.user_turn():
        await runtime.tick()
    assert ran == []

    tick = await runtime.tick()

    assert ran == ["x"]
    assert tick.outcome == "acted"


async def test_nested_user_turns_release_together(runtime) -> None:
    """Two overlapping messages must not clear the flag halfway."""
    with runtime.user_turn():
        with runtime.user_turn():
            assert runtime.busy_with_user
        assert runtime.busy_with_user
    assert not runtime.busy_with_user


async def test_a_failed_turn_still_releases_the_flag(runtime) -> None:
    with pytest.raises(RuntimeError):
        with runtime.user_turn():
            raise RuntimeError("the model is gone")
    assert not runtime.busy_with_user


# --- sleeping, not polling (spec 21.1) ---------------------------------------


async def test_the_next_wake_is_the_soonest_source(runtime, registry, clock) -> None:
    early, late = FixedSource(name="early"), FixedSource(name="late")
    early.due = clock.now() + timedelta(minutes=3)
    late.due = clock.now() + timedelta(minutes=30)
    registry.add_source(early)
    registry.add_source(late)

    tick = await runtime.tick()

    assert tick.next_wake_at == early.due


async def test_an_already_due_source_does_not_busy_wait(
    runtime, registry, clock
) -> None:
    """A source that keeps saying "now" must not turn the loop into a poll."""
    source = FixedSource()
    source.due = clock.now() - timedelta(hours=1)
    registry.add_source(source)

    tick = await runtime.tick()

    assert tick.next_wake_at is not None
    assert (tick.next_wake_at - clock.now()).total_seconds() >= MIN_SLEEP_SECONDS


async def test_no_source_can_say_so_it_uses_the_idle_interval(
    registry, decisions, clock
) -> None:
    runtime = AutonomousRuntime(
        registry, decisions=decisions, clock=clock, idle_seconds=600
    )
    registry.add_source(FixedSource())  # next_due is None

    tick = await runtime.tick()

    assert tick.next_wake_at == clock.now() + timedelta(seconds=600)


async def test_a_distant_due_time_is_capped_by_the_idle_interval(
    registry, decisions, clock
) -> None:
    """Spec 21.1 is next-due-time sleep, not sleeping through the week."""
    runtime = AutonomousRuntime(
        registry, decisions=decisions, clock=clock, idle_seconds=600
    )
    source = FixedSource()
    source.due = clock.now() + timedelta(days=3)
    registry.add_source(source)

    tick = await runtime.tick()

    assert tick.next_wake_at == clock.now() + timedelta(seconds=600)


# --- the quiet wake-ups are the point ----------------------------------------


async def test_an_empty_tick_is_still_recorded(db, registry, decisions, clock) -> None:
    """A runtime that wakes and does nothing looks exactly like a runtime that
    is not running, unless the empty passes leave a row (§4.5)."""
    ticks = RuntimeTickRepository(db)
    runtime = AutonomousRuntime(registry, decisions=decisions, ticks=ticks, clock=clock)

    for _ in range(5):
        await runtime.tick()

    assert ticks.count() == 5
    assert ticks.executed_count() == 0


async def test_the_audit_can_ask_what_fires_and_nobody_wants(
    db, registry, decisions, clock
) -> None:
    ticks = RuntimeTickRepository(db)
    runtime = AutonomousRuntime(registry, decisions=decisions, ticks=ticks, clock=clock)
    registry.add_source(FixedSource(kinds=("diary_due",)))

    await runtime.tick()

    assert ticks.unclaimed_kinds() == ["diary_due"]


async def test_a_broken_tick_recorder_does_not_cost_a_wake_up(
    registry, decisions, clock
) -> None:
    class Broken:
        def record(self, tick):
            raise RuntimeError("the disk is gone")

    runtime = AutonomousRuntime(
        registry, decisions=decisions, ticks=Broken(), clock=clock
    )

    tick = await runtime.tick()

    assert isinstance(tick, RuntimeTick)


async def test_a_tick_survives_a_restart(db, registry, decisions, clock) -> None:
    ticks = RuntimeTickRepository(db)
    runtime = AutonomousRuntime(registry, decisions=decisions, ticks=ticks, clock=clock)
    await runtime.tick()

    reopened = RuntimeTickRepository(db)

    assert reopened.count() == 1


# --- RUNTIME-004 -------------------------------------------------------------


async def test_start_and_stop_are_symmetric(runtime) -> None:
    await runtime.start()
    assert runtime.running

    await runtime.stop()

    assert not runtime.running


async def test_starting_twice_is_refused(runtime) -> None:
    await runtime.start()
    try:
        with pytest.raises(RuntimeError):
            await runtime.start()
    finally:
        await runtime.stop()


async def test_stopping_a_runtime_that_never_started_is_fine(runtime) -> None:
    await runtime.stop()  # must not raise


async def test_the_loop_ticks_when_it_is_woken(registry, decisions, clock) -> None:
    """Real asyncio, no real waiting: a long idle interval and an interrupt."""
    runtime = AutonomousRuntime(
        registry, decisions=decisions, clock=clock, idle_seconds=3600
    )
    source = FixedSource()
    registry.add_source(source)

    await runtime.start()
    try:
        await _settle()
        first = source.collected
        assert first >= 1  # the startup tick

        runtime.interrupt()
        await _settle()

        assert source.collected > first
    finally:
        await runtime.stop()


async def test_shutdown_drains_a_running_action(registry, decisions, clock) -> None:
    """RUNTIME-004. A half-finished action at exit is how a plan becomes a
    memory of something that never happened."""
    finished: list[str] = []
    registry.add_source(FixedSource(kinds=("activity_due",)))
    registry.add_builder("activity_due", _builder("slow_action"))

    async def slow(candidate, now):
        await asyncio.sleep(0)
        finished.append("done")
        return True

    registry.add_handler("slow_action", slow)
    runtime = AutonomousRuntime(
        registry, decisions=decisions, clock=clock, idle_seconds=3600
    )

    await runtime.start()
    await _settle()
    await runtime.stop()

    assert finished  # it ran to completion rather than being torn in half
    assert not runtime.running


async def _settle(passes: int = 6) -> None:
    """Let the event loop run pending callbacks without advancing real time."""
    for _ in range(passes):
        await asyncio.sleep(0)
