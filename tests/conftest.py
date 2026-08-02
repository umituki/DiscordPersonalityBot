"""Shared fixtures.

Testing rules: temporary databases and isolated filesystem paths only. Nothing
here may touch ``data/``, ``backups/`` or a production SQLite file.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from app.clock import FixedClock
from app.config import AppConfig, load_config
from app.events.bus import EventBus, SubscriberResult
from app.events.dispatcher import EventDispatcher
from app.events.model import Event, EventPayload, register_payload
from app.events.store import EventStore
from app.llm.prompts import PromptRegistry
from app.llm.tracing import DatabaseTracer
from app.state.arbitrator import StateArbitrator
from app.state.committer import StateCommitter
from app.state.policy import ArbitrationPolicy
from app.state.proposal import StateChangeProposal
from app.orchestrator.run_view import RunView
from app.state.snapshot import SnapshotService
from app.storage.database import Database
from app.storage.migrations import migrate
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

REPO_ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


# --- test-only event payload ----------------------------------------------
@register_payload("TEST_SIGNAL")
class SignalPayload(EventPayload):
    label: str = "signal"
    strength: float = 1.0
    #: Some engines read ``payload.text``; this keeps the fixture usable there.
    text: str = ""


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(START)


@pytest.fixture
def db(tmp_path: Path, clock: FixedClock) -> Iterator[Database]:
    database = Database(tmp_path / "test.db", synchronous="OFF")
    database.connect()
    migrate(database, clock=clock)
    yield database
    database.close()


@pytest.fixture
def policy() -> ArbitrationPolicy:
    """The real, committed policy — tests assert against shipped values."""
    return ArbitrationPolicy.load(REPO_ROOT / "config" / "policies" / "state_arbitration.yaml")


@pytest.fixture
def events_repo(db: Database) -> EventRepository:
    return EventRepository(db)


@pytest.fixture
def deliveries(db: Database) -> DeliveryRepository:
    return DeliveryRepository(db)


@pytest.fixture
def failures(db: Database) -> FailureRepository:
    return FailureRepository(db)


@pytest.fixture
def runs(db: Database) -> ProcessingRunRepository:
    return ProcessingRunRepository(db)


@pytest.fixture
def state_repo(db: Database) -> StateRepository:
    return StateRepository(db)


@pytest.fixture
def snapshot_repo(db: Database) -> SnapshotRepository:
    return SnapshotRepository(db)


@pytest.fixture
def manifest_repo(db: Database) -> ManifestRepository:
    return ManifestRepository(db)


@pytest.fixture
def event_store(db: Database, events_repo: EventRepository, clock: FixedClock) -> EventStore:
    return EventStore(db, events_repo, clock=clock)


@pytest.fixture
def snapshots(
    state_repo: StateRepository, snapshot_repo: SnapshotRepository, clock: FixedClock
) -> SnapshotService:
    return SnapshotService(state_repo, snapshot_repo, clock=clock)


@pytest.fixture
def llm_calls_repo(db: Database) -> LLMCallRepository:
    return LLMCallRepository(db)


@pytest.fixture
def tracer(llm_calls_repo: LLMCallRepository, clock: FixedClock) -> DatabaseTracer:
    return DatabaseTracer(llm_calls_repo, clock=clock)


@pytest.fixture
def prompt_registry() -> PromptRegistry:
    """The committed prompt files."""
    return PromptRegistry.load(REPO_ROOT / "config" / "prompts")


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def dispatcher(
    bus: EventBus,
    deliveries: DeliveryRepository,
    failures: FailureRepository,
    clock: FixedClock,
) -> EventDispatcher:
    return EventDispatcher(bus, deliveries, failures, clock=clock)


@pytest.fixture
def arbitrator(policy: ArbitrationPolicy) -> StateArbitrator:
    return StateArbitrator(policy)


@pytest.fixture
def committer(
    db: Database,
    state_repo: StateRepository,
    runs: ProcessingRunRepository,
    deliveries: DeliveryRepository,
    failures: FailureRepository,
    clock: FixedClock,
) -> StateCommitter:
    return StateCommitter(db, state_repo, runs, deliveries, failures, clock=clock)


@pytest.fixture
def make_event(clock: FixedClock):
    """Factory for user-message-like social events."""

    def factory(
        *,
        event_type: str = "TEST_SIGNAL",
        category: str = "social",
        origin: str = "real_discord",
        actor_type: str = "user",
        payload: EventPayload | None = None,
        text: str | None = None,
        **kwargs,
    ) -> Event:
        if payload is None and text is not None:
            payload = SignalPayload(text=text)
        return Event.create(
            event_type=event_type,
            category=category,  # type: ignore[arg-type]
            actor_type=actor_type,  # type: ignore[arg-type]
            source_type="test_harness",
            origin=origin,  # type: ignore[arg-type]
            payload=payload or SignalPayload(),
            clock=clock,
            **kwargs,
        )

    return factory


class RecordingSubscriber:
    """Subscriber that returns pre-programmed proposals and counts calls."""

    def __init__(
        self,
        name: str,
        proposals: tuple[StateChangeProposal, ...] = (),
        *,
        raises: Exception | None = None,
        proposal_factory=None,
    ) -> None:
        self.name = name
        self._proposals = proposals
        self._raises = raises
        self._factory = proposal_factory
        self.calls: list[str] = []
        self.seen_views: list[RunView] = []

    @property
    def seen_snapshots(self):
        return [view.snapshot for view in self.seen_views]

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        self.calls.append(event.event_id)
        self.seen_views.append(view)
        if self._raises is not None:
            raise self._raises
        proposals = self._factory(event, view) if self._factory else self._proposals
        return SubscriberResult(proposals=tuple(proposals))


@pytest.fixture
def recording_subscriber():
    return RecordingSubscriber


@pytest.fixture
def temp_config(tmp_path: Path) -> AppConfig:
    """A full config rooted in tmp_path, with the real policy file copied in."""
    (tmp_path / "config" / "policies").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config" / "prompts", tmp_path / "config" / "prompts")
    # The knowledge bundles are part of the shipped configuration: without them
    # a Genesis has no period knowledge, and the FIRST BOOT knowledge health
    # audit refuses the boot (patch spec 16.1, 17.3).
    shutil.copytree(
        REPO_ROOT / "config" / "knowledge", tmp_path / "config" / "knowledge"
    )
    shutil.copytree(REPO_ROOT / "character", tmp_path / "character")
    for policy in (
        "output_guard.yaml",
        "conversation.yaml",
        "memory.yaml",
        "psychology.yaml",
        "relationship.yaml",
        "belief_self.yaml",
        "agency.yaml",
        "world.yaml",
        "growth.yaml",
        "society.yaml",
        "knowledge.yaml",
        "simulation.yaml",
        "llm.yaml",
    ):
        shutil.copy(
            REPO_ROOT / "config" / "policies" / policy,
            tmp_path / "config" / "policies" / policy,
        )
    shutil.copy(
        REPO_ROOT / "config" / "settings.yaml", tmp_path / "config" / "settings.yaml"
    )
    shutil.copy(
        REPO_ROOT / "config" / "policies" / "state_arbitration.yaml",
        tmp_path / "config" / "policies" / "state_arbitration.yaml",
    )
    config = load_config(root_dir=tmp_path, env={}, use_dotenv=False)
    # No model host in tests: one attempt, no backoff waiting.
    return config.model_copy(
        update={
            "llm": config.llm.model_copy(
                update={
                    # Keep tests isolated from an Ollama instance that may be
                    # running on the developer's machine.
                    "base_url": "http://127.0.0.1:1",
                    "max_attempts": 1,
                }
            )
        }
    )
