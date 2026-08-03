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

from app.admin.rebuild import CONFIRMATION as REBUILD_CONFIRMATION
from app.admin.rebuild import RebuildRefused
from app.admin.repair import CONFIRMATION, RepairRefused
from app.admin.shadow import rebuild_genesis, replay_real_history
from app.bootstrap import Application, StartupError
from app.conversation.surface import BANDS
from app.memory.recall_mode import RecallMode
from app.clock import from_iso
from app.config import AppConfig, ConfigError, load_config
from app.interfaces.discord.gateway import DiscordGateway
from app.observability.logging import configure_logging
from app.storage.database import Database
from app.storage.migrations import LATEST_VERSION, migrate, schema_version
from app.storage.repositories.traces import ConversationTraceRepository
from app.versioning.capabilities import CapabilityContractError, load_contracts, summary

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yui", description="YUI v2")
    parser.add_argument("--config", type=Path, default=None, help="path to settings.yaml")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="project root that data/, logs/ and backups/ resolve against",
    )
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
    subparsers.add_parser(
        "capabilities", help="capability contract status (rebuild spec 4.3)"
    )
    subparsers.add_parser(
        "rebuild-status", help="which rebuild epoch this database is in"
    )
    # Rebuild spec 3.4: its own command, and it does not run without the exact
    # confirmation. A reset that can happen by accident is not a reset.
    reset = subparsers.add_parser(
        "rebuild-reset",
        help=(
            "archive this database and start a fresh person "
            f"(requires --confirm {REBUILD_CONFIRMATION})"
        ),
    )
    reset.add_argument("--confirm", default="", metavar="CONFIRMATION")
    reset.add_argument("--reason", default="full rebuild")
    latency = subparsers.add_parser(
        "latency", help="reply-latency percentiles from the conversation traces (19.2)"
    )
    latency.add_argument(
        "--limit", type=int, default=200, help="how many recent turns to read"
    )
    memory_find = subparsers.add_parser(
        "memory-find",
        help="why a memory would or would not be recalled (Phase 2 debug inspector)",
    )
    memory_find.add_argument("query", help="what to search for")
    memory_find.add_argument(
        "--mode",
        default=None,
        choices=[mode.value for mode in RecallMode],
        help="force a recall mode instead of inferring one from the query",
    )
    conversation_plan = subparsers.add_parser(
        "conversation-plan",
        help="what would be decided for this message, before any sentence (Phase 3)",
    )
    conversation_plan.add_argument("message", help="the USER message to read")
    conversation_plan.add_argument(
        "--band",
        default="acquaintance",
        choices=list(BANDS),
        help="pretend the relationship is at this band",
    )
    first_boot = subparsers.add_parser(
        "first-boot", help="the once-per-epoch Genesis (spec 34.20)"
    )
    first_boot.add_argument(
        "action",
        choices=("status", "start", "resume", "pause", "audit", "report"),
        help=(
            "status: what state it is in; start: begin a new life (refused if one "
            "exists); resume: carry on this epoch's run; audit: check without "
            "changing anything; report: the completion record"
        ),
    )
    first_boot.add_argument(
        "--birth",
        default="",
        metavar="ISO8601",
        help="her birth datetime. Required for start; ignored afterwards (17)",
    )
    first_boot.add_argument(
        "--present",
        default="",
        metavar="ISO8601",
        help="the moment her past ends. Defaults to now",
    )

    shadow = subparsers.add_parser(
        "shadow", help="review what she would have done (spec 47)"
    )
    shadow.add_argument(
        "action",
        choices=("status", "list", "suppressed", "review"),
        help=(
            "status: the mode of each capability and whether shadow has seen "
            "anything; list: recent decisions; suppressed: the ones she wanted "
            "and was not allowed; review: record the OWNER's verdict on one"
        ),
    )
    shadow.add_argument(
        "--capability", default="", help="narrow to one of the four"
    )
    shadow.add_argument("--id", default="", metavar="SHADOW_ID", help="for review")
    shadow.add_argument("--note", default="", help="the OWNER's verdict")
    shadow.add_argument("--limit", type=int, default=20)

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


def _line(text: str) -> str:
    """One line of CLI output.

    Spec 38 keeps ``print`` out of the application: operational output is
    logging, and everything a command deliberately shows an operator goes to
    stdout on purpose.
    """
    return f"{text}\n"


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
        # Rebuild spec 30, Phase 5: admin is routed before conversation.
        admin=application.admin_router,
        first_boot=application.first_boot,
        clock=application.clock,
    )


