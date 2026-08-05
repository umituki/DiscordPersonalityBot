"""INVARIANT: an incomplete life cannot reach FIRST_BOOT_COMPLETE
(rebuild spec 34.20 — Phase 13).

The OWNER's instruction for this phase is the shape of the whole file:

    「FIRST_BOOT_COMPLETEを書くコードがある」をテストするのではなく、
    不完全な人生では、どうやってもFIRST_BOOT_COMPLETEへ到達できない
    ことを多数の逆方向テストで証明してください。

So the forward path gets one end-to-end test and everything else pushes the
other way — incomplete, fatal, audit-failed, duplicate, parallel, crashed —
and asks whether the character plane is still shut. It always is.

The gate and the audit are both here on purpose. The audit detects an accident
after it happened; the gate prevents it. A system with only the first has
already let the USER talk to somebody who does not exist yet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.admin.router import ADMIN_PREFIX
from app.bootstrap import Application
from app.firstboot.events import (
    FIRST_BOOT_BLOCKED,
    FIRST_BOOT_COMPLETED,
    FIRST_BOOT_STARTED,
)
from app.firstboot.orchestrator import fingerprint
from app.firstboot.state import (
    CHARACTER_PLANE_OPEN,
    RESUMABLE,
    STARTABLE,
    character_plane_open,
)
from app.genesis.anchors import LifeAnchors, TemperamentSeed
from app.genesis.critics import CriticBoard
from app.genesis.models import (
    AnnualScaffold,
    AnnualSynthesis,
    CriticIssue,
    CriticVerdict,
    ExperienceActor,
    ExperienceCandidate,
    MonthNarrative,
)
from app.genesis.runner import Extraction
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"

#: A two-month life. The point of this phase is the machinery around Genesis,
#: not Genesis itself, and two months exercises every stage that nineteen years
#: would — including the partial final year, which is all of it here.
PRESENT = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
BIRTH = datetime(2024, 11, 1, tzinfo=timezone.utc)


@pytest.fixture
def owned_config(temp_config):
    return temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )


@pytest.fixture
def application(owned_config, clock):
    clock.set(PRESENT)
    built = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(built)
    _make_epoch(built)
    try:
        yield built
    finally:
        built.db.close()


def _make_epoch(application) -> str:
    """A rebuild epoch, because FIRST BOOT happens once per epoch (point 52)."""
    existing = application.rebuild.current_epoch()
    if existing is not None:
        return existing["epoch_id"]
    from app import ids

    epoch_id = ids.new_id("epo")
    application.db.execute(
        "INSERT INTO rebuild_epochs (epoch_id, started_at, reason, genesis_status) "
        "VALUES (?, ?, ?, 'pending')",
        (epoch_id, application.clock.now().isoformat(), "test"),
    )
    return epoch_id


@pytest.fixture
def anchors() -> LifeAnchors:
    return LifeAnchors(
        birth_datetime=BIRTH,
        present_datetime=PRESENT,
        gender_identity="女性",
        embodiment="自分の世界で暮らす一人の人物。",
        culture="日本",
        immutable_rules="身体を持たない",
        temperament=TemperamentSeed(openness=0.3),
    )


class Storyteller:
    """A model that tells a small consistent life, or refuses to."""

    #: What a real generator exposes, so provenance has something to record.
    model = "fixture-storyteller"

    def __init__(self, *, stop_after: dict[str, int] | None = None, body: bool = False) -> None:
        self.stop_after = stop_after or {}
        self.body = body  # a past that reaches into the USER's world
        self.seen: dict[str, int] = {}

    async def generate(self, schema, messages, *, purpose: str, **kwargs):
        self.seen[purpose] = self.seen.get(purpose, 0) + 1
        limit = self.stop_after.get(purpose)
        if limit is not None and self.seen[purpose] > limit:
            return type("Outcome", (), {"ok": False, "value": None})()
        return type("Outcome", (), {"ok": True, "value": self._value(schema)})()

    def _value(self, schema):
        if schema is AnnualScaffold:
            return AnnualScaffold(
                summary="静かな年だった。", people=("ミカ",), interests=("本",)
            )
        if schema is MonthNarrative:
            # identity v2: the world metadata is what the critic checks, so a
            # storyteller producing a forbidden past has to produce it here.
            return MonthNarrative(
                narrative=(
                    "USERと会って話した。" if self.body else "本を読んでいた。"
                ),
                importance="routine",
                participants=("user",) if self.body else ("npc",),
                interaction_scope="local_to_subject_world",
                people=("ミカ",),
            )
        if schema is AnnualSynthesis:
            return AnnualSynthesis(summary="読んでばかりの年。")
        if schema is Extraction:
            return Extraction(
                experiences=(
                    ExperienceCandidate(
                        occurred_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        actor_refs=(ExperienceActor(name="ミカ", subject="npc"),),
                        participants=("npc",),
                        interaction_scope="local_to_subject_world",
                        context="家で",
                        action="本を読んだ",
                        outcome="面白かった",
                        social_significance=0.4,
                    ),
                )
            )
        if schema is CriticVerdict:
            return CriticVerdict(passed=True)
        return schema()


def _teach(application, model) -> None:
    """Give the wired Genesis runner a model to talk to."""
    application.genesis_runner._structured = model  # type: ignore[attr-defined]
    board = application.genesis_runner._critics  # type: ignore[attr-defined]
    if board is not None:
        board._structured = model  # type: ignore[attr-defined]


# =============================================================================
# The forward path, once.
# =============================================================================


async def test_a_fresh_database_can_be_born(application, anchors) -> None:
    """Point 60's forward direction: fresh DB → start → Genesis → audits →
    finalisation → COMPLETE → the character plane opens."""
    _teach(application, Storyteller())
    assert application.first_boot.status() == "PENDING"
    assert not application.first_boot.character_plane_open()

    outcome = await application.first_boot.start(anchors)

    assert outcome.ok, outcome.reason
    assert application.first_boot.status() == "COMPLETE"
    assert application.first_boot.character_plane_open()

    # And it left the record it promised.
    report = application.first_boot.report()
    assert report["life_years"] >= 1
    assert report["months"] >= 1
    assert report["experiences"] > 0
    assert report["anchors_fingerprint"] == fingerprint(anchors)
    types = [event.event_type for event in application.event_store.recent(limit=200)]
    assert FIRST_BOOT_STARTED in types
    assert FIRST_BOOT_COMPLETED in types


async def test_the_completion_event_carries_counts_not_contents(
    application, anchors
) -> None:
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)

    event = next(
        item
        for item in application.event_store.recent(limit=200)
        if item.event_type == FIRST_BOOT_COMPLETED
    )

    assert event.category == "system"
    assert event.payload.experiences > 0
    # Counts, not prose (point 38).
    assert not any(
        isinstance(value, str) and len(value) > 100
        for value in event.payload.model_dump().values()
    )


# =============================================================================
# Everything else pushes the other way.
# =============================================================================

# --- incomplete generation ---------------------------------------------------


async def test_an_incomplete_run_never_completes(application, anchors) -> None:
    """Point 47. The model stops halfway; she is not born, and can be resumed."""
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert application.first_boot.status() == "BLOCKED_RETRYABLE"
    assert not application.first_boot.character_plane_open()
    assert application.first_boot.status() in RESUMABLE


async def test_a_blocked_run_can_be_resumed_to_completion(
    application, anchors
) -> None:
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)
    assert application.first_boot.status() == "BLOCKED_RETRYABLE"

    _teach(application, Storyteller())
    outcome = await application.first_boot.resume()

    assert outcome.ok, outcome.reason
    assert application.first_boot.status() == "COMPLETE"


# --- fatal -------------------------------------------------------------------


async def test_a_fatal_contradiction_blocks_fatally(application, anchors) -> None:
    """Point 48. An identity contradiction is not something a retry fixes."""
    _teach(application, Storyteller(body=True))

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert application.first_boot.status() == "BLOCKED_FATAL"
    assert not application.first_boot.character_plane_open()


async def test_a_fatal_block_refuses_to_resume(application, anchors) -> None:
    """A resume must not walk past a defect. The next attempt would produce the
    same answer, so the OWNER has to look."""
    _teach(application, Storyteller(body=True))
    await application.first_boot.start(anchors)
    assert application.first_boot.status() == "BLOCKED_FATAL"

    outcome = await application.first_boot.resume()

    assert not outcome.ok
    assert "fatal" in outcome.reason
    assert application.first_boot.status() == "BLOCKED_FATAL"
    assert not application.first_boot.character_plane_open()


async def test_an_unavailable_critic_blocks_retryably(application, anchors) -> None:
    """Point 21's other half: could-not-check is not the same as wrong."""

    class NoCritic(Storyteller):
        async def generate(self, schema, messages, *, purpose: str, **kwargs):
            if schema is CriticVerdict:
                raise RuntimeError("the model is gone")
            return await super().generate(schema, messages, purpose=purpose, **kwargs)

    _teach(application, NoCritic())

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert application.first_boot.status() == "BLOCKED_RETRYABLE"
    assert not application.first_boot.character_plane_open()


