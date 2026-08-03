"""E2E: the loop the running application actually has (rebuild spec 21, Phase 6).

The unit tests drive an ``AutonomousRuntime`` assembled in the test. That proves
the loop works; it proves nothing about whether the *application* has one. §4.4
is explicit that instantiation is not completion, and §0's whole complaint is
about subsystems that were constructed at startup and never once driven.

So these drive ``application.runtime`` — the object bootstrap built, with the
scheduler bootstrap wired into it — and check the rows it wrote.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.admin.commands import REGISTRY
from app.admin.router import ADMIN_PREFIX
from app.bootstrap import Application
from app.conversation.service import ConversationResult
from app.interfaces.discord.dto import InboundMessage
from app.runtime.sources import SchedulerSource
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


@pytest.fixture
def owned_config(temp_config):
    return temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )


@pytest.fixture
def application(owned_config, clock):
    built = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


# --- the application has a loop, and it is wired to the scheduler ------------


def test_the_application_builds_a_runtime(application) -> None:
    assert application.runtime is not None
    names = {source.name for source in application.runtime._registry.sources}
    assert "scheduler" in names, "the scheduler is not wired as an opportunity source"


def test_the_scheduler_is_reachable_only_as_a_source(application) -> None:
    """RUNTIME-002, structurally: the loop holds the scheduler behind a source
    that can collect and predict, and cannot execute."""
    source = next(
        item
        for item in application.runtime._registry.sources
        if item.name == "scheduler"
    )
    assert isinstance(source, SchedulerSource)
    assert not hasattr(source, "execute")
    assert not hasattr(source, "run")


async def test_a_due_job_becomes_an_opportunity_and_stops_there(
    application, clock
) -> None:
    """The whole Phase 6 chain, minus the domains that have not landed:
    scheduler → opportunity → (no builder yet) → recorded, nothing executed."""
    application.scheduler.schedule_fixed(
        job_type="activity_due", due_at=clock.now() + timedelta(minutes=1)
    )
    clock.advance(minutes=2)

    tick = await application.runtime.tick()

    assert tick.opportunities == 1
    assert tick.opportunity_kinds == ("activity_due",)
    # Phase 7 registers the builder. Until then this is honestly unclaimed.
    assert tick.unclaimed_kinds == ("activity_due",)
    assert tick.executed is False
    assert application.runtime_ticks.count() == 1


async def test_the_audit_query_names_what_nobody_claimed(application, clock) -> None:
    application.scheduler.schedule_fixed(
        job_type="diary_due", due_at=clock.now() + timedelta(minutes=1)
    )
    clock.advance(minutes=2)

    await application.runtime.tick()

    assert application.runtime_ticks.unclaimed_kinds() == ["diary_due"]


# --- RUNTIME-001, on the wired application -----------------------------------


async def test_a_tick_changes_no_psychological_state(application, clock) -> None:
    """The loop asks questions. Nothing it does moves emotion, mood, needs or
    the relationship — those move through events and the arbitrator."""
    application.scheduler.schedule_fixed(
        job_type="habit_cue", due_at=clock.now() + timedelta(minutes=1)
    )
    clock.advance(minutes=2)
    before = {
        (value.domain, value.key): value.value
        for value in application.state.list_all()
    }

    await application.runtime.tick()

    after = {
        (value.domain, value.key): value.value
        for value in application.state.list_all()
    }
    assert after == before


async def test_a_tick_writes_no_event(application, clock) -> None:
    """Spec 22: an opportunity is not an event. Only a selected and executed
    one becomes one, and the handler owns that."""
    application.scheduler.schedule_fixed(
        job_type="habit_cue", due_at=clock.now() + timedelta(minutes=1)
    )
    clock.advance(minutes=2)
    before = application.event_store.count()

    await application.runtime.tick()

    assert application.event_store.count() == before


# --- RUNTIME-003, through the real conversation service ----------------------


async def test_a_real_turn_holds_the_loop_back(application, clock) -> None:
    """The conversation service must actually take the flag.

    A runtime that supports deferral and a service that never tells it is the
    same as having no rule at all, so this drives the real
    ``ConversationService.handle_inbound`` and asks, from inside the turn,
    whether the loop knows the USER is here. The reply itself is not the
    subject — the inner work is replaced so the assertion is about the flag and
    nothing else.
    """
    assert application.conversation is not None
    seen: list[bool] = []

    async def observe(message, **kwargs):
        seen.append(application.runtime.busy_with_user)
        return ConversationResult(accepted=False)

    application.conversation._handle_inbound = observe  # type: ignore[method-assign]
    inbound = InboundMessage(
        message_id="333",
        channel_id=CHANNEL,
        channel_type="direct_message",
        author_id=OWNER,
        text="やっほー",
        created_at=clock.now(),
    )

    await application.conversation.handle_inbound(inbound)

    assert seen == [True], "the conversation service never told the runtime"
    # And it let go afterwards.
    assert not application.runtime.busy_with_user


async def test_a_failing_turn_still_releases_the_loop(application, clock) -> None:
    """A model error must not leave her permanently unable to do anything."""
    assert application.conversation is not None

    async def explode(message, **kwargs):
        raise RuntimeError("the model is gone")

    application.conversation._handle_inbound = explode  # type: ignore[method-assign]
    inbound = InboundMessage(
        message_id="334",
        channel_id=CHANNEL,
        channel_type="direct_message",
        author_id=OWNER,
        text="やっほー",
        created_at=clock.now(),
    )

    with pytest.raises(RuntimeError):
        await application.conversation.handle_inbound(inbound)

    assert not application.runtime.busy_with_user


async def test_the_flag_is_clear_outside_a_turn(application) -> None:
    assert not application.runtime.busy_with_user


# --- RUNTIME-004, on the wired application -----------------------------------


async def test_the_runtime_is_off_unless_the_config_asks_for_it(
    application,
) -> None:
    """A test, a CLI command or a migration must not silently start a loop that
    acts while nobody is watching."""
    assert application.config.runtime.autonomous is False

    await application.start()
    try:
        assert not application.runtime.running
    finally:
        await application.stop()


async def test_the_lifecycle_starts_and_drains_the_loop(owned_config, clock) -> None:
    autonomous = owned_config.model_copy(
        update={"runtime": owned_config.runtime.model_copy(update={"autonomous": True})}
    )
    built = Application.build(autonomous, clock=clock, configure_logs=False)
    use_offline_model(built)

    await built.start()
    assert built.runtime.running

    await built.stop()

    assert not built.runtime.running


async def test_the_loop_starts_after_recovery_not_before(owned_config, clock) -> None:
    """Waking into a half-recovered world is how she acts on a job the restore
    was about to retire."""
    order: list[str] = []
    autonomous = owned_config.model_copy(
        update={"runtime": owned_config.runtime.model_copy(update={"autonomous": True})}
    )
    built = Application.build(autonomous, clock=clock, configure_logs=False)
    use_offline_model(built)

    original_restore = built.scheduler.restore
    original_start = built.runtime.start

    def restore(*args, **kwargs):
        order.append("restore")
        return original_restore(*args, **kwargs)

    async def start(*args, **kwargs):
        order.append("runtime")
        return await original_start(*args, **kwargs)

    built.scheduler.restore = restore  # type: ignore[method-assign]
    built.runtime.start = start  # type: ignore[method-assign]

    await built.start()
    try:
        assert order == ["restore", "runtime"]
    finally:
        built.runtime.start = original_start  # type: ignore[method-assign]
        await built.stop()


# --- the debug path (Phase 5's registry, extended without touching it) -------


def test_the_runtime_command_exists() -> None:
    assert "runtime" in REGISTRY
    assert REGISTRY["runtime"].is_read_only


async def test_the_runtime_command_shows_the_wake_ups(application, clock) -> None:
    application.scheduler.schedule_fixed(
        job_type="habit_cue", due_at=clock.now() + timedelta(minutes=1)
    )
    clock.advance(minutes=2)
    await application.runtime.tick()

    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} runtime", author_id=OWNER, channel_id=CHANNEL
    )

    assert not outcome.result.failed, outcome.result.error
    rows = [row for section in outcome.result.sections for row in section.rows]
    assert rows
    assert rows[0]["opportunity_kinds"] == "habit_cue"
