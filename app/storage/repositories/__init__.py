"""Repositories — the only place that speaks SQL (spec 38)."""

from app.storage.repositories.deliveries import DeliveryDecision, DeliveryRepository
from app.storage.repositories.events import EventRepository
from app.storage.repositories.failures import FailureRecord, FailureRepository
from app.storage.repositories.llm_calls import LLMCallRepository
from app.storage.repositories.manifests import ManifestRepository
from app.storage.repositories.runs import ProcessingRunRepository
from app.storage.repositories.snapshots import SnapshotRepository
from app.storage.repositories.state import StateRepository

__all__ = [
    "DeliveryDecision",
    "DeliveryRepository",
    "EventRepository",
    "FailureRecord",
    "FailureRepository",
    "LLMCallRepository",
    "ManifestRepository",
    "ProcessingRunRepository",
    "SnapshotRepository",
    "StateRepository",
]
