"""Application assembly and startup sequence.

Spec 32 startup order, restricted to what exists so far::

    config
    → logging
    → open database
    → integrity check
    → schema / migration
    → crash recovery
    → prompts / runtime manifest
    → readiness
    → Ollama health

World catch-up, scheduler restore and the Discord connection are added by their
own phases.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.clock import Clock, SystemClock
from app.config import AppConfig, load_config
from app.events.bus import EventBus
from app.events.dispatcher import EventDispatcher
from app.events.model import Event, SystemStartedPayload, SystemStoppedPayload
from app.events.store import EventStore
from app.llm.client import TracedLLMClient
from app.llm.ollama import OllamaClient
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.tracing import DatabaseTracer
from app.observability.logging import configure_logging
from app.orchestrator.processor import EventProcessor
from app.state.arbitrator import StateArbitrator
from app.state.committer import StateCommitter
from app.state.dependency_graph import validate_registry
from app.state.policy import ArbitrationPolicy
from app.state.snapshot import SnapshotService
from app.storage.database import Database
from app.storage.migrations import LATEST_VERSION, migrate, schema_version, verify_schema
from app.storage.repositories import (
    DeliveryRepository,
    EventRepository,
    FailureRepository,
    LLMCallRepository,
    ManifestRepository,
    ProcessingRunRepository,
    SnapshotRepository,
    StateRepository,
)
from app.versioning.manifest import RuntimeManifest, ManifestService, base_components, detect_commit_hash

logger = logging.getLogger(__name__)


class StartupError(RuntimeError):
    """Raised when the application cannot reach a safe ready state."""


@dataclass
class Application:
    """Wired application. Construct with :meth:`build`."""

    config: AppConfig
    clock: Clock
    db: Database
    policy: ArbitrationPolicy
    event_store: EventStore
    bus: EventBus
    dispatcher: EventDispatcher
    snapshots: SnapshotService
    arbitrator: StateArbitrator
    committer: StateCommitter
    processor: EventProcessor
    state: StateRepository
    runs: ProcessingRunRepository
    deliveries: DeliveryRepository
    failures: FailureRepository
    llm_calls: LLMCallRepository
    prompts: PromptRegistry
    llm: TracedLLMClient
    structured: StructuredGenerator
    manifest_id: str
    schema_version: int
    started: bool = False
    llm_healthy: bool = False

    # --- construction ------------------------------------------------------
    @classmethod
    def build(
        cls,
        config: AppConfig | None = None,
        *,
        clock: Clock | None = None,
        auto_migrate: bool = True,
        configure_logs: bool = True,
    ) -> Application:
        resolved_config = config or load_config()
        resolved_clock = clock or SystemClock()
        resolved_config.ensure_directories()

        if configure_logs:
            configure_logging(
                level=resolved_config.logging.level,
                log_file=resolved_config.log_path,
                fmt=resolved_config.logging.format,
            )

        # Structural self-check: every owned domain has a layer (spec 9.1/9.3).
        validate_registry()

        policy = ArbitrationPolicy.load(resolved_config.state_arbitration_policy_path)

        db = Database(
            resolved_config.database_path,
            journal_mode=resolved_config.database.journal_mode,
            synchronous=resolved_config.database.synchronous,
            busy_timeout_ms=resolved_config.database.busy_timeout_ms,
            foreign_keys=resolved_config.database.foreign_keys,
        )
        db.connect()

        integrity = db.integrity_check()
        if integrity != "ok":
            raise StartupError(f"database integrity check failed: {integrity}")

        if auto_migrate:
            migrate(db, clock=resolved_clock)
        verify_schema(db, expected_version=LATEST_VERSION)
        current_schema = schema_version(db)

        # --- repositories ---------------------------------------------------
        events_repo = EventRepository(db)
        deliveries = DeliveryRepository(db)
        failures = FailureRepository(db)
        runs = ProcessingRunRepository(db)
        state_repo = StateRepository(db)
        snapshot_repo = SnapshotRepository(db)
        manifest_repo = ManifestRepository(db)
        llm_call_repo = LLMCallRepository(db)

        # --- crash recovery (spec 32) ---------------------------------------
        interrupted = runs.mark_interrupted(now=resolved_clock.now())
        if interrupted:
            logger.warning("recovered %d interrupted run(s) from a previous process", interrupted)

        # --- prompts (spec 38: versioned prompt files, never inline) ---------
        prompts = PromptRegistry.load(resolved_config.prompts_dir)

        # --- runtime manifest (spec 29) -------------------------------------
        components = base_components(
            policy_version=policy.policy_version, schema_version=current_schema
        )
        components["model_version"] = resolved_config.llm.model
        components["llm_provider"] = resolved_config.llm.provider
        components.update(prompts.manifest_components())
        manifest = RuntimeManifest(
            config_version=resolved_config.config_version,
            event_schema_version=1,
            code_commit_hash=detect_commit_hash(resolved_config.root_dir),
            components=components,
        )
        manifest_record = ManifestService(manifest_repo, clock=resolved_clock).ensure(manifest)

        # --- pipeline --------------------------------------------------------
        event_store = EventStore(db, events_repo, clock=resolved_clock)
        bus = EventBus()
        dispatcher = EventDispatcher(bus, deliveries, failures, clock=resolved_clock)
        snapshots = SnapshotService(state_repo, snapshot_repo, clock=resolved_clock)
        arbitrator = StateArbitrator(policy)
        committer = StateCommitter(
            db, state_repo, runs, deliveries, failures, clock=resolved_clock
        )
        ollama = OllamaClient(
            base_url=resolved_config.llm.base_url,
            model=resolved_config.llm.model,
            request_timeout_s=resolved_config.llm.request_timeout_s,
            connect_timeout_s=resolved_config.llm.connect_timeout_s,
            concurrency=resolved_config.llm.concurrency,
            num_ctx=resolved_config.llm.num_ctx,
            temperature=resolved_config.llm.temperature,
            keep_alive=resolved_config.llm.keep_alive,
            clock=resolved_clock,
        )
        tracer = DatabaseTracer(
            llm_call_repo,
            clock=resolved_clock,
            manifest_id=manifest_record.manifest_id,
            trace_payloads=resolved_config.llm.trace_payloads,
            max_traced_chars=resolved_config.llm.max_traced_chars,
        )
        # Free-form calls are traced by the decorator; structured generation
        # traces every attempt itself, so it wraps the raw client instead.
        llm_client = TracedLLMClient(ollama, tracer, clock=resolved_clock)
        structured = StructuredGenerator(
            ollama,
            prompts=prompts,
            failures=failures,
            tracer=tracer,
            clock=resolved_clock,
            max_attempts=resolved_config.llm.max_attempts,
        )

        processor = EventProcessor(
            db=db,
            event_store=event_store,
            dispatcher=dispatcher,
            snapshots=snapshots,
            arbitrator=arbitrator,
            committer=committer,
            runs=runs,
            failures=failures,
            manifest_id=manifest_record.manifest_id,
            mode=resolved_config.runtime.mode,
            clock=resolved_clock,
        )

        logger.info(
            "application built environment=%s schema=%d manifest=%s policy=%d",
            resolved_config.app.environment,
            current_schema,
            manifest_record.manifest_id,
            policy.policy_version,
        )
        return cls(
            config=resolved_config,
            clock=resolved_clock,
            db=db,
            policy=policy,
            event_store=event_store,
            bus=bus,
            dispatcher=dispatcher,
            snapshots=snapshots,
            arbitrator=arbitrator,
            committer=committer,
            processor=processor,
            state=state_repo,
            runs=runs,
            deliveries=deliveries,
            failures=failures,
            llm_calls=llm_call_repo,
            prompts=prompts,
            llm=llm_client,
            structured=structured,
            manifest_id=manifest_record.manifest_id,
            schema_version=current_schema,
        )

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> Event:
        """Record readiness. Returns the SYSTEM_STARTED event."""
        if self.started:
            raise StartupError("application already started")
        event = Event.create(
            event_type="SYSTEM_STARTED",
            category="system",
            actor_type="system",
            source_type="bootstrap",
            origin="system",
            priority="P2",
            payload=SystemStartedPayload(
                manifest_id=self.manifest_id,
                schema_version_applied=self.schema_version,
                mode=self.config.runtime.mode,
            ),
            clock=self.clock,
        )
        await self.db.run(self.event_store.append, event)
        self.started = True

        # Spec 32: Ollama health belongs to the startup sequence. An unhealthy
        # model degrades the runtime; it does not corrupt state, so by default
        # it does not block readiness (spec 28.3).
        self.llm_healthy = await self.llm.health()
        if not self.llm_healthy:
            if self.config.llm.require_healthy_on_start:
                await self.stop("llm unavailable")
                raise StartupError(
                    f"model {self.config.llm.model!r} is not available at "
                    f"{self.config.llm.base_url}"
                )
            logger.warning(
                "model %s not reachable at %s; running degraded",
                self.config.llm.model,
                self.config.llm.base_url,
            )

        logger.info(
            "application ready mode=%s llm_healthy=%s",
            self.config.runtime.mode,
            self.llm_healthy,
        )
        return event

    async def stop(self, reason: str = "shutdown") -> None:
        if self.started:
            event = Event.create(
                event_type="SYSTEM_STOPPED",
                category="system",
                actor_type="system",
                source_type="bootstrap",
                origin="system",
                priority="P2",
                payload=SystemStoppedPayload(reason=reason),
                clock=self.clock,
            )
            await self.db.run(self.event_store.append, event)
            self.started = False
        await self.llm.aclose()
        self.db.close()
        logger.info("application stopped reason=%s", reason)

    async def __aenter__(self) -> Application:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()