async def _run(config_file: Path | None, root: Path | None = None) -> int:
    config = load_config(config_file, root_dir=root)
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


def _migrate(config_file: Path | None, root: Path | None = None) -> int:
    config = load_config(config_file, root_dir=root)
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


def _backup(config_file: Path | None, reason: str, root: Path | None = None) -> int:
    """Take a backup with SQLite's own mechanism and verify it (spec 32)."""
    config = load_config(config_file, root_dir=root)
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


def _capabilities(config_file: Path | None, root: Path | None = None) -> int:
    """Rebuild spec 4.3: what actually runs, per capability."""
    config = load_config(config_file, root_dir=root)
    contracts = load_contracts(config.root_dir / "config" / "capabilities")
    report = {
        "summary": summary(contracts),
        "capabilities": {
            name: {"status": contract.status, "acceptance_test": contract.acceptance_test}
            for name, contract in sorted(contracts.items())
        },
    }
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    # Nothing is complete until every capability is E2E_VERIFIED (4.2).
    return 0 if summary(contracts)["E2E_VERIFIED"] == len(contracts) else 1


def _rebuild_status(config_file: Path | None, root: Path | None = None) -> int:
    config = load_config(config_file, root_dir=root)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        epoch = application.rebuild.current_epoch()
        if epoch is None:
            sys.stdout.write(
                json.dumps({"epoch": None, "note": "no rebuild has been performed"})
                + "\n"
            )
            return 1
        sys.stdout.write(
            json.dumps(
                {
                    "epoch_id": epoch["epoch_id"],
                    "started_at": epoch["started_at"],
                    "spec_version": epoch["spec_version"],
                    "schema_version": epoch["schema_version"],
                    "genesis_status": epoch["genesis_status"],
                    "archived_db": epoch["archived_db_path"],
                    "backup": epoch["backup_path"],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0
    finally:
        application.db.close()


def _rebuild_reset(
    config_file: Path | None,
    *,
    confirm: str,
    reason: str,
    root: Path | None = None,
) -> int:
    """Rebuild spec 3.4. Archives the old database; never deletes it.

    The confirmation is checked before anything is opened: a mistyped reset
    must not so much as touch the database it was about to replace.
    """
    if confirm != REBUILD_CONFIRMATION:
        raise RebuildRefused(
            f"a rebuild reset requires --confirm {REBUILD_CONFIRMATION}"
        )
    config = load_config(config_file, root_dir=root)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        result = application.rebuild.reset(confirmation=confirm, reason=reason)
        sys.stdout.write(json.dumps(result.as_detail(), indent=2, ensure_ascii=False) + "\n")
        return 0 if result.ok else 1
    finally:
        application.db.close()


def _latency(config_file: Path | None, limit: int, root: Path | None = None) -> int:
    """Patch spec 19.2 / 20: what the USER's wait actually looks like.

    Reported from recorded turns only. With nothing recorded it says so rather
    than printing a zero that reads like a passing measurement.
    """
    config = load_config(config_file, root_dir=root)
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


def _diagnose(config_file: Path | None, root: Path | None = None) -> int:
    """Patch spec 23.3: say what is wrong. Change nothing."""
    config = load_config(config_file, root_dir=root)
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
    root: Path | None = None,
) -> int:
    """Patch spec 23.4. Dry run unless asked; never switches unless confirmed."""
    config = load_config(config_file, root_dir=root)
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


def _status(config_file: Path | None, root: Path | None = None) -> int:
    config = load_config(config_file, root_dir=root)
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


def _memory_find(
    config_file: Path | None, query: str, mode: str | None, root: Path | None = None
) -> int:
    """Phase 2 debug inspector. Reads only — this cannot change memory state.

    It goes through :class:`MemoryInspector`, which holds no writer at all, so
    running it a hundred times leaves accessibility and recall_count exactly
    where they were.
    """
    config = load_config(config_file, root_dir=root)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        report = asyncio.run(
            application.memory_inspector.describe(
                query, mode=RecallMode(mode) if mode else None
            )
        )
        sys.stdout.write(report + "\n")
        return 0
    finally:
        application.db.close()


def _conversation_plan(
    config_file: Path | None, message: str, band: str, root: Path | None = None
) -> int:
    """Phase 3 §46. What the turn would decide, without deciding anything.

    This runs the *same* ``plan_turn`` the reply path runs — a preview that took
    a different path would be a preview of a different system. It stops before
    the realizer, writes nothing and sends nothing.
    """
    config = load_config(config_file, root_dir=root)
    config.ensure_directories()
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        plan = asyncio.run(
            application.conversation_engine.plan_turn(
                user_text=message, relationship_band=band  # type: ignore[arg-type]
            )
        )
        social, surface = plan.social, plan.surface
        hints, references = plan.style_hints, plan.references
        sys.stdout.write(
            "social:\n"
            f"  primary_move: {social.primary_move}\n"
            f"  secondary_move: {social.secondary_move}\n"
            f"  question: {social.question}\n"
            f"  tone: {social.tone}\n"
            f"  response_energy: {social.response_energy}\n"
            f"  topic_direction: {social.topic_direction}\n"
            f"  user_state_hint: {social.user_state_hint}\n"
            f"  self_disclosure: {social.self_disclosure}\n"
            f"  source: {social.source}\n"
            "\nsurface:\n"
            f"  length: {surface.length}\n"
            f"  register: {surface.register}\n"
            f"  directness: {surface.directness}\n"
            f"  question_budget: {surface.question_budget}\n"
            f"  initiative: {surface.initiative}\n"
            f"  relationship_band: {surface.relationship_band}\n"
            "\nintent:\n  "
            + plan.intent.render().replace("\n", "\n  ")
            + "\n"
            "\nreferences:\n"
            f"  {len(references)}\n"
            "\nstyle hints:\n"
            f"  {hints.render() or '(none)'}\n"
            "\nNo state mutation.\n"
        )
        return 0
    finally:
        application.db.close()


async def _first_boot(
    config_path: Path | None, *, action: str, birth: str, present: str,
    root: Path | None = None,
) -> int:
    """The FIRST BOOT CLI (points 12-14).

    Every action goes through the orchestrator. This function parses arguments
    and prints; it makes no decision about whether YUI exists, because there is
    exactly one thing allowed to make that decision and it is not the CLI.
    """
    config = load_config(config_path, root_dir=root)
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        orchestrator = application.first_boot
        if action == "status":
            sys.stdout.write(_line(orchestrator.progress().describe()))
            return 0
        if action == "report":
            report = orchestrator.report()
            sys.stdout.write(_line(json.dumps(report, ensure_ascii=False, indent=2) if report else "(no report yet)"))
            return 0
        if action == "audit":
            outcome = await orchestrator.audit()
            for audit in outcome.audits:
                sys.stdout.write(_line(f"[{'ok' if audit.passed else 'FAIL'}] {audit.name} {audit.detail}"))
            if not outcome.ok:
                sys.stdout.write(_line(f"\nnot eligible for completion: {outcome.reason}"))
            return 0 if outcome.ok else 1
        if action == "pause":
            # Point 59. Stop, keep everything, resume later. The running
            # process notices at its next checkpoint.
            outcome = orchestrator.pause()
            sys.stdout.write(_line(outcome.describe()))
            return 0 if outcome.ok else 1
        if action == "start":
            anchors = _anchors_from(application, birth, present)
            if anchors is None:
                sys.stdout.write(_line("--birth is required to start (ISO 8601)"))
                return 2
            outcome = await orchestrator.start(anchors)
        else:
            # Point 14: resume takes no run id. The epoch's own run is the only
            # one it may continue — letting an operator name one is how a
            # different life gets attached to the current epoch.
            outcome = await orchestrator.resume()

        sys.stdout.write(_line(outcome.describe()))
        if outcome.preflight is not None and not outcome.preflight.passed:
            sys.stdout.write(_line(outcome.preflight.describe()))
        if outcome.audits:
            for audit in outcome.audits:
                sys.stdout.write(_line(f"[{'ok' if audit.passed else 'FAIL'}] {audit.name} {audit.detail}"))
        sys.stdout.write("\n")
        sys.stdout.write(_line(orchestrator.progress().describe()))
        return 0 if outcome.ok else 1
    finally:
        application.db.close()


def _shadow(
    config_path: Path | None,
    *,
    action: str,
    capability: str,
    shadow_id: str,
    note: str,
    limit: int,
    root: Path | None = None,
) -> int:
    """The OWNER review surface (spec 46, 47).

    Shadow mode is only worth running if somebody reads the result, and a
    review that has to be assembled by hand from four tables does not happen.
    """
    config = load_config(config_path, root_dir=root)
    configure_logging(level=config.logging.level, log_file=config.log_path)
    application = Application.build(config, auto_migrate=False, configure_logs=False)
    try:
        repository = application.shadow_decisions
        if action == "status":
            for name, mode in sorted(application.shadow.as_dict().items()):
                sys.stdout.write(_line(f"{name:22} {mode}"))
            sys.stdout.write(_line(""))
            for row in repository.tally():
                sys.stdout.write(
                    _line(
                        f"{row['capability']:22} {row['mode']:7} "
                        f"considered {row['considered']:4} "
                        f"wanted {row['wanted'] or 0:4} acted {row['acted'] or 0:4}"
                    )
                )
            waiting = repository.unreviewed_count()
            sys.stdout.write(_line(f"\n{waiting} decision(s) awaiting review"))
            return 0
        if action in ("list", "suppressed"):
            rows = (
                repository.suppressed(limit=limit)
                if action == "suppressed"
                else repository.recent(capability=capability, limit=limit)
            )
            if not rows:
                sys.stdout.write(_line("(nothing recorded)"))
                return 0
            for row in rows:
                mark = "reviewed" if row["reviewed_at"] else "-"
                sys.stdout.write(
                    _line(
                        f"{row['shadow_id']} {row['decided_at']} "
                        f"{row['capability']} {row['mode']} "
                        f"would={bool(row['would_act'])} acted={bool(row['acted'])} "
                        f"{mark} {row['subject'][:60]}"
                    )
                )
            return 0
        if not shadow_id or not note:
            sys.stdout.write(_line("review needs --id and --note"))
            return 2
        if repository.get(shadow_id) is None:
            sys.stdout.write(_line(f"no such decision: {shadow_id}"))
            return 1
        repository.review(shadow_id, note=note, now=application.clock.now())
        sys.stdout.write(_line(f"reviewed {shadow_id}"))
        return 0
    finally:
        application.db.close()


def _anchors_from(application: Application, birth: str, present: str):
    from app.genesis.anchors import LifeAnchors

    if not birth:
        return None
    identity = application.identity
    return LifeAnchors(
        birth_datetime=from_iso(birth),
        present_datetime=from_iso(present) if present else application.clock.now(),
        gender_identity=getattr(identity, "gender", "") or "",
        embodiment=getattr(identity, "embodiment", "") or "デジタルな存在。",
        language="ja",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_run(args.config, args.root))
        if args.command == "migrate":
            return _migrate(args.config, args.root)
        if args.command == "status":
            return _status(args.config, args.root)
        if args.command == "backup":
            return _backup(args.config, args.reason, args.root)
        if args.command == "diagnose":
            return _diagnose(args.config, args.root)
        if args.command == "latency":
            return _latency(args.config, args.limit, args.root)
        if args.command == "capabilities":
            return _capabilities(args.config, args.root)
        if args.command == "rebuild-status":
            return _rebuild_status(args.config, args.root)
        if args.command == "rebuild-reset":
            return _rebuild_reset(
                args.config, confirm=args.confirm, reason=args.reason, root=args.root
            )
        if args.command == "memory-find":
            return _memory_find(args.config, args.query, args.mode, args.root)
        if args.command == "conversation-plan":
            return _conversation_plan(args.config, args.message, args.band, args.root)
        if args.command == "first-boot":
            return asyncio.run(
                _first_boot(
                    args.config,
                    action=args.action,
                    birth=args.birth,
                    present=args.present,
                    root=args.root,
                )
            )
        if args.command == "shadow":
            return _shadow(
                args.config,
                action=args.action,
                capability=args.capability,
                shadow_id=args.id,
                note=args.note,
                limit=args.limit,
                root=args.root,
            )
        if args.command == "repair":
            return _repair(
                args.config,
                apply=args.apply,
                switch=args.switch,
                max_blocks=args.max_blocks,
                root=args.root,
            )
    except RebuildRefused as exc:
        logging.getLogger("app.main").error("rebuild refused: %s", exc)
        return 3
    except CapabilityContractError as exc:
        logging.getLogger("app.main").error("%s", exc)
        return 2
    except RepairRefused as exc:
        logging.getLogger("app.main").error("repair refused: %s", exc)
        return 3
    except (ConfigError, StartupError) as exc:
        logging.getLogger("app.main").error("%s", exc)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