# --- the audits are the last gate --------------------------------------------


async def test_a_finished_genesis_with_a_failing_audit_does_not_complete(
    application, anchors
) -> None:
    """Point 49, and the most important test in the file.

    Generation runs to the end. Everything Genesis knows about is fine. And an
    audit fails, so she is not born — because the audits are a condition of
    completion rather than a report on it.
    """
    _teach(application, Storyteller())
    real_audits = application.genesis_runner.first_boot_audits

    async def one_fails(run_id, anchors_arg):
        from app.genesis.runner import AuditResult

        results = list(await real_audits(run_id, anchors_arg))
        return tuple(
            AuditResult(r.name, False, "deliberately broken")
            if r.name == "coverage"
            else r
            for r in results
        )

    application.genesis_runner.first_boot_audits = one_fails  # type: ignore[method-assign]

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert application.first_boot.status() == "BLOCKED_RETRYABLE"
    assert not application.first_boot.character_plane_open()
    # Generation itself really did finish.
    run_id = application.first_boot_state.current()["genesis_run_id"]
    assert application.life_records.years(run_id)
    assert application.genesis_experiences.count(
        genesis_run_id=run_id, replay_status="pending"
    ) == 0


async def test_a_lingering_past_activity_blocks_completion(
    application, anchors
) -> None:
    """Point 33. Something she began in the simulated past must not still be in
    progress when she boots."""
    _teach(application, Storyteller())
    real_promote = application.genesis_runner.promote_survivors

    def promote_then_linger(run_id, anchors_arg):
        result = real_promote(run_id, anchors_arg)
        application.world.start_activity(name="2025年に始めた読書", kind="leisure")
        return result

    application.genesis_runner.promote_survivors = promote_then_linger  # type: ignore[method-assign]

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert "ongoing" in outcome.reason
    assert application.first_boot.status() != "COMPLETE"
    assert not application.first_boot.character_plane_open()


