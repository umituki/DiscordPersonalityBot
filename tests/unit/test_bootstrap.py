"""Application assembly and lifecycle (spec 32, 35 Phase 0)."""

from __future__ import annotations

from app.bootstrap import Application
from app.config import AppConfig
from app.storage.migrations import LATEST_VERSION


def build(config: AppConfig, clock, **kwargs) -> Application:
    return Application.build(config, clock=clock, configure_logs=False, **kwargs)


async def test_build_start_stop(temp_config: AppConfig, clock) -> None:
    application = build(temp_config, clock)
    try:
        assert application.schema_version == LATEST_VERSION
        started = await application.start()
        assert started.event_type == "SYSTEM_STARTED"
        assert application.started is True
        await application.stop("test")
        assert application.started is False
    finally:
        if application.db.is_open:
            application.db.close()


async def test_lifecycle_events_are_recorded(temp_config: AppConfig, clock) -> None:
    application = build(temp_config, clock)
    async with application:
        pass

    reopened = build(temp_config, clock)
    try:
        types = [event.event_type for event in reopened.event_store.recent()]
        assert "SYSTEM_STARTED" in types
        assert "SYSTEM_STOPPED" in types
    finally:
        reopened.db.close()


async def test_manifest_is_stable_across_restarts(temp_config: AppConfig, clock) -> None:
    first = build(temp_config, clock)
    first_id = first.manifest_id
    first.db.close()

    second = build(temp_config, clock)
    try:
        assert second.manifest_id == first_id
    finally:
        second.db.close()


async def test_state_survives_a_restart(temp_config: AppConfig, clock) -> None:
    first = build(temp_config, clock)
    first.state.write_value(
        domain="needs", key="relatedness", value=0.61, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    first.db.close()

    second = build(temp_config, clock)
    try:
        assert second.state.get("needs", "relatedness").value == 0.61
    finally:
        second.db.close()


def test_directories_are_created_under_the_configured_root(temp_config: AppConfig, clock) -> None:
    application = build(temp_config, clock)
    try:
        assert temp_config.data_dir.is_dir()
        assert temp_config.database_path.is_file()
        assert temp_config.database_path.parent == temp_config.data_dir
    finally:
        application.db.close()
