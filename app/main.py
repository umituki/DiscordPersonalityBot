"""Command line entry point.

No Discord interface exists yet (Phase 3), so ``run`` starts the application,
holds it in a ready state, and shuts down cleanly on SIGINT/SIGTERM. That is
enough to verify the spec 35 Phase 0 "start/stop" acceptance criterion and the
Phase 1 restart-recovery behaviour.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from app.bootstrap import Application, StartupError
from app.config import AppConfig, ConfigError, load_config
from app.interfaces.discord.gateway import DiscordGateway
from app.observability.logging import configure_logging
from app.storage.database import Database
from app.storage.migrations import LATEST_VERSION, migrate, schema_version

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yui", description="YUI v2")
    parser.add_argument("--config", type=Path, default=None, help="path to settings.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="start the application and hold it ready")
    subparsers.add_parser("migrate", help="apply pending database migrations")
    subparsers.add_parser("status", help="print schema, manifest and store counters")
    return parser


def _build_gateway(application: Application, config: AppConfig) -> DiscordGateway | None:
    """Attach Discord only when both the USER and a token are configured."""
    if application.conversation is None:
        return None
    if config.secrets.discord_bot_token is None:
        logger.warning("DISCORD_BOT_TOKEN is not set; running without the Discord gateway")
        return None
    return DiscordGateway(
        application.conversation,
        token=config.secrets.require_discord_token(),
        clock=application.clock,
    )


async def _run(config_file: Path | None) -> int:
    config = load_config(config_file)
    application = Application.build(config)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, stop_event.set)
        except NotImplementedError:  # Windows without ProactorEventLoop support
            signal.signal(signal_number, lambda *_: stop_event.set())

    await application.start()

    gateway = _build_gateway(application, config)
    gateway_task: asyncio.Task[None] | None = None
    if gateway is not None:
        gateway_task = asyncio.create_task(gateway.start(), name="discord-gateway")
        gateway_task.add_done_callback(lambda _task: stop_event.set())
        logger.info("discord gateway starting")
    else:
        logger.info("yui is ready; no interface attached")

    try:
        await stop_event.wait()
    finally:
        if gateway is not None:
            await gateway.close()
        if gateway_task is not None:
            gateway_task.cancel()
            try:
                await gateway_task
            except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
                if not isinstance(exc, asyncio.CancelledError):
                    logger.error("discord gateway ended with an error: %r", exc)
        await application.stop("signal")
    return 0


def _migrate(config_file: Path | None) -> int:
    config = load_config(config_file)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    with Database(
        config.database_path,
        journal_mode=config.database.journal_mode,
        synchronous=config.database.synchronous,
        busy_timeout_ms=config.database.busy_timeout_ms,
        foreign_keys=config.database.foreign_keys,
    ) as db:
        result = migrate(db)
        logger.info(
            "migrations applied=%s schema_version=%d (latest %d)",
            list(result.applied),
            result.schema_version,
            LATEST_VERSION,
        )
    return 0


def _status(config_file: Path | None) -> int:
    config = load_config(config_file)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        report = {
            "environment": config.app.environment,
            "database": str(config.database_path),
            "schema_version": schema_version(application.db),
            "latest_schema_version": LATEST_VERSION,
            "manifest_id": application.manifest_id,
            "policy_version": application.policy.policy_version,
            "events": application.event_store.count(),
            "runs": application.runs.count(),
            "state_changes": application.state.change_count(),
            "state_domains": application.state.domains(),
            "emotion": {
                value.key: value.value for value in application.state.list_domain("emotion")
            },
            "mood": {value.key: value.value for value in application.state.list_domain("mood")},
            "needs": {value.key: value.value for value in application.state.list_domain("needs")},
            "failures": application.failures.count(),
            "llm_model": config.llm.model,
            "llm_base_url": config.llm.base_url,
            "llm_calls": application.llm_calls.count(),
            "prompts": list(application.prompts.ids()),
            "episodes": application.memories.episode_count(),
            "episodic_memories": application.memories.memory_count(status="active"),
            "semantic_memories": application.memories.semantic_count(),
            "beliefs": application.beliefs.held_beliefs().__len__(),
            "self_schemas": len(application.self_model.active_schemas()),
            "tools": list(application.tools.registry.names()),
            "current_activity": (
                None
                if application.world.current_activity() is None
                else application.world.current_activity().name
            ),
            "pending_jobs": len(application.scheduler.pending()),
            "unanswered_contacts": application.proactive.unanswered(),
            "adaptations": {
                adaptation.name: round(adaptation.value, 3)
                for adaptation in application.adaptations.all()
            },
            "personality": {
                trait.name: round(trait.baseline, 3) for trait in application.growth.traits()
            },
            "values": {
                value.name: round(value.priority, 3)
                for value in application.values.ranking()[:3]
            },
            "narrative_themes": [theme.theme for theme in application.growth.themes(limit=5)],
            "deep_update_candidates": len(application.growth.pending_candidates()),
            "consolidation_due": application.consolidation.due(),
            "drift_anomalies": len(application.drift.open_anomalies()),
            "integrity": application.db.integrity_check(),
        }
    finally:
        application.db.close()
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_run(args.config))
        if args.command == "migrate":
            return _migrate(args.config)
        if args.command == "status":
            return _status(args.config)
    except (ConfigError, StartupError) as exc:
        logging.getLogger("app.main").error("%s", exc)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