async def test_a_stale_maximal_emotion_blocks_completion(
    application, anchors
) -> None:
    """Point 32. A feeling about something nineteen years ago must not be her
    current emotional state."""
    _teach(application, Storyteller())
    application.state.write_value(
        domain="emotion",
        key="joy",
        value=0.97,
        confidence=None,
        now=PRESENT - timedelta(days=900),
        run_id=None,
        event_id=None,
        expected_version=None,
    )

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert "emotion" in outcome.reason
    assert not application.first_boot.character_plane_open()


# --- duplicate and parallel --------------------------------------------------


async def test_a_second_start_is_refused(application, anchors) -> None:
    """Point 50. Two starts would build two lives and attach both to one epoch."""
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)
    runs_before = application.genesis_runs.count()

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert "use resume" in outcome.reason
    assert application.genesis_runs.count() == runs_before


async def test_start_after_complete_is_refused(application, anchors) -> None:
    """Point 51. Genesis happens once per epoch."""
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)
    assert application.first_boot.status() == "COMPLETE"

    started = await application.first_boot.start(anchors)
    resumed = await application.first_boot.resume()

    assert not started.ok and not resumed.ok
    assert application.genesis_runs.count() == 1


async def test_two_processes_cannot_both_run(application, anchors) -> None:
    """Point 27/28. The lease, tested by holding it from elsewhere."""
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)

    application.first_boot_state.acquire(
        application.first_boot_state.current()["epoch_id"],
        now=application.clock.now(),
        holder="another-terminal",
    )
    outcome = await application.first_boot.resume()

    assert not outcome.ok
    assert "another process" in outcome.reason


async def test_a_parallel_start_takes_the_lease_only_once(
    application, anchors
) -> None:
    epoch_id = application.first_boot_state.current() or application.first_boot_state.ensure(
        _make_epoch(application)
    )
    epoch = epoch_id["epoch_id"] if hasattr(epoch_id, "keys") else epoch_id
    now = application.clock.now()

    first = application.first_boot_state.acquire(epoch, now=now, holder="a")
    second = application.first_boot_state.acquire(epoch, now=now, holder="b")

    assert first is True
    assert second is False


# --- crash and recovery ------------------------------------------------------


async def test_a_crashed_run_is_recoverable_from_a_new_application(
    owned_config, clock, anchors
) -> None:
    """Point 25/26. Not the same Python object: a genuinely new Application,
    the way a restarted machine would build one.
    """
    clock.set(PRESENT)
    first = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(first)
    _make_epoch(first)
    _teach(first, Storyteller())

    # Kill it mid-replay, the way a machine dying would.
    real_process = first.processor.process
    budget = {"left": 1}

    async def die(event, **kwargs):
        from app.simulation.events import SIMULATED_EXPERIENCE

        if event.event_type == SIMULATED_EXPERIENCE:
            if budget["left"] <= 0:
                raise RuntimeError("the machine died")
            budget["left"] -= 1
        return await real_process(event, **kwargs)

    first.processor.process = die  # type: ignore[method-assign]
    outcome = await first.first_boot.start(anchors)
    assert not outcome.ok
    first.db.close()

    # A new process, a new Application, the same database.
    second = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(second)
    _teach(second, Storyteller())
    try:
        assert second.first_boot.status() in ("RUNNING", "BLOCKED_RETRYABLE")
        assert not second.first_boot.character_plane_open()

        resumed = await second.first_boot.resume()

        assert resumed.ok, resumed.reason
        assert second.first_boot.status() == "COMPLETE"
        assert second.first_boot.character_plane_open()
    finally:
        second.db.close()


async def test_a_stale_running_lease_is_reported_as_recoverable(
    application, anchors
) -> None:
    """Point 26. A crash leaves RUNNING with nothing alive to correct it.

    Refusing to resume it would be a deadlock only a database edit could break.
    """
    epoch = application.first_boot_state.ensure(_make_epoch(application))
    application.first_boot_state.transition(
        epoch["epoch_id"],
        expected=("PENDING",),
        to="RUNNING",
        now=application.clock.now(),
    )

    assert application.first_boot.status() == "RUNNING"
    assert application.first_boot.recovery_required()
    assert not application.first_boot.character_plane_open()
    assert application.first_boot.progress().recovery_required


# --- the anchors are frozen --------------------------------------------------


