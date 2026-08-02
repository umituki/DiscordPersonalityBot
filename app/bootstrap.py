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
from app.conversation.engine import ConversationEngine
from app.conversation.guard import OutputGuard, OutputGuardPolicy
from app.conversation.policy import ConversationPolicy
from app.conversation.service import ConversationService
from app.interfaces.discord.adapter import DiscordMessageAdapter
from app.memory.engine import MemoryEngine
from app.memory.policy import MemoryPolicy
from app.memory.transcripts import ConversationTranscriptSource
from app.psychology.appraisal import AppraisalEngine
from app.psychology.emotion import EmotionEngine
from app.psychology.mood import MoodEngine
from app.psychology.needs import NeedEngine
from app.psychology.policy import PsychologyPolicy
from app.social.attachment import AttachmentEngine
from app.social.belief_policy import BeliefSelfPolicy
from app.social.beliefs import BeliefEngine
from app.social.self_model import SelfEngine
from app.social.policy import RelationshipPolicy
from app.social.relationship import RelationshipEngine
from app.social.user_model import SocialCognitionEngine
from app.llm.client import TracedLLMClient
from app.llm.ollama import OllamaClient
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.tracing import DatabaseTracer
from app.observability.logging import configure_logging
from app.resources.identity import Identity, load_identity
from app.orchestrator.processor import EventProcessor
from app.state.arbitrator import StateArbitrator
from app.state.committer import StateCommitter
from app.state.dependency_graph import validate_registry
from app.state.policy import ArbitrationPolicy
from app.state.snapshot import SnapshotService
from app.storage.database import Database
from app.storage.migrations import LATEST_VERSION, migrate, schema_version, verify_schema
from app.storage.repositories import (
    BeliefRepository,
    ConversationRepository,
    MemoryRepository,
    DeliveryRepository,
    EventRepository,
    FailureRepository,
    LLMCallRepository,
    ManifestRepository,
    ProcessingRunRepository,
    SelfRepository,
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
    conversations: ConversationRepository
    memories: MemoryRepository
    memory: MemoryEngine
    memory_policy: MemoryPolicy
    psychology_policy: PsychologyPolicy
    relationship_policy: RelationshipPolicy
    relationship: RelationshipEngine
    attachment: AttachmentEngine
    social_cognition: SocialCognitionEngine
    belief_self_policy: BeliefSelfPolicy
    beliefs: BeliefEngine
    self_model: SelfEngine
    appraisal: AppraisalEngine
    emotion: EmotionEngine
    mood: MoodEngine
    needs: NeedEngine
    identity: Identity
    conversation_policy: ConversationPolicy
    guard: OutputGuard
    conversation_engine: ConversationEngine
    #: ``None`` until the owner's Discord user id is configured.
    conversation: ConversationService | None
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
        conversation_repo = ConversationRepository(db)
        memory_repo = MemoryRepository(db)
        belief_repo = BeliefRepository(db)
        self_repo = SelfRepository(db)

        # --- crash recovery (spec 32) ---------------------------------------
        interrupted = runs.mark_interrupted(now=resolved_clock.now())
        if interrupted:
            logger.warning("recovered %d interrupted run(s) from a previous process", interrupted)

        # --- static identity and conversation policy (spec 1.3, 40) ----------
        identity = load_identity(resolved_config.character_dir)
        guard_policy = OutputGuardPolicy.load(resolved_config.output_guard_policy_path)
        conversation_policy = ConversationPolicy.load(resolved_config.conversation_policy_path)
        memory_policy = MemoryPolicy.load(resolved_config.memory_policy_path)
        psychology_policy = PsychologyPolicy.load(resolved_config.psychology_policy_path)
        relationship_policy = RelationshipPolicy.load(resolved_config.relationship_policy_path)
        belief_self_policy = BeliefSelfPolicy.load(resolved_config.belief_self_policy_path)

        # --- prompts (spec 38: versioned prompt files, never inline) ---------
        prompts = PromptRegistry.load(resolved_config.prompts_dir)

        # --- runtime manifest (spec 29) -------------------------------------
        components = base_components(
            policy_version=policy.policy_version, schema_version=current_schema
        )
        components["model_version"] = resolved_config.llm.model
        components["llm_provider"] = resolved_config.llm.provider
        components["identity_version"] = identity.version_tag()
        components["output_guard_policy_version"] = str(guard_policy.policy_version)
        components["conversation_policy_version"] = str(conversation_policy.policy_version)
        components["memory_policy_version"] = str(memory_policy.policy_version)
        components["psychology_policy_version"] = str(psychology_policy.policy_version)
        components["relationship_policy_version"] = str(relationship_policy.policy_version)
        components["belief_self_policy_version"] = str(belief_self_policy.policy_version)
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

        guard = OutputGuard(guard_policy)
        conversation_engine = ConversationEngine(
            identity=identity,
            prompts=prompts,
            structured=structured,
            guard=guard,
            policy=conversation_policy,
            clock=resolved_clock,
        )

        memory_engine = MemoryEngine(
            repository=memory_repo,
            policy=memory_policy,
            structured=structured,
            prompts=prompts,
            transcripts=ConversationTranscriptSource(
                conversation_repo, yui_label=identity.name
            ),
            clock=resolved_clock,
        )

        # --- immediate psychology (spec 9.5 phases 1-2) ----------------------
        appraisal_engine = AppraisalEngine(
            identity=identity,
            prompts=prompts,
            structured=structured,
            policy=psychology_policy.appraisal,
            clock=resolved_clock,
        )
        emotion_engine = EmotionEngine(psychology_policy.emotion, clock=resolved_clock)
        mood_engine = MoodEngine(psychology_policy.mood, clock=resolved_clock)
        need_engine = NeedEngine(psychology_policy.needs, clock=resolved_clock)

        # --- adaptive social state (spec 9.5 phase 6) ------------------------
        relationship_engine = RelationshipEngine(relationship_policy, clock=resolved_clock)
        attachment_engine = AttachmentEngine(
            relationship_policy.attachment, clock=resolved_clock
        )
        social_cognition_engine = SocialCognitionEngine(
            relationship_policy.user_model, clock=resolved_clock
        )
        # Beliefs and the self model are evidence-driven services rather than
        # per-event subscribers: they change when evidence arrives, which the
        # knowledge and consolidation phases will supply (spec 12.4, 25).
        belief_engine = BeliefEngine(belief_repo, belief_self_policy.belief, clock=resolved_clock)
        self_engine = SelfEngine(self_repo, belief_self_policy.self_schema, clock=resolved_clock)

        # Update order follows the layers: immediate psychology first, then the
        # adaptive layer that reads it (spec 9.1, 9.5).
        bus.register(emotion_engine, kind="psychology", order=20)
        bus.register(mood_engine, kind="psychology", order=30)
        bus.register(need_engine, kind="psychology", order=40)
        bus.register(relationship_engine, kind="psychology", order=50)
        bus.register(attachment_engine, kind="psychology", order=60)
        bus.register(social_cognition_engine, kind="psychology", order=70)

        processor = EventProcessor(
            db=db,
            event_store=event_store,
            dispatcher=dispatcher,
            snapshots=snapshots,
            arbitrator=arbitrator,
            committer=committer,
            runs=runs,
            failures=failures,
            interpreter=appraisal_engine,
            manifest_id=manifest_record.manifest_id,
            mode=resolved_config.runtime.mode,
            clock=resolved_clock,
        )

        # The conversation path exists only when the single USER is identified
        # (spec 1.2). Without it, YUI has no one to talk to and stays offline.
        conversation: ConversationService | None = None
        owner_user_id = resolved_config.secrets.discord_owner_user_id
        if owner_user_id:
            channel_id = resolved_config.secrets.discord_channel_id
            conversation = ConversationService(
                processor=processor,
                engine=conversation_engine,
                conversations=conversation_repo,
                adapter=DiscordMessageAdapter(
                    owner_user_id=owner_user_id,
                    allowed_channel_ids=frozenset({channel_id} if channel_id else set()),
                    clock=resolved_clock,
                ),
                failures=failures,
                policy=conversation_policy,
                memory=memory_engine,
                appraisal=appraisal_engine,
                clock=resolved_clock,
            )
        else:
            logger.warning(
                "DISCORD_OWNER_USER_ID is not set; the conversation interface is disabled"
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
            conversations=conversation_repo,
            memories=memory_repo,
            memory=memory_engine,
            memory_policy=memory_policy,
            psychology_policy=psychology_policy,
            relationship_policy=relationship_policy,
            relationship=relationship_engine,
            attachment=attachment_engine,
            social_cognition=social_cognition_engine,
            belief_self_policy=belief_self_policy,
            beliefs=belief_engine,
            self_model=self_engine,
            appraisal=appraisal_engine,
            emotion=emotion_engine,
            mood=mood_engine,
            needs=need_engine,
            identity=identity,
            conversation_policy=conversation_policy,
            guard=guard,
            conversation_engine=conversation_engine,
            conversation=conversation,
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
