"""INVARIANT: operations that could destroy a life are hard to perform.

* Character Mode and Admin Mode are completely separate, and "forget that" in
  conversation is not a hard delete (spec 30).
* YUI has no admin authority (spec 30).
* A destructive operation walks impact → preview → confirmation → verified
  snapshot → mutation → cascade → validation → audit, or it does not run.
* A backup is taken with SQLite's own mechanism, verified, and restore-tested
  before it counts as one (spec 32).
* One local model, one queue: a reply outranks a diary entry, and background
  work yields to the USER at a safe point (spec 33).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.admin.control_plane import AdminControlPlane, AdminRefused, assert_not_yui
from app.admin.models import OPERATION_RISK
from app.bootstrap import Application
from app.reliability.resources import (
    PRIORITIES,
    ResourceManager,
    WorkCancelled,
    is_background,
    rank,
)
from app.storage.backup import BackupService, looks_cloud_synced
from app.storage.repositories.admin import AdminActionRepository, MemoryAdminRepository

pytestmark = pytest.mark.invariant


@pytest.fixture
def backups(db, tmp_path, clock) -> BackupService:
    return BackupService(db, backups_dir=tmp_path / "backups", clock=clock)


@pytest.fixture
def admin(db, backups, clock, tmp_path) -> AdminControlPlane:
    from app.llm.structured import StructuredGenerator
    from app.memory.engine import MemoryEngine
    from app.memory.policy import MemoryPolicy
    from app.memory.transcripts import StaticTranscriptSource, Transcript
    from app.llm.prompts import PromptRegistry
    from app.storage.repositories.memory import MemoryRepository
    from tests.unit.test_llm_structured import ScriptedClient

    root = Path(__file__).resolve().parents[2]
    prompts = PromptRegistry.load(root / "config" / "prompts")
    memory = MemoryEngine(
        repository=MemoryRepository(db),
        policy=MemoryPolicy.load(root / "config" / "policies" / "memory.yaml"),
        structured=StructuredGenerator(
            ScriptedClient([]), prompts=prompts, clock=clock, max_attempts=1
        ),
        prompts=prompts,
        transcripts=StaticTranscriptSource(Transcript(text="", turn_count=0, user_turn_count=0)),
        clock=clock,
    )
    return AdminControlPlane(
        actions=AdminActionRepository(db),
        memories=MemoryAdminRepository(db),
        memory=memory,
        backups=backups,
        clock=clock,
    )


def stored_memory(db, clock) -> str:
    """One encoded memory to operate on."""
    from app import ids
    from app.memory.models import EpisodicMemory
    from app.storage.repositories.memory import MemoryRepository

    memories = MemoryRepository(db)
    episode = memories.open_episode(
        conversation_id=None, origin="real_discord", started_at=clock.now()
    )
    memory_id = ids.new_id("mem")
    memories.insert_memory(
        EpisodicMemory(
            memory_id=memory_id,
            episode_id=episode.episode_id,
            origin="real_discord",
            summary="覚えていたこと",
            topics=("海",),
            importance=0.6,
            emotional_intensity=0.4,
            accessibility=0.8,
            content_confidence=0.7,
            source_confidence=0.7,
            temporal_confidence=0.7,
            novelty=0.5,
            prediction_error=0.1,
            occurred_at=clock.now(),
            created_at=clock.now(),
            updated_at=clock.now(),
            last_decayed_at=clock.now(),
            source_event_ids=(),
        )
    )
    return memory_id


# --- YUI has no admin authority (spec 30) -----------------------------------
def test_yui_may_not_perform_an_admin_operation(admin, db, clock) -> None:
    memory_id = stored_memory(db, clock)

    with pytest.raises(AdminRefused):
        admin.memory_operation("suppress", memory_id, actor="yui", dry_run=False)
    with pytest.raises(AdminRefused):
        assert_not_yui("YUI")
    assert admin.count() == 0


async def test_yui_has_no_admin_tool_registered(temp_config, clock) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        names = set(application.tools.registry.names())
        assert not {
            name for name in names if any(word in name for word in ("admin", "delete", "purge"))
        }
    finally:
        application.db.close()


# --- "forget that" is not a delete (spec 30) --------------------------------
def test_conversational_forgetting_only_suppresses(admin, db, clock) -> None:
    memory_id = stored_memory(db, clock)

    outcome = admin.from_conversation(memory_id, actor="owner")

    assert outcome.applied is True
    assert outcome.action.operation == "suppress"
    assert outcome.action.risk_class == "MUTATING"
    # Still on disk, simply unreachable by recall.
    assert MemoryAdminRepository(db).status_of(memory_id) == "suppressed"
    assert MemoryAdminRepository(db).exists(memory_id) == 1


def test_the_risk_classes_are_what_the_spec_says() -> None:
    assert OPERATION_RISK["suppress"] == "MUTATING"
    assert OPERATION_RISK["invalidate"] == "MUTATING"
    assert OPERATION_RISK["redact"] == "DESTRUCTIVE"
    assert OPERATION_RISK["hard_delete"] == "DESTRUCTIVE"
    assert OPERATION_RISK["inspect"] == "SAFE"


# --- the destructive procedure (spec 30) ------------------------------------
def test_a_destructive_operation_without_confirmation_is_refused(admin, db, clock) -> None:
    memory_id = stored_memory(db, clock)

    outcome = admin.memory_operation(
        "hard_delete", memory_id, actor="owner", dry_run=False, confirm=False
    )

    assert outcome.refused is True
    assert outcome.applied is False
    assert MemoryAdminRepository(db).exists(memory_id) == 1


def test_a_dry_run_changes_nothing_and_is_still_recorded(admin, db, clock) -> None:
    memory_id = stored_memory(db, clock)

    outcome = admin.memory_operation("hard_delete", memory_id, actor="owner", dry_run=True)

    assert outcome.applied is False
    assert outcome.action.stage == "audited"
    assert outcome.action.dry_run is True
    assert MemoryAdminRepository(db).exists(memory_id) == 1
    assert outcome.impact.reversible is False


def test_a_confirmed_destructive_operation_walks_the_whole_sequence(
    admin, db, clock
) -> None:
    memory_id = stored_memory(db, clock)

    outcome = admin.memory_operation(
        "hard_delete", memory_id, actor="owner", dry_run=False, confirm=True
    )

    assert outcome.applied is True
    assert outcome.action.stage == "audited"
    assert outcome.action.confirmed_by == "owner"
    # A verified pre-operation snapshot exists and is on disk.
    assert outcome.action.snapshot_path
    assert Path(outcome.action.snapshot_path).is_file()
    # And the mutation actually happened, with validation recording it.
    assert MemoryAdminRepository(db).exists(memory_id) == 0
    assert outcome.action.result["validation"]["ok"] is True


def test_every_admin_operation_leaves_an_audit_trail(admin, db, clock) -> None:
    memory_id = stored_memory(db, clock)
    admin.memory_operation("hard_delete", memory_id, actor="owner", dry_run=True)
    admin.memory_operation("suppress", memory_id, actor="owner", dry_run=False)

    trail = admin.for_target(memory_id)
    assert [action.operation for action in trail] == ["hard_delete", "suppress"]
    assert all(action.requested_at is not None for action in trail)


def test_the_objective_archive_survives_a_deleted_memory(admin, db, clock) -> None:
    """Spec 10.1: deleting what she remembers is not deleting what happened."""
    memory_id = stored_memory(db, clock)
    before = int(db.scalar("SELECT COUNT(*) FROM episodes") or 0)

    admin.memory_operation(
        "hard_delete", memory_id, actor="owner", dry_run=False, confirm=True
    )

    assert int(db.scalar("SELECT COUNT(*) FROM episodes") or 0) == before


# --- backups (spec 32) -------------------------------------------------------
def test_a_backup_is_verified_and_restore_tested(backups, db) -> None:
    record = backups.create(kind="manual", reason="test")

    assert record.integrity == "ok"
    assert record.restore_tested is True
    assert record.usable is True
    assert record.path.is_file()
    assert record.size_bytes > 0


def test_a_backup_is_a_real_database_not_a_file_copy(backups) -> None:
    """SQLite's backup API produces something that opens and answers."""
    record = backups.create()

    connection = sqlite3.connect(record.path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() is not None
    finally:
        connection.close()


def test_a_corrupted_backup_is_never_reported_as_usable(backups) -> None:
    record = backups.create()
    record.path.write_bytes(b"this is not a database")

    assert backups.verify(record.backup_id) is False
    # The stored record still says it was good when taken; verification is what
    # decides, and it is re-runnable precisely for this reason.
    assert backups.get(record.backup_id).path == record.path


def test_a_cloud_synced_database_path_is_recognised() -> None:
    """Spec 32: a live SQLite file must not sit in a sync folder."""
    assert looks_cloud_synced(Path("C:/Users/me/OneDrive/yui/data/yui.db")) is True
    assert looks_cloud_synced(Path("/home/me/Dropbox/yui.db")) is True
    assert looks_cloud_synced(Path("C:/yui/data/yui.db")) is False


def test_a_destructive_operation_needs_a_usable_snapshot(admin, db, clock, tmp_path) -> None:
    """Spec 30: no verified pre-operation snapshot, no mutation."""
    from dataclasses import replace

    from app.storage.backup import BackupError

    memory_id = stored_memory(db, clock)

    class RefusingBackups(BackupService):
        """A host whose disk is failing: the copy is written and unreadable."""

        def create(self, *, kind: str = "manual", reason: str = ""):
            record = super().create(kind=kind, reason=reason)
            record.path.write_bytes(b"corrupt")
            return replace(record, integrity="malformed", restore_tested=False)

    refusing = RefusingBackups(db, backups_dir=tmp_path / "b2", clock=clock)
    plane = AdminControlPlane(
        actions=AdminActionRepository(db),
        memories=MemoryAdminRepository(db),
        memory=admin._memory,  # noqa: SLF001 - the engine is not what is under test
        backups=refusing,
        clock=clock,
    )

    with pytest.raises(BackupError):
        plane.memory_operation(
            "hard_delete", memory_id, actor="owner", dry_run=False, confirm=True
        )
    assert MemoryAdminRepository(db).exists(memory_id) == 1


# --- resource priority (spec 33) --------------------------------------------
def test_the_priority_order_is_the_one_in_the_spec() -> None:
    assert PRIORITIES == ("P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7")
    assert rank("P0") < rank("P3") < rank("P7")
    assert is_background("P0") is False
    assert is_background("P1") is False
    assert is_background("P3") is True
    assert is_background("P7") is True


async def test_a_reply_goes_before_a_diary_entry() -> None:
    manager = ResourceManager(concurrency=1)
    order: list[str] = []

    held = await manager.acquire("P2", name="holder")

    async def work(priority: str, label: str) -> None:
        job = await manager.acquire(priority, name=label)
        order.append(label)
        manager.release(job)

    diary = asyncio.create_task(work("P6", "diary"))
    simulation = asyncio.create_task(work("P7", "simulation"))
    await asyncio.sleep(0)
    reply = asyncio.create_task(work("P0", "reply"))
    await asyncio.sleep(0)

    manager.release(held)
    await asyncio.gather(diary, simulation, reply)

    assert order[0] == "reply"


async def test_equal_priorities_keep_their_arrival_order() -> None:
    manager = ResourceManager(concurrency=1)
    order: list[str] = []
    held = await manager.acquire("P0", name="holder")

    async def work(label: str) -> None:
        job = await manager.acquire("P4", name=label)
        order.append(label)
        manager.release(job)

    tasks = [asyncio.create_task(work(f"job{index}")) for index in range(3)]
    await asyncio.sleep(0)
    manager.release(held)
    await asyncio.gather(*tasks)

    assert order == ["job0", "job1", "job2"]


def test_background_work_yields_to_the_user_at_a_safe_point() -> None:
    manager = ResourceManager()
    manager.user_input_arrived()

    # Foreground work is never asked to stop.
    manager.checkpoint("P0")
    manager.checkpoint("P1")

    with pytest.raises(WorkCancelled):
        manager.checkpoint("P6", name="diary")
    assert manager.stats.yielded == 1


def test_yielding_stops_when_the_user_has_been_answered() -> None:
    manager = ResourceManager()
    manager.user_input_arrived()
    assert manager.should_yield("P7") is True

    reply = asyncio.run(manager.acquire("P0", name="reply"))
    manager.release(reply)

    assert manager.yielding is False
    manager.checkpoint("P7")  # no longer asked to yield