async def test_a_resume_uses_the_stored_anchors(application, anchors) -> None:
    """Point 17. Not the CLI config: re-reading a changed one would move her
    birthday halfway through her life."""
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)
    run_id = application.first_boot_state.current()["genesis_run_id"]
    stored = application.genesis_runs.anchors(run_id)

    _teach(application, Storyteller())
    await application.first_boot.resume()

    after = application.genesis_runs.anchors(run_id)
    assert after["birth_datetime"] == stored["birth_datetime"]
    assert after["embodiment"] == stored["embodiment"]


async def test_a_changed_fingerprint_is_fatal(application, anchors) -> None:
    """Point 18. The fingerprint is a check that the resume is continuing the
    same life."""
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)
    epoch_id = application.first_boot_state.current()["epoch_id"]
    application.first_boot_state.set_fingerprint(epoch_id, "something-else")

    outcome = await application.first_boot.resume()

    assert not outcome.ok
    assert "fingerprint" in outcome.reason


# --- the audit command has no side effects (point 29) ------------------------


async def test_the_audit_command_changes_nothing(application, anchors) -> None:
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)
    run_id = application.first_boot_state.current()["genesis_run_id"]
    before = (
        application.event_store.count(),
        application.memories.memory_count(),
        application.society.people(min_tier=0).__len__(),
        application.genesis_experiences.count(genesis_run_id=run_id),
        len(application.genesis_runs.checkpoints(run_id)),
    )

    for _ in range(3):
        await application.first_boot.audit()

    assert (
        application.event_store.count(),
        application.memories.memory_count(),
        application.society.people(min_tier=0).__len__(),
        application.genesis_experiences.count(genesis_run_id=run_id),
        len(application.genesis_runs.checkpoints(run_id)),
    ) == before


# --- survivor promotion is idempotent (point 7) ------------------------------


async def test_promoting_twice_makes_one_person(application, anchors) -> None:
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)
    run_id = application.first_boot_state.current()["genesis_run_id"]
    people = len(application.society.people(min_tier=0))
    links = [
        entity["npc_id"]
        for entity in application.life_entities.all_for(run_id)
        if entity["npc_id"]
    ]

    again = application.genesis_runner.promote_survivors(run_id, anchors)

    assert again == 0
    assert len(application.society.people(min_tier=0)) == people
    assert [
        entity["npc_id"]
        for entity in application.life_entities.all_for(run_id)
        if entity["npc_id"]
    ] == links


def test_the_schema_refuses_two_entities_for_one_npc(application) -> None:
    """Point 7's belt and braces. "Promote twice" is the shape of bug that
    survives code review and dies on a unique index."""
    import sqlite3

    from app import ids

    run_id = application.genesis_runs.start(
        birth=BIRTH, present=PRESENT, years=1, now=application.clock.now()
    )
    first = application.life_entities.upsert(
        genesis_run_id=run_id, type="NPC", canonical_name="A",
        introduced_at=application.clock.now(), objective={},
    )
    second = application.life_entities.upsert(
        genesis_run_id=run_id, type="NPC", canonical_name="B",
        introduced_at=application.clock.now(), objective={},
    )
    application.life_entities.link_npc(first, "npc_same")

    with pytest.raises(sqlite3.IntegrityError):
        application.life_entities.link_npc(second, "npc_same")


# --- bootstrap does not start Genesis (point 8) ------------------------------


async def test_startup_reads_the_state_and_starts_nothing(
    application, anchors
) -> None:
    runs_before = application.genesis_runs.count()

    await application.start()
    try:
        assert application.genesis_runs.count() == runs_before
        assert application.first_boot_status in ("PENDING", "READY")
        assert application.character_plane is False
        # Point 57: the autonomous runtime stays down. A 2026 life running
        # beside a Genesis replay would interleave two timelines.
        assert not application.runtime.running
    finally:
        await application.stop()


async def test_the_runtime_stays_down_even_when_configured_on(
    owned_config, clock, anchors
) -> None:
    """Point 57. `autonomous: true` is not permission to live before she exists.

    A 2026 life running beside a Genesis replay would interleave two timelines
    into one event stream — and the config saying "yes" is exactly the case
    where that would happen silently.
    """
    clock.set(PRESENT)
    autonomous = owned_config.model_copy(
        update={"runtime": owned_config.runtime.model_copy(update={"autonomous": True})}
    )
    built = Application.build(autonomous, clock=clock, configure_logs=False)
    use_offline_model(built)
    _make_epoch(built)

    await built.start()
    try:
        assert built.config.runtime.autonomous is True
        assert not built.runtime.running
        assert built.character_plane is False
    finally:
        await built.stop()
        built.db.close()


async def test_the_runtime_comes_up_once_she_exists(
    owned_config, clock, anchors
) -> None:
    """The same configuration, after first boot: now it runs."""
    clock.set(PRESENT)
    autonomous = owned_config.model_copy(
        update={"runtime": owned_config.runtime.model_copy(update={"autonomous": True})}
    )
    built = Application.build(autonomous, clock=clock, configure_logs=False)
    use_offline_model(built)
    _make_epoch(built)
    _teach(built, Storyteller())
    await built.first_boot.start(anchors)

    await built.start()
    try:
        assert built.character_plane is True
        assert built.runtime.running
    finally:
        await built.stop()
        built.db.close()


