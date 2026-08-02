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

from app.admin.repair import CONFIRMATION, RepairRefused
from app.admin.shadow import rebuild_genesis, replay_real_history
from app.bootstrap import Application, StartupError
from app.config import AppConfig, ConfigError, load_config
from app.interfaces.discord.gateway import DiscordGateway
from app.observability.logging import configure_logging
from app.storage.database import Database
from app.storage.migrations import LATEST_VERSION, migrate, schema_version
from app.storage.repositories.traces import ConversationTraceRepository

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yui", description="YUI v2")
    parser.add_argument("--config", type=Path, default=None, help="path to settings.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="start the application and hold it ready")
    subparsers.add_parser("migrate", help="apply pending database migrations")
    subparsers.add_parser("status", help="print schema, manifest and store counters")
    backup = subparsers.add_parser("backup", help="take a verified backup (spec 32)")
    backup.add_argument("--reason", default="manual", help="why this backup was taken")

    # Patch spec 23. Three separate commands on purpose: diagnosing, rebuilding
    # and switching are three decisions, and only the last one touches the
    # database the USER's history is in.
    subparsers.add_parser(
        "diagnose", help="report the health of the Genesis under this database (23.3)"
    )
    latency = subparsers.add_parser(
        "latency", help="reply-latency percentiles from the conversation traces (19.2)"
    )
    latency.add_argument(
        "--limit", type=int, default=200, help="how many recent turns to read"
    )
    repair = subparsers.add_parser(
        "repair", help="rebuild a broken Genesis in a shadow database (23.4)"
    )
    repair.add_argument(
        "--apply",
        action="store_true",
        help="build the shadow and replay real history (without this, dry run only)",
    )
    repair.add_argument(
        "--switch",
        default="",
        metavar="CONFIRMATION",
        help=(
            f"replace production with the verified shadow. Requires {CONFIRMATION!r}; "
            "the old database is kept as the rollback"
        ),
    )
    repair.add_argument(
        "--max-blocks", type=int, default=None, help="cap the rebuilt simulation"
    )
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


def _backup(config_file: Path | None, reason: str) -> int:
    """Take a backup with SQLite's own mechanism and verify it (spec 32)."""
    config = load_config(config_file)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        record = application.backups.create(kind="manual", reason=reason)
        sys.stdout.write(
            json.dumps(
                {
                    "backup_id": record.backup_id,
                    "path": str(record.path),
                    "size_bytes": record.size_bytes,
                    "integrity": record.integrity,
                    "restore_tested": record.restore_tested,
                    "usable": record.usable,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0 if record.usable else 1
    finally:
        application.db.close()


def _latency(config_file: Path | None, limit: int) -> int:
    """Patch spec 19.2 / 20: what the USER's wait actually looks like.

    Reported from recorded turns only. With nothing recorded it says so rather
    than printing a zero that reads like a passing measurement.
    """
    config = load_config(config_file)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        traces = ConversationTraceRepository(application.db)
        samples = sorted(traces.latencies(limit=limit))
        if not samples:
            sys.stdout.write(
                json.dumps({"turns": 0, "note": "no conversation turns recorded yet"})
                + "\n"
            )
            return 1
        report = {
            "turns": len(samples),
            "median_ms": _percentile(samples, 0.50),
            "p95_ms": _percentile(samples, 0.95),
            "max_ms": samples[-1],
            # Spec 26: warm short DM median <= 15s, p95 <= 30s.
            "meets_median_target": _percentile(samples, 0.50) <= 15_000,
            "meets_p95_target": _percentile(samples, 0.95) <= 30_000,
        }
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return 0 if report["meets_median_target"] and report["meets_p95_target"] else 1
    finally:
        application.db.close()


def _percentile(sorted_samples: list[int], fraction: float) -> int:
    """Nearest-rank, so a small sample reports a value that really happened."""
    index = max(0, min(len(sorted_samples) - 1, round(fraction * len(sorted_samples)) - 1))
    return sorted_samples[index]


def _diagnose(config_file: Path | None) -> int:
    """Patch spec 23.3: say what is wrong. Change nothing."""
    config = load_config(config_file)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        report = application.legacy.scan()
        sys.stdout.write(
            json.dumps(report.as_detail(), indent=2, ensure_ascii=False) + "\n"
        )
        return 0 if report.sound else 1
    finally:
        application.db.close()


def _repair(
    config_file: Path | None,
    *,
    apply: bool,
    switch: str,
    max_blocks: int | None,
) -> int:
    """Patch spec 23.4. Dry run unless asked; never switches unless confirmed."""
    config = load_config(config_file)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        plan = application.repair.dry_run()
        if not apply:
            sys.stdout.write(plan.render() + "\n")
            return 0 if not plan.needed or plan.possible else 1
        if not plan.possible:
            sys.stdout.write(plan.render() + "\n")
            return 1

        async def rebuild(shadow_path, seed, scaffold):
            return await rebuild_genesis(
                config=config,
                shadow_path=shadow_path,
                seed=seed,
                scaffold=scaffold,
                clock=application.clock,
                max_blocks=max_blocks,
            )

        async def replay(shadow_path, events):
            report = await replay_real_history(
                config=config,
                shadow_path=shadow_path,
                events=events,
                clock=application.clock,
            )
            return report.processed

        result = asyncio.run(
            application.repair.rebuild(rebuild=rebuild, replay=replay, plan=plan)
        )
        payload = {
            "plan": plan.as_detail(),
            "backup": None if result.backup is None else str(result.backup.path),
            "replayed": result.replayed,
            "verification": result.verification.as_detail(),
            "refusal": result.refusal,
        }
        if switch:
            application.repair.switch(result, confirmation=switch)
            payload["switched"] = result.switched
            payload["rollback"] = str(result.rollback_path)
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 0 if result.ready_to_switch else 1
    finally:
        application.db.close()


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
            "npcs": len(application.society.people()),
            "tracked_npcs": len(application.society.tracked_people()),
            "npc_stages": {
                relationship.npc_id: relationship.stage
                for relationship in application.npc_relationships.all_relationships()[:5]
            },
            "groups": [group.name for group in application.society.groups_of_yui()],
            "knowledge": application.knowledge.counts(),
            "coverage_jobs": len(application.knowledge_builder.coverage_jobs()),
            "simulation": (
                None
                if application.simulation.latest_run() is None
                else {
                    "status": application.simulation.latest_run().status,
                    "blocks": application.simulation.latest_run().blocks_run,
                    "experiences": application.simulation.latest_run().experiences,
                }
            ),
            "first_boot": application.genesis.has_booted(),
            "admin_actions": application.admin.count(),
            "backups": application.backups.count(),
            "latest_backup": (
                None
                if application.backups.latest_usable() is None
                else str(application.backups.latest_usable().path)
            ),
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
        if args.command == "backup":
            return _backup(args.config, args.reason)
        if args.command == "diagnose":
            return _diagnose(args.config)
        if args.command == "latency":
            return _latency(args.config, args.limit)
        if args.command == "repair":
            return _repair(
                args.config,
                apply=args.apply,
                switch=args.switch,
                max_blocks=args.max_blocks,
            )
    except RepairRefused as exc:
        logging.getLogger("app.main").error("repair refused: %s", exc)
        return 3
    except (ConfigError, StartupError) as exc:
        logging.getLogger("app.main").error("%s", exc)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
