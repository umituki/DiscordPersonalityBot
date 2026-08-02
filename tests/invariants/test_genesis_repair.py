"""INVARIANT: a repair keeps the real history (patch spec 23).

    FIRST BOOT後のreal Discord historyがあるためDB自動削除禁止。

Fixing the code does not fix a database that already booted from a broken
Genesis, and that database is also the only place the USER's real conversations
exist. So the repair is a shadow rebuild: production is read, never written,
until the owner confirms a switch that renames the old file aside rather than
removing it.

The database under test here is deliberately broken the way the real one was —
experiences recorded, nothing read, no memories, no knowledge — and then
repaired end to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.admin.legacy import LegacyHealthScanner
from app.admin.repair import CONFIRMATION, RepairRefused
from app.admin.shadow import rebuild_genesis, replay_real_history, shadow_config
from app.bootstrap import Application
from app.conversation.events import (
    USER_MESSAGE_RECEIVED,
    YUI_MESSAGE_SENT,
    UserMessageReceivedPayload,
    YuiMessageSentPayload,
)
from app.events.model import Event
from app.simulation.seed import SeedRequest
from app.storage.database import Database
from app.storage.repositories.events import EventRepository
from app.storage.repositories.health import HealthRepository
from app.storage.repositories.simulation import SimulationRepository
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

PERIOD_START = datetime(2003, 4, 1, 9, 0, tzinfo=timezone.utc)
PERIOD_END = datetime(2013, 4, 1, 9, 0, tzinfo=timezone.utc)
BOOT_AT = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


def conversation_events(count: int = 3) -> list[Event]:
    """A short real history: the USER said something and YUI answered."""
    events: list[Event] = []
    for index in range(count):
        moment = BOOT_AT + timedelta(hours=index)
        said = Event.create(
            event_type=USER_MESSAGE_RECEIVED,
            category="social",
            actor_type="user",
            actor_id=OWNER,
            target_type="yui",
            source_type="discord_message",
            source_id=str(1000 + index),
            origin="real_discord",
            priority="P0",
            occurred_at=moment,
            payload=UserMessageReceivedPayload(
                text=f"やっほー{index}",
                channel_id=CHANNEL,
                message_id=str(1000 + index),
                author_id=OWNER,
                is_direct_message=True,
            ),
        )
        answered = said.child(
            event_type=YUI_MESSAGE_SENT,
            category="social",
            actor_type="yui",
            source_type="discord_message",
            source_id=str(2000 + index),
            target_type="user",
            target_id=OWNER,
            priority="P1",
            occurred_at=moment + timedelta(seconds=30),
            payload=YuiMessageSentPayload(
                text=f"やっほー。{index}",
                channel_id=CHANNEL,
                message_id=str(2000 + index),
                in_reply_to_event_id=said.event_id,
            ),
        )
        events.extend((said, answered))
    return events


@pytest.fixture
def broken_production(temp_config, clock):
    """A database that booted from a Genesis the causal chain never ran in.

    Built the way the real one was: a simulation whose experiences were
    recorded and never read, then a FIRST BOOT forced past the audits, then
    real Discord history on top of it.
    """
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)

    # Record the experiences with nothing reading them: no interpreter, no
    # memory, no knowledge, no consolidation.
    application.processor._interpreter = None  # noqa: SLF001
    application.simulation._memory = None  # noqa: SLF001
    application.simulation._coverage = None  # noqa: SLF001
    application.simulation._consolidation = None  # noqa: SLF001

    async def build():
        seed = application.seed_builder.build(
            SeedRequest(answers={}, interests=("音楽", "読書"))
        )
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        result = await application.simulation.run(scaffold, max_blocks=30)
        # The old FIRST BOOT let this through; record it the same way.
        SimulationRepository(application.db).record_first_boot(
            result.run.simulation_id, now=BOOT_AT
        )
        for event in conversation_events():
            application.event_store.append(event)
        return result

    yield application, build

    application.db.close()


# --- 23.3 the diagnosis ------------------------------------------------------
async def test_a_broken_genesis_is_named_as_one(broken_production) -> None:
    application, build = broken_production
    await build()

    report = application.legacy.scan()

    assert report.genesis_health == "needs_rebuild"
    codes = {finding.code for finding in report.findings}
    assert "appraisal_never_ran" in codes
    assert "simulated_episodic_path_inactive" in codes
    assert "historical_knowledge_empty" in codes
    assert "periodic_consolidation_missing" in codes


async def test_the_diagnosis_sees_the_real_history_that_must_survive(
    broken_production,
) -> None:
    application, build = broken_production
    await build()

    report = application.legacy.scan()

    assert report.has_real_history
    assert report.real_events == 6
    assert report.rebuildable


def test_a_database_that_never_booted_has_nothing_to_repair(
    db, clock
) -> None:
    scanner = LegacyHealthScanner(
        health=HealthRepository(db), simulations=SimulationRepository(db)
    )
    report = scanner.scan()

    assert report.genesis_health == "no_genesis"
    assert report.sound


# --- 23.2 the dry run --------------------------------------------------------
async def test_the_dry_run_writes_nothing(broken_production) -> None:
    application, build = broken_production
    await build()
    before = application.event_store.count()

    plan = application.repair.dry_run()

    assert plan.needed
    assert plan.possible
    assert plan.history.count == 6
    assert plan.history.chronological
    assert application.event_store.count() == before
    assert not plan.shadow_path.exists()


async def test_the_dry_run_reads_the_original_seed_and_scaffold(
    broken_production,
) -> None:
    """23.4 step 2: a rebuild without them would be a different person."""
    application, build = broken_production
    await build()

    plan = application.repair.dry_run()

    assert plan.seed is not None
    assert plan.scaffold is not None
    assert plan.scaffold.period_start == PERIOD_START


async def test_a_repair_without_the_original_seed_is_blocked(
    broken_production,
) -> None:
    """23.4 step 2: rebuilding from a different seed would be a different person."""
    application, build = broken_production
    await build()
    plan = application.repair.dry_run()
    plan.seed = None

    blockers = application.repair._blockers(plan)  # noqa: SLF001

    assert any("temperament seed" in blocker for blocker in blockers)


async def test_a_previous_attempt_is_not_overwritten(broken_production) -> None:
    application, build = broken_production
    await build()
    first = application.repair.dry_run()
    first.shadow_path.parent.mkdir(parents=True, exist_ok=True)
    first.shadow_path.write_bytes(b"an earlier attempt")

    plan = application.repair.dry_run()

    assert plan.possible is False
    assert any("shadow database already exists" in blocker for blocker in plan.blockers)


# --- 23.4 the rebuild --------------------------------------------------------
async def test_a_rebuilt_genesis_keeps_every_real_event(
    broken_production, temp_config, clock
) -> None:
    application, build = broken_production
    await build()
    plan = application.repair.dry_run()

    result = await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock),
        replay=_replay(temp_config, clock),
        plan=plan,
    )

    assert result.refusal == "", result.refusal
    assert result.genesis is not None and result.genesis.booted, result.genesis.refusal
    assert result.replayed == plan.history.count
    assert result.verification.history_preserved
    assert result.verification.clean, result.verification.as_detail()


async def test_the_shadow_comes_back_healthy(
    broken_production, temp_config, clock
) -> None:
    """The point of the whole exercise: the new database is not the old one."""
    application, build = broken_production
    await build()
    plan = application.repair.dry_run()

    await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock),
        replay=_replay(temp_config, clock),
        plan=plan,
    )

    shadow = Database(plan.shadow_path)
    try:
        shadow.connect()
        report = LegacyHealthScanner(
            health=HealthRepository(shadow), simulations=SimulationRepository(shadow)
        ).scan()
        assert report.genesis_health == "healthy", report.render()
        assert HealthRepository(shadow).pipeline_metrics().appraised_simulated_events > 0
    finally:
        shadow.close()


async def test_what_yui_said_is_kept_exactly(
    broken_production, temp_config, clock
) -> None:
    """23.4 steps 8-9: no wording is regenerated and nothing is re-sent."""
    application, build = broken_production
    await build()
    plan = application.repair.dry_run()
    original = {
        event.event_id: event.payload.text
        for event in plan.history.events
        if event.event_type == YUI_MESSAGE_SENT
    }

    await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock),
        replay=_replay(temp_config, clock),
        plan=plan,
    )

    shadow = Database(plan.shadow_path)
    try:
        shadow.connect()
        events = EventRepository(shadow)
        for event_id, text in original.items():
            replayed = events.get(event_id)
            assert replayed is not None, "a real event was lost in the repair"
            assert replayed.payload.text == text
    finally:
        shadow.close()


async def test_the_real_history_comes_after_the_new_first_boot(
    broken_production, temp_config, clock
) -> None:
    """Spec 22.7's last line still holds in the repaired database."""
    application, build = broken_production
    await build()
    plan = application.repair.dry_run()

    await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock),
        replay=_replay(temp_config, clock),
        plan=plan,
    )

    shadow = Database(plan.shadow_path)
    try:
        shadow.connect()
        booted = SimulationRepository(shadow).booted_run()
        assert booted is not None and booted.first_boot_at is not None
        user_events = EventRepository(shadow).list_by_origins(("real_discord",))
        assert user_events
        assert all(
            event.occurred_at >= booted.simulated_to for event in user_events
        )
    finally:
        shadow.close()