async def test_startup_after_completion_opens_the_plane(
    application, anchors
) -> None:
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)

    await application.start()
    try:
        assert application.first_boot_status == "COMPLETE"
        assert application.character_plane is True
    finally:
        await application.stop()


def test_bootstrap_never_calls_genesis() -> None:
    """Structural. A bootstrap that *could* start a nineteen-year run is one
    that can start it by accident."""
    import re
    from pathlib import Path

    source = Path("app/bootstrap.py").read_text(encoding="utf-8")
    assert not re.search(r"genesis_runner\.run\(", source)
    assert not re.search(r"first_boot\.(start|resume)\(", source)


# --- the character plane gate (points 9, 11) ---------------------------------


def test_only_complete_opens_the_plane() -> None:
    for status in (
        "PENDING",
        "READY",
        "RUNNING",
        "PAUSED",
        "BLOCKED_RETRYABLE",
        "BLOCKED_FATAL",
        "AUDITING",
    ):
        assert not character_plane_open(status), status
    assert character_plane_open("COMPLETE")
    assert CHARACTER_PLANE_OPEN == frozenset({"COMPLETE"})


async def test_a_user_message_is_refused_before_first_boot(
    application, anchors, clock
) -> None:
    """Point 11. The gate, not the audit. A USER message before first boot must
    not reach the conversation service at all."""
    from app.interfaces.discord.gateway import DiscordGateway

    seen: list[str] = []

    class Watching:
        def intends_to_reply(self, inbound):  # pragma: no cover - never reached
            seen.append(inbound.text)
            return True

        def start_trace(self, inbound):  # pragma: no cover
            raise AssertionError("the conversation service was reached")

        async def handle_inbound(self, inbound, **kwargs):  # pragma: no cover
            raise AssertionError("the conversation service was reached")

    gateway = DiscordGateway(
        Watching(), token="fake", first_boot=application.first_boot, clock=clock
    )

    @dataclass
    class Channel:
        id: int = int(CHANNEL)
        sent: list = None  # type: ignore[assignment]

        def __post_init__(self):
            self.sent = []

        async def send(self, text):
            self.sent.append(text)
            return type("Sent", (), {"id": 1})()

    @dataclass
    class Author:
        id: int = int(OWNER)
        bot: bool = False

    @dataclass
    class Message:
        channel: Any
        author: Any
        content: str
        created_at: Any
        id: int = 1
        guild: Any = None
        attachments: tuple = ()
        reference: Any = None

    channel = Channel()
    result = await gateway.handle_message(
        Message(channel=channel, author=Author(), content="やっほー", created_at=clock.now())
    )

    assert result.accepted is False
    assert seen == []


async def test_the_admin_plane_still_answers_before_first_boot(
    application, clock
) -> None:
    """Point 10. Character plane disabled, admin plane available — an operator
    has to be able to ask what is going on."""
    from app.interfaces.discord.gateway import DiscordGateway

    gateway = DiscordGateway(
        object(), token="fake", admin=application.admin_router,
        first_boot=application.first_boot, clock=clock,
    )

    @dataclass
    class Channel:
        id: int = int(CHANNEL)

        def __post_init__(self):
            self.sent: list[str] = []

        async def send(self, text):
            self.sent.append(text)
            return type("Sent", (), {"id": 1})()

    @dataclass
    class Author:
        id: int = int(OWNER)
        bot: bool = False

    @dataclass
    class Message:
        channel: Any
        author: Any
        content: str
        created_at: Any
        id: int = 1
        guild: Any = None
        attachments: tuple = ()
        reference: Any = None

    channel = Channel()
    await gateway.handle_message(
        Message(
            channel=channel,
            author=Author(),
            content=f"{ADMIN_PREFIX} firstboot",
            created_at=clock.now(),
        )
    )

    assert channel.sent, "the admin plane answered nothing"
    assert "PENDING" in channel.sent[0]


async def test_a_gate_that_cannot_read_the_state_stays_shut(clock) -> None:
    from app.interfaces.discord.gateway import DiscordGateway

    class Broken:
        def character_plane_open(self):
            raise RuntimeError("the database is gone")

    gateway = DiscordGateway(object(), token="fake", first_boot=Broken(), clock=clock)

    assert gateway._character_plane_open() is False


# --- the debug view (points 40, 41) ------------------------------------------


async def test_the_firstboot_view_is_read_only(application, anchors) -> None:
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)
    before = application.event_store.count()

    for _ in range(3):
        outcome = await application.admin_router.route(
            text=f"{ADMIN_PREFIX} firstboot", author_id=OWNER, channel_id=CHANNEL
        )
        assert not outcome.result.failed, outcome.result.error

    assert application.event_store.count() == before


def test_the_admin_plane_cannot_start_a_life() -> None:
    """Point 41. Nineteen years of generation should not be one typo away."""
    from app.admin.commands import REGISTRY

    assert "firstboot" in REGISTRY
    assert REGISTRY["firstboot"].is_read_only
    for key in REGISTRY:
        assert not key.startswith("firstboot start")
        assert not key.startswith("firstboot resume")


async def test_the_genesis_experiences_view_shows_replay_status(
    application, anchors
) -> None:
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)

    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} genesis experiences", author_id=OWNER, channel_id=CHANNEL
    )

    assert not outcome.result.failed, outcome.result.error
    rows = [row for section in outcome.result.sections for row in section.rows]
    assert rows
    assert rows[0]["replay_status"] == "replayed"


