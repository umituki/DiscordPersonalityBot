"""Application assembly and lifecycle (spec 32, 35 Phase 0)."""

from __future__ import annotations

import json

import pytest

from app.bootstrap import Application, StartupError
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


async def test_starts_degraded_when_the_model_is_unreachable(
    temp_config: AppConfig, clock
) -> None:
    """Spec 28.3: an unavailable model degrades the runtime, it does not corrupt it."""
    application = build(temp_config, clock)
    try:
        await application.start()
        assert application.started is True
        assert application.llm_healthy is False
    finally:
        await application.stop("test")


async def test_required_model_blocks_readiness(temp_config: AppConfig, clock) -> None:
    strict = temp_config.model_copy(
        update={"llm": temp_config.llm.model_copy(update={"require_healthy_on_start": True})}
    )
    application = build(strict, clock)
    with pytest.raises(StartupError, match="not available"):
        await application.start()
    assert application.db.is_open is False


def test_manifest_tracks_model_and_prompt_versions(temp_config: AppConfig, clock) -> None:
    """Spec 2.17: a model or prompt change is a software change, not growth."""
    application = build(temp_config, clock)
    try:
        row = application.db.query_one(
            "SELECT components_json FROM runtime_manifests WHERE manifest_id = ?",
            (application.manifest_id,),
        )
        components = json.loads(row["components_json"])
        assert components["model_version"] == temp_config.llm.model
        assert components["prompt.structured_repair"].startswith("v1+")
    finally:
        application.db.close()


def test_changing_the_model_creates_a_new_manifest(temp_config: AppConfig, clock) -> None:
    first = build(temp_config, clock)
    original = first.manifest_id
    first.db.close()

    upgraded = temp_config.model_copy(
        update={"llm": temp_config.llm.model_copy(update={"model": "qwen3.5:14b"})}
    )
    second = build(upgraded, clock)
    try:
        assert second.manifest_id != original
    finally:
        second.db.close()