async def test_a_rebuild_takes_a_verified_backup_first(
    broken_production, temp_config, clock
) -> None:
    """23.2: back up before touching anything."""
    application, build = broken_production
    await build()

    result = await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock), replay=_replay(temp_config, clock)
    )

    assert result.backup is not None
    assert result.backup.usable
    assert result.backup.path.exists()


async def test_production_is_untouched_by_a_rebuild(
    broken_production, temp_config, clock
) -> None:
    """23.1: nothing is deleted, and the old database still answers."""
    application, build = broken_production
    await build()
    before = application.event_store.count()

    await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock), replay=_replay(temp_config, clock)
    )

    assert application.event_store.count() == before
    assert application.legacy.scan().genesis_health == "needs_rebuild"


# --- 23.4 steps 12-14: the switch -------------------------------------------
async def test_switching_requires_the_owners_confirmation(
    broken_production, temp_config, clock
) -> None:
    application, build = broken_production
    await build()
    result = await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock), replay=_replay(temp_config, clock)
    )

    with pytest.raises(RepairRefused):
        application.repair.switch(result, confirmation="yes")
    with pytest.raises(RepairRefused):
        application.repair.switch(result, confirmation="")

    assert result.switched is False


async def test_a_shadow_that_did_not_verify_is_never_switched_to(
    broken_production, temp_config, clock
) -> None:
    application, build = broken_production
    await build()
    result = await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock), replay=_replay(temp_config, clock)
    )
    result.verification.history_preserved = False

    with pytest.raises(RepairRefused):
        application.repair.switch(result, confirmation=CONFIRMATION)


