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

from app.admin.control_plane import AdminControlPlane
from app.admin.queries import DebugQueryService, DebugSources
from app.admin.router import AdminRouter
from app.admin.legacy import LegacyHealthScanner
from app.admin.rebuild import RebuildService
from app.admin.repair import RepairService
from app.agency.decision import DecisionEngine
from app.agency.goals import GoalEngine
from app.agency.habits import HabitEngine
from app.agency.policy import AgencyPolicy
from app.clock import Clock, SystemClock
from app.config import AppConfig, load_config
from app.consolidation.adaptations import AdaptationEngine
from app.consolidation.drift import DriftMonitor
from app.consolidation.growth import GrowthEngine
from app.consolidation.job import ConsolidationJob
from app.consolidation.policy import GrowthPolicy
from app.consolidation.values import ValueEngine
from app.events.bus import EventBus
from app.events.dispatcher import EventDispatcher
from app.events.model import Event, SystemStartedPayload, SystemStoppedPayload
from app.epistemics.actions import EpistemicActionSelector
from app.knowledge.builder import KnowledgeBuilder
from app.knowledge.coverage import CoveragePlanner
from app.firstboot.orchestrator import FirstBootOrchestrator
from app.genesis.critics import CriticBoard
from app.genesis.runner import GenesisRunner
from app.knowledge.investigation import InvestigationService
from app.knowledge.providers import BundleProvider, ProviderRegistry
from app.knowledge.search import FixtureSearchProvider
from app.knowledge.policy import KnowledgePolicy
from app.knowledge.service import KnowledgeService
from app.simulation.engine import PastSimulationEngine
from app.simulation.genesis import GenesisService
from app.simulation.policy import SimulationPolicy
from app.simulation.seed import SeedBuilder
from app.jobs.proactive import ProactiveEngine
from app.jobs.scheduler import Scheduler
from app.runtime.autonomous import AutonomousRuntime
from app.runtime.agency import AgencyActions, AgencyCandidates, GoalSource, HabitSource
from app.diary.service import DiaryContextBuilder, DiaryService
from app.runtime.knowledge import GapSource, KnowledgeActions, KnowledgeCandidates
from app.runtime.life import ActivitySource, LifeActions, SleepSource
from app.runtime.proactive import (
    ProactiveActions,
    ProactiveDeliberation,
    ProactiveSource,
)
from app.live.readiness import LiveReadiness
from app.runtime.shadow import ShadowController
from app.runtime.social import GroupSource, NPCSource, SocialActions, SocialCandidates
from app.runtime.spontaneous_memory import (
    SpontaneousMemoryActions,
    SpontaneousMemoryCandidates,
    SpontaneousMemorySource,
)
from app.runtime.sources import Registry as RuntimeRegistry, SchedulerSource
from app.events.store import EventStore
from app.consolidation.events import DEEP_CONSOLIDATION_REVIEW
from app.conversation.engine import ConversationEngine
from app.conversation.common_ground import CommonGroundTracker
from app.conversation.references import FixtureReferenceProvider, NullReferenceProvider
from app.conversation.repetition import SurfaceRepetitionMonitor
from app.conversation.social_interpretation import SocialInterpreter
from app.conversation.surface import SurfacePlanner
from app.conversation.guard import OutputGuard, OutputGuardPolicy
from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
from app.grounding.context import GroundingContextBuilder
from app.grounding.memory_semantics import SemanticMemoryGroundingGuard
from app.grounding.policy import GroundingPolicy
from app.conversation.policy import ConversationPolicy
from app.conversation.service import ConversationService
from app.interfaces.discord.adapter import DiscordMessageAdapter
from app.memory.engine import MemoryEngine
from app.memory.inspector import MemoryInspector
from app.memory.policy import MemoryPolicy
from app.memory.material import (
    CompositeMaterialSource,
    ConversationEpisodeSource,
    SimulationEpisodeSource,
    VirtualLifeEpisodeSource,
)
from app.psychology.appraisal import AppraisalEngine
from app.psychology.emotion import EmotionEngine
from app.psychology.mood import MoodEngine
from app.psychology.needs import NeedEngine
from app.psychology.policy import PsychologyPolicy
from app.social.attachment import AttachmentEngine
from app.social.belief_policy import BeliefSelfPolicy
from app.social.beliefs import BeliefEngine
from app.social.self_model import SelfEngine
from app.tools.builtin import register_builtin_tools, register_search_provider
from app.tools.manager import ToolManager, ToolRegistry
from app.world.policy import WorldPolicy
from app.world.service import WorldService
from app.social.policy import RelationshipPolicy
from app.social.relationship import RelationshipEngine
from app.social.user_model import SocialCognitionEngine
from app.society.groups import GroupEngine
from app.society.policy import SocietyPolicy
from app.society.relationships import NPCRelationshipEngine
from app.society.service import SocietyService
from app.llm.client import TracedLLMClient
from app.llm.client import PassThroughLimiter
from app.llm.ollama import OllamaClient
from app.llm.policy import LLMPolicy
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.tracing import DatabaseTracer
from app.observability.logging import configure_logging
from app.observability.trace import ConversationTracer
from app.reliability.resources import ResourceManager
from app.storage.backup import BackupService, looks_cloud_synced
from app.versioning.capabilities import load_contracts
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
    AcquisitionRepository,
    ActivityRepository,
    AdminActionRepository,
    BackupRepository,
    AdaptationRepository,
    BeliefRepository,
    CandidateRepository,
    ConsolidationRepository,
    CoverageJobRepository,
    DecisionRepository,
    DiaryRepository,
    ConversationRepository,
    ConversationTraceRepository,
    CommonGroundRepository,
    RebuildEpochRepository,
    DriftRepository,
    MemoryAdminRepository,
    MemoryRepository,
    DeliveryRepository,
    EventRepository,
    ExposureRepository,
    FailureRepository,
    FirstBootRepository,
    GenerationAuditRepository,
    GenesisExperienceRepository,
    GenesisRunRepository,
    GoalRepository,
    GroupRepository,
    HealthRepository,
    JobRepository,
    KnowledgeGapRepository,
    KnowledgeRepository,
    LifeDayRepository,
    LifeEntityRepository,
    LifeRecordRepository,
    HabitRepository,
    LLMCallRepository,
    ManifestRepository,
    NPCInteractionRepository,
    NPCModelRepository,
    NPCRelationshipRepository,
    NPCRepository,
    NarrativeRepository,
    PersonalityRepository,
    PlanRepository,
    ProactiveRepository,
    ProactiveDeliberationRepository,
    ProcessingRunRepository,
    RuntimeTickRepository,
    SpontaneousMemoryCueRepository,
    SearchCallRepository,
    ShadowDecisionRepository,
    SelfRepository,
    SimulationRepository,
    SleepRepository,
    SnapshotRepository,
    SocialLinkRepository,
    StateRepository,
    ToolCallRepository,
    ValueRepository,
    WorldHistoryRepository,
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
    tracer: ConversationTracer
    memories: MemoryRepository
    memory: MemoryEngine
    memory_inspector: MemoryInspector
    admin_router: AdminRouter
    memory_policy: MemoryPolicy
    psychology_policy: PsychologyPolicy
    relationship_policy: RelationshipPolicy
    relationship: RelationshipEngine
    attachment: AttachmentEngine
    social_cognition: SocialCognitionEngine
    belief_self_policy: BeliefSelfPolicy
    beliefs: BeliefEngine
    self_model: SelfEngine
    tools: ToolManager
    agency_policy: AgencyPolicy
    goals: GoalEngine
    habits: HabitEngine
    decisions: DecisionEngine
    epistemics: EpistemicActionSelector
    world_policy: WorldPolicy
    world: WorldService
    scheduler: Scheduler
    jobs: JobRepository
    proactive: ProactiveEngine
    runtime: AutonomousRuntime
    runtime_ticks: RuntimeTickRepository
    spontaneous_memory_cues: SpontaneousMemoryCueRepository
    diary: DiaryService
    investigation: InvestigationService
    genesis_runner: GenesisRunner
    first_boot: FirstBootOrchestrator
    live: LiveReadiness
    first_boot_state: FirstBootRepository
    genesis_runs: GenesisRunRepository
    genesis_experiences: GenesisExperienceRepository
    life_records: LifeRecordRepository
    life_entities: LifeEntityRepository
    generation_audits: GenerationAuditRepository
    gaps: KnowledgeGapRepository
    knowledge_repo: KnowledgeRepository
    searches: SearchCallRepository
    diaries: DiaryRepository
    life_days: LifeDayRepository
    proactive_deliberation: ProactiveDeliberation
    proactive_deliberations: ProactiveDeliberationRepository
    shadow: ShadowController
    shadow_decisions: ShadowDecisionRepository
    growth_policy: GrowthPolicy
    adaptations: AdaptationEngine
    growth: GrowthEngine
    values: ValueEngine
    drift: DriftMonitor
    consolidation: ConsolidationJob
    llm_policy: LLMPolicy
    knowledge_policy: KnowledgePolicy
    knowledge_builder: KnowledgeBuilder
    knowledge_providers: ProviderRegistry
    coverage: CoveragePlanner
    knowledge: KnowledgeService
    simulation_policy: SimulationPolicy
    seed_builder: SeedBuilder
    simulation: PastSimulationEngine
    genesis: GenesisService
    resources: ResourceManager
    backups: BackupService
    admin: AdminControlPlane
    legacy: LegacyHealthScanner
    repair: RepairService
    rebuild: RebuildService
    society_policy: SocietyPolicy
    society: SocietyService
    npc_relationships: NPCRelationshipEngine
    groups: GroupEngine
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
    #: Phase 13. What the FIRST BOOT Authority said at startup, and whether the
    #: character plane is therefore open. Read, never decided, here.
    first_boot_status: str = "PENDING"
    character_plane: bool = False
    #: Set by :meth:`start` from the spec 32 startup sequence.
    catch_up: object | None = None
    retired_jobs: int = 0

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
        common_ground_repo = CommonGroundRepository(db)
        memory_repo = MemoryRepository(db)
        belief_repo = BeliefRepository(db)
        self_repo = SelfRepository(db)
        tool_call_repo = ToolCallRepository(db)
        goal_repo = GoalRepository(db)
        plan_repo = PlanRepository(db)
        habit_repo = HabitRepository(db)
        decision_repo = DecisionRepository(db)
        activity_repo = ActivityRepository(db)
        sleep_repo = SleepRepository(db)
        world_history_repo = WorldHistoryRepository(db)
        job_repo = JobRepository(db)
        proactive_repo = ProactiveRepository(db)
        adaptation_repo = AdaptationRepository(db)
        trait_repo = PersonalityRepository(db)
        value_repo = ValueRepository(db)
        candidate_repo = CandidateRepository(db)
        narrative_repo = NarrativeRepository(db)
        drift_repo = DriftRepository(db)
        consolidation_repo = ConsolidationRepository(db)
        npc_repo = NPCRepository(db)
        npc_model_repo = NPCModelRepository(db)
        npc_relationship_repo = NPCRelationshipRepository(db)
        group_repo = GroupRepository(db)
        social_link_repo = SocialLinkRepository(db)
        npc_interaction_repo = NPCInteractionRepository(db)
        knowledge_repo = KnowledgeRepository(db)
        exposure_repo = ExposureRepository(db)
        acquisition_repo = AcquisitionRepository(db)
        coverage_job_repo = CoverageJobRepository(db)
        simulation_repo = SimulationRepository(db)
        health_repo = HealthRepository(db)
        # Patch spec 19.2: one row per turn, so the USER's wait can be read
        # stage by stage instead of guessed at.
        trace_repo = ConversationTraceRepository(db)
        conversation_tracer = ConversationTracer(
            trace_repo, clock=resolved_clock
        )

        # --- crash recovery (spec 32) ---------------------------------------
        interrupted = runs.mark_interrupted(now=resolved_clock.now())
        if interrupted:
            logger.warning("recovered %d interrupted run(s) from a previous process", interrupted)

        # --- static identity and conversation policy (spec 1.3, 40) ----------
        identity = load_identity(resolved_config.character_dir)
        guard_policy = OutputGuardPolicy.load(resolved_config.output_guard_policy_path)
        grounding_policy = GroundingPolicy.load(resolved_config.grounding_policy_path)
        conversation_policy = ConversationPolicy.load(resolved_config.conversation_policy_path)
        memory_policy = MemoryPolicy.load(resolved_config.memory_policy_path)
        psychology_policy = PsychologyPolicy.load(resolved_config.psychology_policy_path)
        relationship_policy = RelationshipPolicy.load(resolved_config.relationship_policy_path)
        belief_self_policy = BeliefSelfPolicy.load(resolved_config.belief_self_policy_path)
        agency_policy = AgencyPolicy.load(resolved_config.agency_policy_path)
        world_policy = WorldPolicy.load(resolved_config.world_policy_path)
        growth_policy = GrowthPolicy.load(resolved_config.growth_policy_path)
        society_policy = SocietyPolicy.load(resolved_config.society_policy_path)
        knowledge_policy = KnowledgePolicy.load(resolved_config.knowledge_policy_path)
        llm_policy = LLMPolicy.load(resolved_config.llm_policy_path)
        simulation_policy = SimulationPolicy.load(resolved_config.simulation_policy_path)

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
        components["grounding_policy_version"] = str(grounding_policy.policy_version)
        components["conversation_policy_version"] = str(conversation_policy.policy_version)
        components["memory_policy_version"] = str(memory_policy.policy_version)
        components["psychology_policy_version"] = str(psychology_policy.policy_version)
        components["relationship_policy_version"] = str(relationship_policy.policy_version)
        components["belief_self_policy_version"] = str(belief_self_policy.policy_version)
        components["agency_policy_version"] = str(agency_policy.policy_version)
        components["world_policy_version"] = str(world_policy.policy_version)
        components["growth_policy_version"] = str(growth_policy.policy_version)
        components["society_policy_version"] = str(society_policy.policy_version)
        components["knowledge_policy_version"] = str(knowledge_policy.policy_version)
        components["llm_policy_version"] = str(llm_policy.policy_version)
        components["simulation_policy_version"] = str(simulation_policy.policy_version)
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
        # Patch spec 4: the ResourceManager is the single scheduler for model
        # slots, so it is built before the client that will defer to it.
        resources = ResourceManager(concurrency=resolved_config.llm.concurrency)
        ollama = OllamaClient(
            base_url=resolved_config.llm.base_url,
            model=resolved_config.llm.model,
            request_timeout_s=resolved_config.llm.request_timeout_s,
            connect_timeout_s=resolved_config.llm.connect_timeout_s,
            concurrency=resolved_config.llm.concurrency,
            num_ctx=resolved_config.llm.num_ctx,
            temperature=resolved_config.llm.temperature,
            keep_alive=resolved_config.llm.keep_alive,
            limiter=PassThroughLimiter(),
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
            policy=llm_policy,
            resources=resources,
        )

        guard = OutputGuard(guard_policy)
        # Rebuild spec 15. The guard resolves claims; the builder assembles what
        # they are resolved against. Both are wired here so the reply path has
        # no way to run without them.
        claim_extractor = ClaimExtractor(grounding_policy)
        claim_guard = ClaimGroundingGuard(claim_extractor)
        # Rebuild spec 13.2, Phase 3 §27-§28. A corpus is optional; a broken
        # one must not stop her speaking, so a failed load degrades to no
        # references rather than to no startup.
        references = NullReferenceProvider()
        corpus_path = resolved_config.references_dir / "fixture_ja.yaml"
        if corpus_path.is_file():
            try:
                references = FixtureReferenceProvider.load(corpus_path)
            except Exception:  # noqa: BLE001 - references are never load-bearing
                logger.exception("dialogue reference corpus failed to load")
        components["dialogue_reference_source"] = references.provenance.source_name

        # Rebuild spec 47, Phase 14. One controller, four capabilities: the
        # mode for each of them is read once, here, so that "is proactive
        # contact live" cannot have two answers in one process.
        shadow_decisions = ShadowDecisionRepository(db)
        shadow = ShadowController(
            modes={
                "proactive_contact": resolved_config.runtime.proactive_mode,
                "intentional_silence": resolved_config.runtime.silence_mode,
                "npc_contact": resolved_config.runtime.npc_contact_mode,
                "web_search": resolved_config.runtime.search_mode,
            },
            repository=shadow_decisions,
            clock=resolved_clock,
        )
        conversation_engine = ConversationEngine(
            identity=identity,
            prompts=prompts,
            structured=structured,
            guard=guard,
            policy=conversation_policy,
            grounding=claim_guard,
            memory_grounding=SemanticMemoryGroundingGuard(
                structured=structured,
                prompts=prompts,
            ),
            interpreter=SocialInterpreter(
                identity=identity, prompts=prompts, structured=structured
            ),
            planner=SurfacePlanner(),
            references=references,
            repetition=SurfaceRepetitionMonitor(),
            shadow=shadow,
            clock=resolved_clock,
        )

        memory_engine = MemoryEngine(
            repository=memory_repo,
            policy=memory_policy,
            structured=structured,
            prompts=prompts,
            # Patch spec 15.2: one source per kind of life. Without the
            # simulated one, every simulated episode read as empty and was
            # discarded, so nineteen years produced no memories at all.
            material=CompositeMaterialSource(
                (
                    ConversationEpisodeSource(conversation_repo, yui_label=identity.name),
                    SimulationEpisodeSource(event_store),
                    VirtualLifeEpisodeSource(event_store),
                )
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
        # --- tools (spec 26): the only authority on what actually ran --------
        tool_registry = ToolRegistry()
        register_builtin_tools(tool_registry, clock=resolved_clock)
        tool_manager = ToolManager(tool_registry, tool_call_repo, clock=resolved_clock)

        # --- virtual life (spec 18, 19) --------------------------------------
        world_service = WorldService(
            activities=activity_repo,
            sleeps=sleep_repo,
            history=world_history_repo,
            policy=world_policy,
            clock=resolved_clock,
        )
        scheduler = Scheduler(job_repo, world_policy.scheduler, clock=resolved_clock)
        proactive_engine = ProactiveEngine(
            proactive_repo, world_policy.proactive, clock=resolved_clock
        )

        # --- agency (spec 15, 17) --------------------------------------------
        goal_engine = GoalEngine(goal_repo, plan_repo, agency_policy.goals, clock=resolved_clock)
        habit_engine = HabitEngine(habit_repo, agency_policy.habits, clock=resolved_clock)
        decision_engine = DecisionEngine(
            decision_repo, agency_policy.decision, clock=resolved_clock
        )
        epistemic_selector = EpistemicActionSelector(
            agency_policy.epistemic, clock=resolved_clock
        )

        # --- autonomous runtime (spec 21, 22) --------------------------------
        # The loop itself. Phase 7 registers activity and sleep into the same
        # registry just below, once the processor exists; Phases 8-11 add goals,
        # habits, NPCs, knowledge and proactive contact. None of them edits the
        # loop.
        runtime_tick_repo = RuntimeTickRepository(db)
        spontaneous_memory_cues = SpontaneousMemoryCueRepository(db)
        runtime_registry = RuntimeRegistry()
        runtime_registry.add_source(SchedulerSource(scheduler))
        autonomous_runtime = AutonomousRuntime(
            runtime_registry,
            decisions=decision_engine,
            ticks=runtime_tick_repo,
            clock=resolved_clock,
            idle_seconds=world_policy.scheduler.idle_wake_seconds,
        )

        belief_engine = BeliefEngine(belief_repo, belief_self_policy.belief, clock=resolved_clock)
        self_engine = SelfEngine(self_repo, belief_self_policy.self_schema, clock=resolved_clock)

        # --- growth (spec 12, 23): deep state, consolidated separately -------
        adaptation_engine = AdaptationEngine(
            adaptation_repo, growth_policy.adaptation, clock=resolved_clock
        )
        growth_engine = GrowthEngine(
            candidates=candidate_repo,
            traits=trait_repo,
            narratives=narrative_repo,
            policy=growth_policy,
            clock=resolved_clock,
        )
        value_engine = ValueEngine(
            candidates=candidate_repo,
            values=value_repo,
            policy=growth_policy,
            clock=resolved_clock,
        )
        drift_monitor = DriftMonitor(
            state=state_repo,
            drifts=drift_repo,
            policy=growth_policy.drift,
            clock=resolved_clock,
        )

        # --- virtual society (spec 20) ---------------------------------------
        # The objective record of who exists is separate from YUI's
        # relationships with them, which are separate again from the USER
        # relationship above. Three writers, three domains, no overlap.
        society_service = SocietyService(
            npcs=npc_repo,
            relationships=npc_relationship_repo,
            groups=group_repo,
            links=social_link_repo,
            interactions=npc_interaction_repo,
            policy=society_policy,
            clock=resolved_clock,
        )
        npc_relationship_engine = NPCRelationshipEngine(
            npcs=npc_repo,
            relationships=npc_relationship_repo,
            models=npc_model_repo,
            policy=society_policy,
            clock=resolved_clock,
        )
        group_engine = GroupEngine(group_repo, society_policy, clock=resolved_clock)

        # --- historical knowledge (spec 21) ----------------------------------
        # The builder records what the world knew and when; the service is the
        # only thing that may say YUI knows it, and only from an acquisition.
        knowledge_builder = KnowledgeBuilder(
            knowledge=knowledge_repo,
            jobs=coverage_job_repo,
            policy=knowledge_policy,
            clock=resolved_clock,
        )
        knowledge_service = KnowledgeService(
            knowledge=knowledge_repo,
            exposures=exposure_repo,
            acquisitions=acquisition_repo,
            policy=knowledge_policy,
            memory=memory_engine,
            clock=resolved_clock,
        )
        # Patch spec 16.2: the authority is an external source, never what a
        # model happens to know. Bundles the owner curated are the source that
        # ships; a lookup tool can be registered here too, and its results go
        # through the Tool Manager so a claim has provenance.
        knowledge_providers = ProviderRegistry(builder=knowledge_builder)
        knowledge_providers.register(BundleProvider(config.knowledge_dir))
        # Patch spec 16.3: coverage is planned per period per class. A window
        # nothing was found for is recorded as a gap, not passed silently.
        coverage_planner = CoveragePlanner(
            builder=knowledge_builder,
            providers=knowledge_providers,
            clock=resolved_clock,
        )

        # Update order follows the layers: immediate psychology first, then the
        # adaptive layer that reads it (spec 9.1, 9.5).
        bus.register(emotion_engine, kind="psychology", order=20)
        bus.register(mood_engine, kind="psychology", order=30)
        bus.register(need_engine, kind="psychology", order=40)
        bus.register(relationship_engine, kind="psychology", order=50)
        bus.register(attachment_engine, kind="psychology", order=60)
        bus.register(social_cognition_engine, kind="psychology", order=70)
        # Spec 20: NPC state reacts to NPC events only. The engine filters on
        # actor type as well, so neither path can reach the other's domain.
        bus.register(npc_relationship_engine, kind="psychology", order=72)
        bus.register(group_engine, kind="psychology", order=74)
        # The world is Layer 0 and is refreshed before anything interprets it.
        bus.register(world_service, kind="system", order=10)
        # Deep state is reviewed only by the consolidation event, never by an
        # ordinary one (spec 9.5, 12.3). The event type filter is that rule.
        bus.register(
            adaptation_engine,
            kind="system",
            event_types=(DEEP_CONSOLIDATION_REVIEW,),
            order=80,
        )
        bus.register(
            growth_engine, kind="system", event_types=(DEEP_CONSOLIDATION_REVIEW,), order=90
        )
        bus.register(
            value_engine, kind="system", event_types=(DEEP_CONSOLIDATION_REVIEW,), order=91
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
            interpreter=appraisal_engine,
            manifest_id=manifest_record.manifest_id,
            mode=resolved_config.runtime.mode,
            clock=resolved_clock,
        )

        # --- life, driven by the loop (spec 24, 25 — Phase 7) ----------------
        # The first real builders and handlers. Registered here rather than in
        # `app/runtime/` because this is where the processor exists: an
        # autonomous act goes through the ordinary pipeline like any other.
        sleep_source = SleepSource(
            world_service, state_repo, policy=world_policy.sleep, clock=resolved_clock
        )
        life_actions = LifeActions(
            world_service,
            processor=processor,
            state=state_repo,
            policy=world_policy.sleep,
            source=sleep_source,
            clock=resolved_clock,
        )
        life_actions.register(
            runtime_registry,
            sources=(sleep_source, ActivitySource(world_service, clock=resolved_clock)),
        )

        # --- associative memory, driven by durable world cues (spec 17.6) ---
        # Collecting a cue may only claim its offer window. Retrieval and
        # practice stay behind the Decision Engine and the selected handler.
        spontaneous_source = SpontaneousMemorySource(
            cues=spontaneous_memory_cues,
            activities=activity_repo,
            interactions=npc_interaction_repo,
            world=world_service,
            clock=resolved_clock,
        )
        spontaneous_candidates = SpontaneousMemoryCandidates(
            cues=spontaneous_memory_cues,
            world=world_service,
            state=state_repo,
        )
        SpontaneousMemoryActions(
            cues=spontaneous_memory_cues,
            memory=memory_engine,
            processor=processor,
            clock=resolved_clock,
        ).register(
            runtime_registry,
            source=spontaneous_source,
            candidates=spontaneous_candidates,
        )

        # --- goals, habits, NPCs and groups (spec 29-31 — Phase 8) -----------
        # §31: 既存 engine を Runtime に接続する. GoalEngine, HabitEngine,
        # SocietyService and GroupEngine have existed and been unit-tested for
        # a long time; this is the wiring that makes anything drive them.
        agency_candidates = AgencyCandidates(
            goal_engine, habit_engine, policy=agency_policy
        )
        AgencyActions(
            goals=goal_engine,
            habits=habit_engine,
            world=world_service,
            processor=processor,
            candidates=agency_candidates,
            clock=resolved_clock,
        ).register(
            runtime_registry,
            sources=(
                GoalSource(goal_engine, world_service, clock=resolved_clock),
                HabitSource(
                    habit_engine,
                    world_service,
                    policy=agency_policy.habits,
                    clock=resolved_clock,
                ),
            ),
        )
        SocialActions(
            society=society_service,
            world=world_service,
            processor=processor,
            candidates=SocialCandidates(society_service, state_repo),
            shadow=shadow,
            clock=resolved_clock,
        ).register(
            runtime_registry,
            sources=(
                NPCSource(society_service, world_service, clock=resolved_clock),
                GroupSource(society_service, world_service, clock=resolved_clock),
            ),
        )

        # Where an unprompted message would go, and who may drive the admin
        # plane. Read once, here, because Phase 9's proactive chain needs the
        # channel and Phase 5's router needs the owner.

        # Where an unprompted message would go, and who may drive the admin
        # plane. Read once, here, because Phase 9's proactive chain needs the
        # channel and Phase 5's router needs the owner.
        owner_user_id = resolved_config.secrets.discord_owner_user_id
        channel_id = resolved_config.secrets.discord_channel_id

        # --- Genesis v2 (spec 34, 35 — Phase 12) -----------------------------
        # Built here and driven by the CLI, not by startup: Genesis is a
        # once-ever FIRST BOOT process, and a bootstrap that could start one is
        # a bootstrap that can start one by accident.
        genesis_run_repo = GenesisRunRepository(db)
        life_record_repo = LifeRecordRepository(db)
        life_entity_repo = LifeEntityRepository(db)
        generation_audit_repo = GenerationAuditRepository(db)
        genesis_experience_repo = GenesisExperienceRepository(db)
        genesis_runner = GenesisRunner(
            runs=genesis_run_repo,
            records=life_record_repo,
            entities=life_entity_repo,
            audits=generation_audit_repo,
            experiences=genesis_experience_repo,
            critics=CriticBoard(
                audits=generation_audit_repo,
                structured=structured,
                prompts=prompts,
                clock=resolved_clock,
            ),
            structured=structured,
            prompts=prompts,
            processor=processor,
            memory=memory_repo,
            memory_engine=memory_engine,
            society=society_service,
            knowledge_repo=knowledge_repo,
            event_store=event_store,
            clock=resolved_clock,
        )

        # --- search and knowledge (spec 17, 32 — Phase 11) -------------------
        # The formal entrance for information from outside. The provider is
        # reached through the tool, never beside it, so ToolManager stays the
        # only authority on whether a search actually ran (32.2).
        gap_repo = KnowledgeGapRepository(db)
        search_repo = SearchCallRepository(db)
        search_provider = FixtureSearchProvider.load(
            resolved_config.knowledge_dir / "search_fixture.yaml"
        )
        register_search_provider(tool_registry, search_provider, clock=resolved_clock)
        investigation = InvestigationService(
            selector=epistemic_selector,
            provider=search_provider,
            tools=tool_manager,
            knowledge=knowledge_service,
            knowledge_repo=knowledge_repo,
            searches=search_repo,
            gaps=gap_repo,
            beliefs=belief_engine,
            processor=processor,
            clock=resolved_clock,
        )
        KnowledgeActions(
            investigation,
            gap_repo,
            candidates=KnowledgeCandidates(gap_repo, investigation, clock=resolved_clock),
            shadow=shadow,
            clock=resolved_clock,
        ).register(
            runtime_registry,
            sources=(GapSource(gap_repo, world_service, clock=resolved_clock),),
        )

        # --- diary (spec 26 — Phase 10) --------------------------------------
        # 26.3's constraint shapes the wiring: the service is handed to the
        # sleep action, which awaits it and then goes to sleep regardless.
        life_day_repo = LifeDayRepository(db)
        diary_repo = DiaryRepository(db)
        diary_service = DiaryService(
            days=life_day_repo,
            diaries=diary_repo,
            builder=DiaryContextBuilder(
                world=world_service,
                conversations=conversation_repo,
                society=society_service,
                memories=memory_repo,
                goals=goal_engine,
                state=state_repo,
                clock=resolved_clock,
            ),
            processor=processor,
            structured=structured,
            prompts=prompts,
            memory=memory_engine,
            clock=resolved_clock,
        )
        life_actions.attach_diary(diary_service)

        # --- proactive contact (spec 28 — Phase 9) ---------------------------
        # SHADOW by default (28.4: 初期運用は SHADOW). The sender is the null one
        # unless the mode is LIVE, so shadow is not "a real sender we remember
        # not to call" — there is nothing there to call.
        proactive_deliberations = ProactiveDeliberationRepository(db)
        proactive_source = ProactiveSource(
            event_store,
            state_repo,
            conversation_repo,
            world=world_service,
            clock=resolved_clock,
        )
        proactive_deliberation = ProactiveDeliberation(
            engine=proactive_engine,
            source=proactive_source,
            deliberations=proactive_deliberations,
            shadow=shadow,
            structured=structured,
            prompts=prompts,
            guard=guard,
            sender=None,
            processor=processor,
            channel_id=channel_id or "",
            clock=resolved_clock,
        )
        ProactiveActions(
            proactive_deliberation,
            state=state_repo,
            engine=proactive_engine,
            clock=resolved_clock,
        ).register(runtime_registry, sources=(proactive_source,))

        consolidation_job = ConsolidationJob(
            processor=processor,
            event_store=event_store,
            state=state_repo,
            consolidations=consolidation_repo,
            candidates=candidate_repo,
            narratives=narrative_repo,
            memories=memory_repo,
            adaptations=adaptation_engine,
            growth=growth_engine,
            values=value_engine,
            drift=drift_monitor,
            memory=memory_engine,
            policy=growth_policy,
            mood_baseline_valence=psychology_policy.mood.baseline_valence,
            clock=resolved_clock,
        )

        # --- genesis (spec 22) -----------------------------------------------
        # The past simulation reuses this processor, so a simulated experience
        # goes through the same appraisal, engines, arbitrator and transaction
        # as a real one. There is no second personality engine.
        seed_builder = SeedBuilder(simulation_policy.seed, clock=resolved_clock)
        simulation_engine = PastSimulationEngine(
            processor=processor,
            repository=simulation_repo,
            knowledge=knowledge_service,
            growth=growth_engine,
            structured=structured,
            prompts=prompts,
            policy=simulation_policy,
            # Patch spec 14: consolidation runs *during* the simulated life,
            # not once at the end, so the Deep Gate sees several separated
            # evidence windows instead of one.
            consolidation=consolidation_job,
            # Patch spec 15.1: experiences reach the ordinary memory pipeline
            # — segmentation, the Encoding Gate, forgetting — and are never
            # written into the memory table directly.
            memory=memory_engine,
            coverage=coverage_planner,
            clock=resolved_clock,
        )
        genesis_service = GenesisService(
            repository=simulation_repo,
            events=events_repo,
            event_store=event_store,
            consolidation=consolidation_job,
            knowledge=knowledge_service,
            growth=growth_engine,
            drift=drift_monitor,
            policy=simulation_policy.first_boot,
            # Patch spec 17: the audits read committed rows, not what the run
            # reported about itself.
            health=health_repo,
            clock=resolved_clock,
        )

        # --- operations (spec 30, 32, 33) -------------------------------------
        # Rebuild spec 30, Phase 5. The debug plane reads repositories and the
        # memory inspector, and holds no engine that could commit, encode,
        # practise or send. It is deliberately built from read APIs only.
        memory_inspector = MemoryInspector(memory_engine.retriever, clock=resolved_clock)

        admin_action_repo = AdminActionRepository(db)
        backup_service = BackupService(
            db, backups_dir=resolved_config.backups_dir, clock=resolved_clock
        )
        if looks_cloud_synced(resolved_config.database_path):
            # Spec 32: a live SQLite file in a consumer sync folder will be
            # corrupted eventually. Refusing to start would be worse than
            # saying so loudly at every startup.
            logger.error(
                "the live database is inside a cloud-synced folder (%s); "
                "spec 32 forbids this and it will corrupt the database",
                resolved_config.database_path,
            )
        admin_control_plane = AdminControlPlane(
            actions=admin_action_repo,
            memories=MemoryAdminRepository(db),
            memory=memory_engine,
            backups=backup_service,
            clock=resolved_clock,
        )
        # Patch spec 23. Both read-only until the owner asks for a rebuild, and
        # neither can delete anything: a database that booted from a broken
        # Genesis still holds real Discord history (23.1).
        legacy_scanner = LegacyHealthScanner(
            health=health_repo, simulations=simulation_repo
        )
        # Rebuild spec 3.4: its own command, never part of a normal start.
        rebuild_service = RebuildService(
            db=db,
            backups=backup_service,
            backups_dir=resolved_config.backups_dir,
            clock=resolved_clock,
        )
        repair_service = RepairService(
            db=db,
            events=events_repo,
            simulations=simulation_repo,
            health=health_repo,
            backups=backup_service,
            shadow_dir=resolved_config.data_dir / "shadow",
            clock=resolved_clock,
        )

        # --- FIRST BOOT (spec 34.20 — Phase 13) ------------------------------
        # The single Authority on whether YUI exists yet. Built here and asked
        # by startup, the gateway, the CLI and the admin plane — none of which
        # is allowed its own opinion (point 1).
        #
        # Deliberately *not* started here (point 8): reading the state is a
        # startup concern, and beginning a nineteen-year generation is not.
        rebuild_epoch_repo = RebuildEpochRepository(db)
        first_boot_repo = FirstBootRepository(db)
        first_boot = FirstBootOrchestrator(
            repository=first_boot_repo,
            genesis=genesis_runner,
            genesis_runs=genesis_run_repo,
            records=life_record_repo,
            entities=life_entity_repo,
            experiences=genesis_experience_repo,
            audits=generation_audit_repo,
            rebuild=rebuild_epoch_repo,
            event_store=event_store,
            memories=memory_repo,
            state=state_repo,
            world=world_service,
            society=society_service,
            jobs=job_repo,
            life_days=life_day_repo,
            processor=processor,
            prompts=prompts,
            backups=backup_service,
            db=db,
            schema_version=current_schema,
            latest_schema=LATEST_VERSION,
            data_dir=resolved_config.data_dir,
            clock=resolved_clock,
        )

        # Rebuild spec 50 Phase 15. The go-live gate. Reads everything and
        # owns nothing: first boot, the contracts, the shadow record, the
        # model, the backups. The Discord gateway asks *this* at the door now,
        # and it still asks exactly one object exactly one question — Phase 15
        # makes the answer stricter, not plural.
        try:
            contracts = load_contracts()
        except Exception:  # noqa: BLE001 - a missing contract is a blocker, not a crash
            logger.exception("could not load the capability contracts")
            contracts = None
        live_readiness = LiveReadiness(
            first_boot=first_boot,
            shadow=shadow,
            shadow_decisions=shadow_decisions,
            contracts=contracts,
            backups=BackupRepository(db),
            ticks=runtime_tick_repo,
            owner_id=owner_user_id or "",
            channel_id=channel_id or "",
            token=(
                None
                if resolved_config.secrets.discord_bot_token is None
                else "set"
            ),
            schema_version=current_schema,
            latest_schema=LATEST_VERSION,
            enabled=resolved_config.runtime.live,
        )

        # The conversation path exists only when the single USER is identified
        # (spec 1.2). Without it, YUI has no one to talk to and stays offline.
        # Rebuild spec 30, Phase 5. Built before the conversation service so
        # the gateway can route admin first; ownership is checked here and
        # nowhere else.
        admin_router = AdminRouter(
            DebugQueryService(
                DebugSources(
                    state=state_repo,
                    events=event_store,
                    conversations=conversation_repo,
                    traces=trace_repo,
                    memories=memory_repo,
                    memory_inspector=memory_inspector,
                    failures=failures,
                    runs=runs,
                    llm_calls=llm_call_repo,
                    beliefs=belief_repo,
                    self_model=self_repo,
                    personality=trait_repo,
                    values=value_repo,
                    narrative=narrative_repo,
                    adaptations=adaptation_repo,
                    consolidations=consolidation_repo,
                    drift=drift_repo,
                    activities=activity_repo,
                    sleep=sleep_repo,
                    jobs=job_repo,
                    proactive=proactive_repo,
                    goals=goal_repo,
                    habits=habit_repo,
                    plans=plan_repo,
                    decisions=decision_repo,
                    npcs=npc_repo,
                    npc_relationships=npc_relationship_repo,
                    npc_interactions=npc_interaction_repo,
                    groups=group_repo,
                    knowledge=knowledge_repo,
                    tools=tool_call_repo,
                    acquisitions=acquisition_repo,
                    health=health_repo,
                    manifests=manifest_repo,
                    rebuild=rebuild_epoch_repo,
                    common_ground=common_ground_repo,
                    admin_actions=admin_action_repo,
                    runtime_ticks=runtime_tick_repo,
                    spontaneous_memory=spontaneous_memory_cues,
                    proactive_deliberations=proactive_deliberations,
                    shadow=shadow,
                    shadow_decisions=shadow_decisions,
                    live=live_readiness,
                    proactive_engine=proactive_engine,
                    proactive_source=proactive_source,
                    diary=diary_repo,
                    gaps=gap_repo,
                    searches=search_repo,
                    genesis_runs=genesis_run_repo,
                    life_records=life_record_repo,
                    generation_audits=generation_audit_repo,
                    genesis_experiences=genesis_experience_repo,
                    first_boot=first_boot,
                    life_days=life_day_repo,
                ),
                clock=resolved_clock,
            ),
            owner_user_id=owner_user_id,
            allowed_channel_ids=frozenset({channel_id} if channel_id else set()),
            backup=backup_service,
            clock=resolved_clock,
        )

        conversation: ConversationService | None = None
        if owner_user_id:
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
                event_store=event_store,
                tools=tool_manager,
                common_ground=CommonGroundTracker(
                    common_ground_repo,
                    extractor=claim_extractor,
                    guard=claim_guard,
                ),
                grounding=GroundingContextBuilder(
                    events=event_store,
                    activities=activity_repo,
                    memories=memory_repo,
                    beliefs=belief_repo,
                    goals=goal_repo,
                    tools=tool_manager,
                    npcs=npc_repo,
                    state=state_repo,
                    clock=resolved_clock,
                ),
                tracer=conversation_tracer,
                # RUNTIME-003: a USER turn outranks anything the loop wanted.
                runtime=autonomous_runtime,
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
            tracer=conversation_tracer,
            memories=memory_repo,
            memory=memory_engine,
            # Phase 2 §2Q: a separate object with no writer, so a debug search
            # cannot practise a memory however ``recall`` later changes.
            memory_inspector=memory_inspector,
            admin_router=admin_router,
            memory_policy=memory_policy,
            psychology_policy=psychology_policy,
            relationship_policy=relationship_policy,
            relationship=relationship_engine,
            attachment=attachment_engine,
            social_cognition=social_cognition_engine,
            belief_self_policy=belief_self_policy,
            beliefs=belief_engine,
            self_model=self_engine,
            tools=tool_manager,
            agency_policy=agency_policy,
            goals=goal_engine,
            habits=habit_engine,
            decisions=decision_engine,
            epistemics=epistemic_selector,
            world_policy=world_policy,
            world=world_service,
            scheduler=scheduler,
            jobs=job_repo,
            proactive=proactive_engine,
            runtime=autonomous_runtime,
            runtime_ticks=runtime_tick_repo,
            spontaneous_memory_cues=spontaneous_memory_cues,
            diary=diary_service,
            investigation=investigation,
            genesis_runner=genesis_runner,
            first_boot=first_boot,
            live=live_readiness,
            first_boot_state=first_boot_repo,
            genesis_runs=genesis_run_repo,
            genesis_experiences=genesis_experience_repo,
            life_records=life_record_repo,
            life_entities=life_entity_repo,
            generation_audits=generation_audit_repo,
            gaps=gap_repo,
            knowledge_repo=knowledge_repo,
            searches=search_repo,
            diaries=diary_repo,
            life_days=life_day_repo,
            proactive_deliberation=proactive_deliberation,
            shadow=shadow,
            shadow_decisions=shadow_decisions,
            proactive_deliberations=proactive_deliberations,
            growth_policy=growth_policy,
            adaptations=adaptation_engine,
            growth=growth_engine,
            values=value_engine,
            drift=drift_monitor,
            consolidation=consolidation_job,
            llm_policy=llm_policy,
            knowledge_policy=knowledge_policy,
            knowledge_builder=knowledge_builder,
            knowledge_providers=knowledge_providers,
            coverage=coverage_planner,
            knowledge=knowledge_service,
            simulation_policy=simulation_policy,
            seed_builder=seed_builder,
            simulation=simulation_engine,
            genesis=genesis_service,
            resources=resources,
            backups=backup_service,
            admin=admin_control_plane,
            legacy=legacy_scanner,
            repair=repair_service,
            rebuild=rebuild_service,
            society_policy=society_policy,
            society=society_service,
            npc_relationships=npc_relationship_engine,
            groups=group_engine,
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

        # Spec 32 startup order: recovery, then world catch-up, then scheduler
        # restore, then readiness. Time passed while the process was down and
        # pretending otherwise would leave the world lying about itself.
        self.catch_up = await self.db.run(self._catch_up_world)
        self.retired_jobs = await self.db.run(self.scheduler.restore)
        if self.retired_jobs:
            logger.info("scheduler restored; %d stale job(s) retired", self.retired_jobs)

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

        # Phase 13, points 8-10 and 54-57. Before FIRST BOOT the application
        # comes up in *management mode*: the CLI, the admin plane, backups,
        # status and resume all work, and nothing that constitutes her living
        # does. The autonomous runtime in particular stays down — Genesis is
        # replaying nineteen years of her past, and a 2026 life running
        # alongside it would interleave two timelines into one event stream.
        #
        # Nothing here starts Genesis. Reading the state is a startup concern;
        # beginning a nineteen-year generation needs the OWNER to ask.
        self.first_boot_status = self.first_boot.status()
        self.character_plane = self.first_boot.character_plane_open()
        if not self.character_plane:
            logger.warning(
                "management mode: first boot is %s; the character plane is closed",
                self.first_boot_status,
            )
            return event

        # RUNTIME-004. Last, on purpose: the loop must not start looking for
        # things to do until recovery, catch-up and scheduler restore have
        # settled. Waking into a half-recovered world is how she acts on a job
        # that the restore was about to retire.
        #
        # Point 37: and before the gateway, which `app.main` starts after this
        # returns. The USER's first word must not arrive at a system whose life
        # has not been switched on.
        if self.config.runtime.autonomous:
            await self.runtime.start()

        logger.info(
            "application ready mode=%s llm_healthy=%s autonomous=%s",
            self.config.runtime.mode,
            self.llm_healthy,
            self.config.runtime.autonomous,
        )
        return event

    def _catch_up_world(self):
        """Advance the world over the offline gap (spec 18.3, 32)."""
        entry = self.state.get("world", "sleep_pressure")
        if entry is None:
            return None
        return self.world.catch_up(
            since=entry.updated_at, sleep_pressure=entry.numeric or 0.25
        )

    async def stop(self, reason: str = "shutdown") -> None:
        # RUNTIME-004, first on purpose: cancel and drain the loop before the
        # database closes. An action half-done at exit is how a plan turns into
        # a memory of something that never happened.
        await self.runtime.stop()
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
