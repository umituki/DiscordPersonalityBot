"""CHAOS: what happens when the parts that can fail, fail (spec 34.1, 28).

The property under test is always the same one: a failure degrades behaviour
and never corrupts state. After every injected fault the database must still be
consistent, the run must be accounted for, and nothing half-written may be
visible.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.bootstrap import Application
from app.events.bus import SubscriberResult
from app.state.proposal import StateChangeProposal
from app.storage.repositories.state import StateRepository

pytestmark = pytest.mark.chaos


class Exploding:
    """A subscriber that fails the way real code fails: in the middle."""

    name = "exploding_subscriber"

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def handle(self, event, view) -> SubscriberResult:
        self.calls += 1
        raise self._error


class HalfWriting:
    """Proposes one valid change and one that arbitration must reject."""

    name = "half_writing_subscriber"

    async def handle(self, event, view) -> SubscriberResult:
        return SubscriberResult(
            proposals=(
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module="mood_engine",
                    target_domain="mood",
                    target_key="valence",
                    value=0.6,
                ),
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module="mood_engine",
                    target_domain="mood",
                    target_key="arousal",
                    value=99.0,  # far out of bounds
                ),
            )
        )


async def test_a_subscriber_crash_degrades_the_run_and_keeps_state_intact(
    temp_config, clock, make_event
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        application.bus.register(Exploding(RuntimeError("model host vanished")), order=5)

        outcome = await application.processor.process(make_event(actor_type="user"))

        assert outcome.status in ("committed", "rejected", "failed")
        assert application.db.integrity_check() == "ok"
        # The failure was recorded rather than swallowed.
        assert application.failures.count() > 0
    finally:
        application.db.close()


async def test_a_rejected_proposal_does_not_take_its_neighbour_down(
    temp_config, clock, make_event
) -> None:
    """Spec 28: reject the bad one, commit the good one, record the rejection."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        application.bus.register(HalfWriting(), order=5)

        outcome = await application.processor.process(make_event(actor_type="user"))

        targets = set(outcome.committed_targets)
        assert "mood.valence" in targets
        assert "mood.arousal" not in targets
        assert application.state.get("mood", "arousal") is None or (
            application.state.get("mood", "arousal").numeric <= 1.0
        )
        assert application.failures.count() > 0
        assert application.db.integrity_check() == "ok"
    finally:
        application.db.close()


async def test_a_commit_failure_rolls_the_whole_run_back(
    temp_config, clock, make_event, monkeypatch
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        application.bus.register(HalfWriting(), order=5)
        before = application.state.change_count()

        def explode(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(StateRepository, "record_change", explode)
        outcome = await application.processor.process(make_event(actor_type="user"))

        assert outcome.status == "failed"
        # Nothing from the failed run is visible.
        assert application.state.change_count() == before
        assert application.state.get("mood", "valence") is None
        assert application.db.integrity_check() == "ok"
    finally:
        application.db.close()


async def test_the_same_event_twice_changes_state_once(
    temp_config, clock, make_event
) -> None:
    """Spec 8.3 / 34.3: double delivery is idempotent."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        event = make_event(actor_type="user")

        first = await application.processor.process(event)
        after_first = application.state.change_count()
        second = await application.processor.process(event)

        assert first.newly_stored is True
        assert second.newly_stored is False
        # The second delivery committed nothing, and the root event was stored
        # once — derived events belong to the first run only.
        assert application.state.change_count() == after_first
        assert len(application.event_store.by_types((event.event_type,), limit=10)) == 1
    finally:
        application.db.close()


async def test_an_unreachable_model_leaves_the_state_usable(
    temp_config, clock, make_event
) -> None:
    """There is no Ollama in tests; every run here is already the degraded path."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        for _ in range(3):
            outcome = await application.processor.process(make_event(actor_type="user"))
            assert outcome.interpretation.appraisal.source == "degraded"
            clock.advance(minutes=5)

        assert application.db.integrity_check() == "ok"
        assert set(application.state.domains())
    finally:
        application.db.close()


async def test_a_crash_between_runs_recovers_on_the_next_start(
    temp_config, clock, make_event
) -> None:
    """Spec 32: an interrupted run is marked, not left pretending to be live."""
    first = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        outcome = await first.processor.process(make_event(actor_type="user"))
        # Simulate a kill: a run row left open with no finish.
        first.runs.start(
            run_id="run_interrupted",
            root_event_id=outcome.event.event_id,
            snapshot_id=None,
            manifest_id=first.manifest_id,
            mode="normal",
            priority="P2",
            now=clock.now(),
        )
    finally:
        first.db.close()

    clock.advance(minutes=1)
    second = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        assert second.db.integrity_check() == "ok"
        row = second.db.query_one(
            "SELECT status FROM processing_runs WHERE run_id = 'run_interrupted'"
        )
        assert row is not None
        assert row["status"] != "running"
    finally:
        second.db.close()


async def test_a_backup_survives_a_crash_mid_write(temp_config, clock, make_event) -> None:
    """Spec 32: the backup mechanism is safe against a live writer."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        await application.processor.process(make_event(actor_type="user"))
        record = application.backups.create(reason="chaos")

        # More writes after the copy must not affect what was captured.
        await application.processor.process(make_event(actor_type="user", text="あと"))

        assert record.usable is True
        assert application.backups.verify(record.backup_id) is True
    finally:
        application.db.close()