# --- preflight (point 15) ----------------------------------------------------


async def test_a_real_user_message_stops_the_preflight(
    application, anchors, make_event
) -> None:
    """Point 11 again, at the other end: a past that already contains the USER
    is not a past she can be given."""
    await application.processor.process(make_event(actor_type="user"))
    _teach(application, Storyteller())

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert "no_real_discord_history" in outcome.reason
    assert application.genesis_runs.count() == 0


async def test_a_missing_prompt_stops_the_preflight(application, anchors) -> None:
    """Found at second zero rather than hour six."""

    class NoPrompts:
        def get(self, prompt_id, version=None):
            raise KeyError(prompt_id)

    application.first_boot._prompts = NoPrompts()  # type: ignore[attr-defined]
    _teach(application, Storyteller())

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert "required_prompts" in outcome.reason


async def test_backwards_anchors_stop_the_preflight(application) -> None:
    backwards = LifeAnchors(
        birth_datetime=PRESENT, present_datetime=BIRTH, embodiment="x"
    )
    _teach(application, Storyteller())

    outcome = await application.first_boot.start(backwards)

    assert not outcome.ok
    assert "anchors_valid" in outcome.reason


async def test_no_epoch_means_no_first_boot(owned_config, clock, anchors) -> None:
    """Point 52. FIRST BOOT is once per rebuild epoch, so there has to be one."""
    clock.set(PRESENT)
    built = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(built)
    _teach(built, Storyteller())
    try:
        outcome = await built.first_boot.start(anchors)

        assert not outcome.ok
        assert "epoch" in outcome.reason
        assert built.genesis_runs.count() == 0
    finally:
        built.db.close()


async def test_a_start_takes_a_backup_first(application, anchors) -> None:
    """Point 16. The snapshot is for recovering from a bug *during* Genesis."""
    taken: list[str] = []
    real_create = application.backups.create

    def watch(*, reason: str, **kwargs):
        taken.append(reason)
        return real_create(reason=reason, **kwargs)

    application.first_boot._backups = type(  # type: ignore[attr-defined]
        "Backups", (), {"create": staticmethod(watch)}
    )()
    _teach(application, Storyteller())

    await application.first_boot.start(anchors)

    assert taken == ["pre_first_boot"]


# --- landing in the present (points 31, 34, 35) ------------------------------


async def test_the_world_lands_instead_of_catching_up(application, anchors) -> None:
    """Point 31. A job whose moment was in the simulated past must not come due
    the instant the runtime wakes — and nineteen years of them must not come due
    together."""
    _teach(application, Storyteller())
    stale = application.jobs.schedule(
        job_type="reminder",
        job_class="FIXED",
        due_at=BIRTH + timedelta(days=30),
        window_end=None,
        payload={},
        priority="P3",
        misfire_policy="skip",
        expires_at=None,
        now=BIRTH,
    )

    outcome = await application.first_boot.start(anchors)

    assert outcome.ok, outcome.reason
    after = application.jobs.get(stale.job_id)
    assert after.status == "expired"
    # Retired, not run: the moment for doing it was years ago.
    assert not [
        job for job in application.jobs.pending(limit=100) if job.due_at <= PRESENT
    ]
    assert application.first_boot.report()["landing"]["retired_jobs"] == 1


async def test_her_first_day_starts_at_first_boot(application, anchors) -> None:
    """Points 34 and 35. The diary's first entry covers today, not nineteen
    years — so the day it belongs to opens here."""
    _teach(application, Storyteller())

    await application.first_boot.start(anchors)

    day = application.life_days.current()
    assert day is not None
    assert day.started_at == PRESENT
    assert application.life_days.count() == 1


async def test_landing_twice_opens_one_day(application, anchors) -> None:
    """A crash between landing and COMPLETE re-runs finalisation. It must not
    give her two first days."""
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)

    application.first_boot._land(anchors)  # type: ignore[attr-defined]
    application.first_boot._land(anchors)  # type: ignore[attr-defined]

    assert application.life_days.count() == 1


async def test_an_unlanded_world_blocks_completion(application, anchors) -> None:
    """The landing is verified, not assumed. If something re-dates a job into
    the past after landing, completion still refuses."""
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)
    application.jobs.schedule(
        job_type="reminder", job_class="FIXED", due_at=BIRTH + timedelta(days=1),
        window_end=None, payload={}, priority="P3", misfire_policy="skip",
        expires_at=None, now=BIRTH,
    )

    problems = application.first_boot._final_consistency(  # type: ignore[attr-defined]
        application.first_boot_state.current()["genesis_run_id"], anchors
    )

    assert any("simulated past" in problem for problem in problems)


# --- a complete and empty life is not a life (point 45) ----------------------


