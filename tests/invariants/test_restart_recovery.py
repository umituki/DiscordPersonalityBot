"""INVARIANT: state is continuous across restart and crash (spec 41, 32).

``Restart / crash 後も状態が連続する`` is the first completion condition of v2.
"""

from __future__ import annotations

import pytest

from app.bootstrap import Application
from app.config import AppConfig
from app.state.proposal import StateChangeProposal

pytestmark = pytest.mark.invariant


def build(config: AppConfig, clock) -> Application:
    return Application.build(config, clock=clock, configure_logs=False)


def replace_emotion_engine(application: Application, engine) -> None:
    """Swap the real emotion engine for a deterministic stand-in.

    ``emotion`` has exactly one writer (spec 9.3), so a test double has to take
    the real engine's place rather than sit beside it.
    """
    application.bus.unregister("emotion_engine")
    application.bus.register(engine, kind="psychology", order=20)


class JoyEngine:
    name = "emotion_engine"

    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, event, snapshot):
        from app.events.bus import SubscriberResult

        self.calls += 1
        return SubscriberResult(
            proposals=(
                StateChangeProposal.adjust(
                    source_event_id=event.event_id,
                    source_module="emotion_engine",
                    target_domain="emotion",
                    target_key="joy",
                    magnitude=0.1,
                ),
            )
        )


async def test_state_and_history_survive_restart(temp_config: AppConfig, clock, make_event) -> None:
    first = build(temp_config, clock)
    first.state.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    replace_emotion_engine(first, JoyEngine())
    event = make_event()
    outcome = await first.processor.process(event)
    assert outcome.status == "committed"
    await first.stop("test restart")

    second = build(temp_config, clock)
    try:
        assert second.state.get("emotion", "joy").value == pytest.approx(0.4)
        assert second.event_store.exists(event.event_id)
        assert len(second.state.change_history("emotion", "joy")) == 1
    finally:
        second.db.close()


async def test_event_is_not_reprocessed_after_restart(
    temp_config: AppConfig, clock, make_event
) -> None:
    first = build(temp_config, clock)
    first.state.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    replace_emotion_engine(first, JoyEngine())
    event = make_event()
    await first.processor.process(event)
    first.db.close()

    second = build(temp_config, clock)
    engine = JoyEngine()
    replace_emotion_engine(second, engine)
    try:
        await second.processor.process(event)
        assert engine.calls == 0  # already consumed before the restart
        assert second.state.get("emotion", "joy").value == pytest.approx(0.4)
    finally:
        second.db.close()


async def test_interrupted_run_is_closed_on_startup(temp_config: AppConfig, clock, make_event) -> None:
    first = build(temp_config, clock)
    event = make_event()
    first.event_store.append(event)
    first.runs.start(
        run_id="run_CRASHED", root_event_id=event.event_id, snapshot_id=None,
        manifest_id=None, mode="test", priority="P0", now=clock.now(),
    )
    # Simulate power loss: the process disappears with the run still open.
    first.db.close()

    second = build(temp_config, clock)
    try:
        row = second.runs.get("run_CRASHED")
        assert row["status"] == "interrupted"
        assert second.runs.unfinished() == []
    finally:
        second.db.close()


async def test_incomplete_deliveries_are_visible_after_restart(
    temp_config: AppConfig, clock, make_event
) -> None:
    first = build(temp_config, clock)
    event = make_event()
    first.event_store.append(event)
    first.deliveries.begin_attempt(event.event_id, "emotion_engine", now=clock.now())
    first.db.close()

    second = build(temp_config, clock)
    try:
        pending = second.deliveries.incomplete()
        assert (event.event_id, "emotion_engine", "pending") in pending
    finally:
        second.db.close()