async def test_the_switch_keeps_the_old_database_as_the_rollback(
    broken_production, temp_config, clock
) -> None:
    """23.4 step 14: the old database is renamed, never removed."""
    application, build = broken_production
    await build()
    result = await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock), replay=_replay(temp_config, clock)
    )
    production = result.plan.production_path

    application.repair.switch(result, confirmation=CONFIRMATION)

    assert result.switched
    assert result.rollback_path is not None
    assert result.rollback_path.exists(), "the old database must still be there"
    assert production.exists()

    repaired = Database(production)
    try:
        repaired.connect()
        assert (
            LegacyHealthScanner(
                health=HealthRepository(repaired),
                simulations=SimulationRepository(repaired),
            )
            .scan()
            .genesis_health
            == "healthy"
        )
        events = EventRepository(repaired)
        conversation = events.list_by_types((USER_MESSAGE_RECEIVED, YUI_MESSAGE_SENT))
        assert len(conversation) == 6, "every real message must have survived"
        # 23.4 step 10: and their consequences were rebuilt on the repaired past,
        # which the broken database never had.
        assert events.count_by_origin(("real_discord",)) > 6
    finally:
        repaired.close()


async def test_a_switch_can_be_rolled_back(
    broken_production, temp_config, clock
) -> None:
    application, build = broken_production
    await build()
    result = await application.repair.rebuild(
        rebuild=_rebuild(temp_config, clock), replay=_replay(temp_config, clock)
    )
    application.repair.switch(result, confirmation=CONFIRMATION)

    kept = application.repair.rollback(result)

    assert result.switched is False
    assert kept.exists(), "the repaired database is kept, not thrown away"
    original = Database(result.plan.production_path)
    try:
        original.connect()
        report = LegacyHealthScanner(
            health=HealthRepository(original), simulations=SimulationRepository(original)
        ).scan()
        assert report.genesis_health == "needs_rebuild", "the original is back"
    finally:
        original.close()


def test_rolling_back_without_a_switch_is_refused(broken_production) -> None:
    application, _ = broken_production
    plan = application.repair.dry_run()
    from app.admin.repair import RepairResult

    with pytest.raises(RepairRefused):
        application.repair.rollback(RepairResult(plan=plan))


# --- the shadow config ------------------------------------------------------
def test_a_shadow_config_points_somewhere_else(temp_config, tmp_path) -> None:
    shadow = tmp_path / "shadow" / "yui.shadow.db"
    changed = shadow_config(temp_config, shadow)

    assert changed.database_path == shadow
    assert changed.database_path != temp_config.database_path
    # Everything else is the same run: same character, same policies.
    assert changed.character_dir == temp_config.character_dir
    assert changed.policies_dir == temp_config.policies_dir


# --- helpers ----------------------------------------------------------------
def _rebuild(config, clock):
    async def run(shadow_path: Path, seed, scaffold):
        application = None
        try:
            return await rebuild_genesis(
                config=config,
                shadow_path=shadow_path,
                seed=seed,
                scaffold=scaffold,
                clock=clock,
                max_blocks=30,
                on_built=use_offline_model,
            )
        finally:
            if application is not None:  # pragma: no cover - defensive
                application.db.close()

    return run


def _replay(config, clock):
    async def run(shadow_path: Path, events):
        report = await replay_real_history(
            config=config,
            shadow_path=shadow_path,
            events=events,
            clock=clock,
            on_built=use_offline_model,
        )
        return report.processed

    return run