async def test_an_empty_life_cannot_complete_even_with_every_audit_passing(
    application, anchors
) -> None:
    """Point 45, pushed as hard as it goes.

    The model produces months and extracts nothing from any of them, and every
    audit is replaced with one that says yes. She still is not born, because
    "complete" and "empty" must not be the same state.
    """

    class Barren(Storyteller):
        def _value(self, schema):
            if schema is Extraction:
                return Extraction(experiences=())
            return super()._value(schema)

    _teach(application, Barren())
    application.genesis_runner.first_boot_audits = (  # type: ignore[method-assign]
        lambda run_id, anchors_arg: _passing_audits()
    )

    outcome = await application.first_boot.start(anchors)

    assert not outcome.ok
    assert "experience" in outcome.reason or "remembers nothing" in outcome.reason
    assert application.first_boot.status() != "COMPLETE"
    assert not application.first_boot.character_plane_open()


async def _passing_audits():
    from app.genesis.runner import AuditResult, FIRST_BOOT_AUDITS

    return tuple(AuditResult(name, True, "") for name in FIRST_BOOT_AUDITS)


def test_a_run_with_no_years_fails_every_zero_row_check(application, anchors) -> None:
    run_id = application.genesis_runs.start(
        birth=BIRTH, present=PRESENT, years=1, now=application.clock.now()
    )

    problems = application.first_boot._final_consistency(run_id, anchors)  # type: ignore[attr-defined]

    assert any("no years" in problem for problem in problems)
    assert any("no months" in problem for problem in problems)
    assert any("experience" in problem for problem in problems)


# --- pause and resume (point 59) ---------------------------------------------


async def test_a_run_can_be_paused_and_resumed(application, anchors) -> None:
    """Point 59. Stopping is not the same as throwing it away, which is why
    there is no CANCELLED."""
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)
    epoch_id = application.first_boot_state.current()["epoch_id"]
    application.first_boot_state.transition(
        epoch_id, expected=("BLOCKED_RETRYABLE",), to="RUNNING",
        now=application.clock.now(),
    )

    paused = application.first_boot.pause()

    assert paused.ok
    assert application.first_boot.status() == "PAUSED"
    assert not application.first_boot.character_plane_open()

    _teach(application, Storyteller())
    resumed = await application.first_boot.resume()

    assert resumed.ok, resumed.reason
    assert application.first_boot.status() == "COMPLETE"


async def test_pause_keeps_the_run_and_the_work(application, anchors) -> None:
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)
    epoch_id = application.first_boot_state.current()["epoch_id"]
    run_id = application.first_boot_state.current()["genesis_run_id"]
    months = len(application.life_records.years(run_id))
    application.first_boot_state.transition(
        epoch_id, expected=("BLOCKED_RETRYABLE",), to="RUNNING",
        now=application.clock.now(),
    )

    application.first_boot.pause()

    assert application.first_boot_state.current()["genesis_run_id"] == run_id
    assert len(application.life_records.years(run_id)) == months


async def test_pause_after_completion_is_refused(application, anchors) -> None:
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)

    outcome = application.first_boot.pause()

    assert not outcome.ok
    assert application.first_boot.status() == "COMPLETE"


# --- the retry budget (point 23) ---------------------------------------------


async def test_enough_retryable_failures_become_fatal(application, anchors) -> None:
    """Point 23. `model_unavailable` looks identical on attempt one and attempt
    fifty; without a budget, `resume` in a shell loop rediscovers it for a week.
    """
    from app.firstboot.state import MAX_ATTEMPTS

    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)
    epoch_id = application.first_boot_state.current()["epoch_id"]
    while application.first_boot_state.get(epoch_id)["attempt_count"] < MAX_ATTEMPTS:
        application.first_boot_state.bump_attempt(epoch_id)

    outcome = await application.first_boot.resume()

    assert not outcome.ok
    assert application.first_boot.status() == "BLOCKED_FATAL"
    assert application.first_boot_state.get(epoch_id)["block_kind"] == "retry_exhausted"
    # The diagnosis survives the escalation.
    assert "incomplete" in application.first_boot_state.get(epoch_id)["block_reason"]
    assert not application.first_boot.character_plane_open()


async def test_an_exhausted_budget_stops_resuming(application, anchors) -> None:
    from app.firstboot.state import MAX_ATTEMPTS

    _teach(application, Storyteller(stop_after={"genesis_month": 1}))
    await application.first_boot.start(anchors)
    epoch_id = application.first_boot_state.current()["epoch_id"]
    while application.first_boot_state.get(epoch_id)["attempt_count"] < MAX_ATTEMPTS:
        application.first_boot_state.bump_attempt(epoch_id)
    await application.first_boot.resume()

    # Even with a working model, the fatal block stands until the OWNER looks.
    _teach(application, Storyteller())
    outcome = await application.first_boot.resume()

    assert not outcome.ok
    assert application.first_boot.status() == "BLOCKED_FATAL"


# --- a fatal block is not tidied up (point 42) -------------------------------


async def test_a_fatal_block_repairs_nothing_by_itself(application, anchors) -> None:
    """Point 42. The system must not decide what to throw away. A half-built
    life is evidence, and deleting it is the OWNER's call."""
    _teach(application, Storyteller(body=True))
    await application.first_boot.start(anchors)
    assert application.first_boot.status() == "BLOCKED_FATAL"
    run_id = application.first_boot_state.current()["genesis_run_id"]
    years = len(application.life_records.years(run_id))
    epoch_id = application.first_boot_state.current()["epoch_id"]

    for _ in range(3):
        await application.first_boot.resume()
        await application.first_boot.audit()

    row = application.first_boot_state.get(epoch_id)
    assert row["status"] == "BLOCKED_FATAL"
    assert row["genesis_run_id"] == run_id
    assert row["block_reason"]
    assert len(application.life_records.years(run_id)) == years
    assert application.genesis_runs.count() == 1


