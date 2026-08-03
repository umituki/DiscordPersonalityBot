"""Repositories — the only place that speaks SQL (spec 38).

Attributes are resolved lazily. Repositories map domain objects from higher
layers (memory, conversation, agency, social) onto tables, so importing them
eagerly here would drag those layers into every module that merely touches
storage — and ``app.state.snapshot`` does exactly that, which turned an
innocent convenience import into an import cycle.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

_EXPORTS: dict[str, str] = {
    "AdaptationRepository": "growth",
    "AdminActionRepository": "admin",
    "BackupRepository": "admin",
    "MemoryAdminRepository": "admin",
    "BeliefRepository": "social",
    "CandidateRepository": "growth",
    "ConsolidationRepository": "growth",
    "DriftRepository": "growth",
    "NarrativeRepository": "growth",
    "PersonalityRepository": "growth",
    "ValueRepository": "growth",
    "ConversationRepository": "conversations",
    "ConversationTraceRepository": "traces",
    "CommonGroundRepository": "common_ground",
    "CoverageJobRepository": "knowledge",
    "DecisionRepository": "agency",
    "DiaryRepository": "diary",
    "LifeDayRepository": "diary",
    "DeliveryDecision": "deliveries",
    "DeliveryRepository": "deliveries",
    "EventRepository": "events",
    "ExposureRepository": "knowledge",
    "FailureRecord": "failures",
    "FailureRepository": "failures",
    "GoalRepository": "agency",
    "GroupRepository": "society",
    "HealthRepository": "health",
    "HabitRepository": "agency",
    "NPCInteractionRepository": "society",
    "NPCModelRepository": "society",
    "NPCRelationshipRepository": "society",
    "NPCRepository": "society",
    "SocialLinkRepository": "society",
    "LLMCallRepository": "llm_calls",
    "ManifestRepository": "manifests",
    "MemoryRepository": "memory",
    "PlanRepository": "agency",
    "ProcessingRunRepository": "runs",
    "ProactiveDeliberationRepository": "runtime",
    "RuntimeTickRepository": "runtime",
    "RebuildEpochRepository": "rebuild",
    "SelfRepository": "social",
    "SnapshotRepository": "snapshots",
    "StateRepository": "state",
    "ToolCallRepository": "tools",
    "AcquisitionRepository": "knowledge",
    "ActivityRepository": "world",
    "JobRepository": "world",
    "KnowledgeGapRepository": "knowledge",
    "KnowledgeRepository": "knowledge",
    "SearchCallRepository": "knowledge",
    "ProactiveRepository": "world",
    "SimulationRepository": "simulation",
    "SleepRepository": "world",
    "WorldHistoryRepository": "world",
}

__all__ = [
    "AcquisitionRepository",
    "ActivityRepository",
    "AdaptationRepository",
    "AdminActionRepository",
    "BackupRepository",
    "BeliefRepository",
    "CandidateRepository",
    "ConsolidationRepository",
    "ConversationRepository",
    "ConversationTraceRepository",
    "CommonGroundRepository",
    "CoverageJobRepository",
    "DecisionRepository",
    "DiaryRepository",
    "LifeDayRepository",
    "DeliveryDecision",
    "DeliveryRepository",
    "DriftRepository",
    "EventRepository",
    "ExposureRepository",
    "FailureRecord",
    "FailureRepository",
    "GoalRepository",
    "GroupRepository",
    "HabitRepository",
    "JobRepository",
    "KnowledgeGapRepository",
    "KnowledgeRepository",
    "SearchCallRepository",
    "LLMCallRepository",
    "ManifestRepository",
    "HealthRepository",
    "MemoryAdminRepository",
    "MemoryRepository",
    "NPCInteractionRepository",
    "NPCModelRepository",
    "NPCRelationshipRepository",
    "NPCRepository",
    "NarrativeRepository",
    "PersonalityRepository",
    "PlanRepository",
    "ProactiveRepository",
    "ProcessingRunRepository",
    "RebuildEpochRepository",
    "ProactiveDeliberationRepository",
    "RuntimeTickRepository",
    "SelfRepository",
    "SimulationRepository",
    "SleepRepository",
    "SnapshotRepository",
    "SocialLinkRepository",
    "StateRepository",
    "ToolCallRepository",
    "ValueRepository",
    "WorldHistoryRepository",
]

assert sorted(__all__) == sorted(_EXPORTS), "__all__ and _EXPORTS disagree"


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"{__name__}.{module_name}"), name)


def __dir__() -> list[str]:
    return list(__all__)


if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.storage.repositories.admin import (
        AdminActionRepository,
        BackupRepository,
        MemoryAdminRepository,
    )
    from app.storage.repositories.agency import (
        DecisionRepository,
        GoalRepository,
        HabitRepository,
        PlanRepository,
    )
    from app.storage.repositories.conversations import ConversationRepository
    from app.storage.repositories.traces import ConversationTraceRepository
    from app.storage.repositories.common_ground import CommonGroundRepository
    from app.storage.repositories.deliveries import DeliveryDecision, DeliveryRepository
    from app.storage.repositories.events import EventRepository
    from app.storage.repositories.failures import FailureRecord, FailureRepository
    from app.storage.repositories.growth import (
        AdaptationRepository,
        CandidateRepository,
        ConsolidationRepository,
        DriftRepository,
        NarrativeRepository,
        PersonalityRepository,
        ValueRepository,
    )
    from app.storage.repositories.knowledge import (
        AcquisitionRepository,
        CoverageJobRepository,
        ExposureRepository,
        KnowledgeGapRepository,
        KnowledgeRepository,
        SearchCallRepository,
    )
    from app.storage.repositories.llm_calls import LLMCallRepository
    from app.storage.repositories.manifests import ManifestRepository
    from app.storage.repositories.health import HealthRepository
    from app.storage.repositories.memory import MemoryRepository
    from app.storage.repositories.diary import DiaryRepository, LifeDayRepository
    from app.storage.repositories.rebuild import RebuildEpochRepository
    from app.storage.repositories.runs import ProcessingRunRepository
    from app.storage.repositories.runtime import (
        ProactiveDeliberationRepository,
        RuntimeTickRepository,
    )
    from app.storage.repositories.simulation import SimulationRepository
    from app.storage.repositories.snapshots import SnapshotRepository
    from app.storage.repositories.social import BeliefRepository, SelfRepository
    from app.storage.repositories.society import (
        GroupRepository,
        NPCInteractionRepository,
        NPCModelRepository,
        NPCRelationshipRepository,
        NPCRepository,
        SocialLinkRepository,
    )
    from app.storage.repositories.state import StateRepository
    from app.storage.repositories.tools import ToolCallRepository
    from app.storage.repositories.world import (
        ActivityRepository,
        JobRepository,
        ProactiveRepository,
        SleepRepository,
        WorldHistoryRepository,
    )