# --- the report says where she came from (points 43, 44) ---------------------


async def test_the_report_records_which_model_made_her(application, anchors) -> None:
    _teach(application, Storyteller())

    await application.first_boot.start(anchors)

    report = application.first_boot.report()
    assert report["prompt_versions"], "no prompt version was recorded"
    assert report["model_versions"], "no model version was recorded"
    assert report["finalisation_stages"][0] == "genesis_audits"
    assert report["finalisation_stages"][-1] == "complete"
    assert report["completed_at"]


async def test_finalisation_runs_its_stages_in_order(application, anchors) -> None:
    """Point 30. The order is a rule, not the accident of how the method reads:
    audits before promotion, promotion before landing, landing before the check
    that the landing worked."""
    _teach(application, Storyteller())
    seen: list[str] = []
    real_touch = application.first_boot_state.touch

    def watch(epoch_id, **kwargs):
        stage = kwargs.get("stage")
        if stage:
            seen.append(stage)
        return real_touch(epoch_id, **kwargs)

    application.first_boot._repository.touch = watch  # type: ignore[attr-defined]

    await application.first_boot.start(anchors)

    ordered = [stage for stage in seen if stage != "genesis"]
    assert ordered == ["survivors_promoted", "landed", "final_consistency", "report_written"]


# --- the CLI (points 12, 14) -------------------------------------------------


@pytest.mark.parametrize("action", ["status", "audit", "report", "pause", "resume"])
def test_every_first_boot_command_runs(owned_config, capsys, action) -> None:
    """A command that raises `NameError` on a fresh database is a command
    nobody ran. Every action, against a database that has never booted."""
    from app import main

    settings = owned_config.root_dir / "config" / "settings.yaml"
    argv = ["--config", str(settings), "--root", str(owned_config.root_dir)]
    # `first-boot` does not migrate on the way in, deliberately: a command that
    # inspects a database should not quietly change its schema.
    main.main([*argv, "migrate"])
    capsys.readouterr()
    code = main.main([*argv, "first-boot", action])

    output = capsys.readouterr().out
    assert output.strip(), f"`first-boot {action}` printed nothing"
    # Nothing has been started, so nothing succeeds — but it says so rather
    # than crashing.
    assert code in (0, 1)


def test_start_needs_a_birthday(owned_config, capsys) -> None:
    from app import main

    settings = owned_config.root_dir / "config" / "settings.yaml"
    argv = ["--config", str(settings), "--root", str(owned_config.root_dir)]
    main.main([*argv, "migrate"])
    capsys.readouterr()
    code = main.main([*argv, "first-boot", "start"])

    assert code == 2
    assert "--birth" in capsys.readouterr().out


# --- the state machine itself ------------------------------------------------


def test_the_state_machine_has_no_cancel() -> None:
    """Genesis generates a life. The useful response to wanting to stop one is
    to stop and resume, not to throw it away."""
    from app.firstboot.state import FirstBootStatus
    import typing

    assert "CANCELLED" not in typing.get_args(FirstBootStatus)


def test_running_is_resumable_and_not_startable() -> None:
    assert "RUNNING" in RESUMABLE
    assert "RUNNING" not in STARTABLE
    assert "BLOCKED_FATAL" not in RESUMABLE


async def test_a_block_records_why_and_when(application, anchors) -> None:
    _teach(application, Storyteller(stop_after={"genesis_month": 1}))

    await application.first_boot.start(anchors)

    row = application.first_boot_state.current()
    assert row["block_kind"] == "incomplete"
    assert row["block_reason"]
    assert row["blocked_at"]
    assert row["attempt_count"] == 1
    types = [event.event_type for event in application.event_store.recent(limit=100)]
    assert FIRST_BOOT_BLOCKED in types


async def test_progress_expects_months_from_the_anchors(
    application, anchors
) -> None:
    """Point 19: never a hard-coded 228. The partial final year means the
    expected count is arithmetic on the anchors."""
    _teach(application, Storyteller())
    await application.first_boot.start(anchors)

    view = application.first_boot.progress()

    assert view.expected_months == sum(span.months for span in anchors.years)
    assert view.expected_months != 228
    assert view.months_done == view.expected_months


async def test_progress_survives_a_restart(owned_config, clock, anchors) -> None:
    """Point 3. The state is in the database because the case it exists for is
    the process going away."""
    clock.set(PRESENT)
    first = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(first)
    _make_epoch(first)
    _teach(first, Storyteller(stop_after={"genesis_month": 1}))
    await first.first_boot.start(anchors)
    before = first.first_boot.progress()
    first.db.close()

    second = Application.build(owned_config, clock=clock, configure_logs=False)
    try:
        after = second.first_boot.progress()

        assert after.status == before.status
        assert after.genesis_run_id == before.genesis_run_id
        assert after.months_done == before.months_done
        assert after.block_kind == before.block_kind
    finally:
        second.db.close()
