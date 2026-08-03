"""INVARIANT: the diary is a third thing, and it never keeps her awake
(rebuild spec 26 — Phase 10).

26.1 draws the line the whole subsystem rests on::

    Event ≠ Memory ≠ Diary

An event is what happened. A memory is what she kept, already reshaped by how
it felt. A diary entry is what she wrote at the end of a day about how the day
went — and it is authoritative about exactly one thing (26.6): that she wrote
it. Not about whether what she wrote was true.

The other rule with teeth is 26.3's:

    ただし LLM failure が睡眠を永久に阻害してはならない。

A model that hangs must not keep her awake. That rules out generate-then-sleep
and forces the order used here.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.admin.router import ADMIN_PREFIX
from app.bootstrap import Application
from app.diary.events import BEDTIME_REFLECTION_STARTED, DIARY_READ, DIARY_WRITTEN
from app.diary.models import DiaryDraft, DiaryReference
from app.world.events import WENT_TO_SLEEP
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


@pytest.fixture
def application(temp_config, clock):
    owned = temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )
    built = Application.build(owned, clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


class Writer:
    """A model that writes a diary, or misbehaves in a specific way."""

    def __init__(self, draft: DiaryDraft | None = None, *, fail=None) -> None:
        self.draft = draft or DiaryDraft(
            content="今日は本を読んだ。静かな日だった。",
            summary="静かな日",
            felt="quiet",
        )
        self.fail = fail
        self.calls = 0

    async def generate(self, schema, messages, *, purpose: str, **kwargs):
        self.calls += 1
        if self.fail == "raise":
            raise RuntimeError("the model is gone")
        if self.fail == "hang":
            await asyncio.sleep(3600)
        if self.fail == "empty":
            return type("Outcome", (), {"ok": True, "value": DiaryDraft()})()
        return type("Outcome", (), {"ok": True, "value": self.draft})()


def _with_model(application, writer):
    application.diary._structured = writer  # type: ignore[attr-defined]
    return writer


def _make_exhausted(application, clock) -> None:
    clock.set(clock.now().replace(hour=3, minute=0))
    existing = application.state.get("world", "sleep_pressure")
    application.state.write_value(
        domain="world", key="sleep_pressure", value=1.0, confidence=None,
        now=clock.now(), run_id=None, event_id=None,
        expected_version=None if existing is None else existing.version,
    )


# --- 26.2: the day is not the calendar date ----------------------------------


def test_a_day_runs_from_waking_to_sleeping(application, clock) -> None:
    """日付境界は 0:00 固定ではなく major wake → next major sleep."""
    day = application.diary.begin_day()

    assert day.is_open
    assert day.started_at == clock.now()
    assert application.diary.current_day().life_day_id == day.life_day_id


def test_opening_a_day_twice_gives_one_day(application, clock) -> None:
    """A wake recorded twice — a retry, a restart mid-transition — must not
    produce two days and therefore two diaries for one evening."""
    first = application.diary.begin_day()
    clock.advance(minutes=5)
    second = application.diary.begin_day()

    assert first.life_day_id == second.life_day_id
    assert application.life_days.count() == 1


async def test_a_nap_does_not_end_a_day(application, clock) -> None:
    """Otherwise an afternoon doze gives her two diaries for one afternoon."""
    _make_exhausted(application, clock)
    _with_model(application, Writer())
    signals = application.world.consider_sleep(sleep_pressure=1.0)
    application.world.fall_asleep(signals=signals, kind="nap")
    before = application.life_days.count()

    clock.advance(hours=1)
    await application.runtime.tick()  # wakes up

    assert application.life_days.count() == before


async def test_a_night_starts_the_next_day(application, clock) -> None:
    _make_exhausted(application, clock)
    _with_model(application, Writer())
    await application.runtime.tick()  # falls asleep
    assert application.world.current_sleep() is not None

    clock.advance(hours=9)
    await application.runtime.tick()  # wakes up

    day = application.diary.current_day()
    assert day is not None
    assert day.is_open


# --- 26.3: the diary never keeps her awake -----------------------------------


async def test_she_writes_before_sleeping(application, clock) -> None:
    application.diary.begin_day()
    clock.advance(hours=16)
    _make_exhausted(application, clock)
    _with_model(application, Writer())

    await application.runtime.tick()

    entry = application.diaries.recent()[0]
    assert entry.is_written
    assert entry.content
    types = [event.event_type for event in application.event_store.recent(limit=20)]
    assert BEDTIME_REFLECTION_STARTED in types
    assert DIARY_WRITTEN in types
    # The order 26.3 asks for: reflection first, then the transition.
    # `recent` is newest-first, so the later event has the smaller index.
    assert types.index(WENT_TO_SLEEP) < types.index(BEDTIME_REFLECTION_STARTED)


async def test_a_hanging_model_does_not_keep_her_awake(application, clock) -> None:
    """The sentence this phase is built around."""
    application.diary.begin_day()
    clock.advance(hours=16)
    _make_exhausted(application, clock)
    _with_model(application, Writer(fail="hang"))
    application.diary._timeout = 0.05  # type: ignore[attr-defined]

    await application.runtime.tick()

    assert application.world.current_sleep() is not None, "she is still awake"
    pending = application.diaries.pending()
    assert len(pending) == 1
    assert pending[0].status == "pending"


async def test_a_broken_model_does_not_keep_her_awake(application, clock) -> None:
    application.diary.begin_day()
    clock.advance(hours=16)
    _make_exhausted(application, clock)
    _with_model(application, Writer(fail="raise"))

    await application.runtime.tick()

    assert application.world.current_sleep() is not None
    assert len(application.diaries.pending()) == 1


async def test_an_empty_draft_leaves_the_entry_owed(application, clock) -> None:
    application.diary.begin_day()
    _make_exhausted(application, clock)
    _with_model(application, Writer(fail="empty"))

    await application.runtime.tick()

    assert application.world.current_sleep() is not None
    assert application.diaries.count(status="written") == 0
    assert len(application.diaries.pending()) == 1


async def test_the_retry_keeps_the_bedtime_it_was_meant_for(
    application, clock
) -> None:
    """A diary written at four in the morning is still last night's diary."""
    application.diary.begin_day()
    clock.advance(hours=16)
    _make_exhausted(application, clock)
    _with_model(application, Writer(fail="raise"))
    await application.runtime.tick()
    owed = application.diaries.pending()[0]
    intended = owed.intended_at

    clock.advance(hours=4)
    _with_model(application, Writer())
    written = await application.diary.retry_pending()

    assert written == 1
    entry = application.diaries.get(owed.diary_id)
    assert entry.status == "late_written"
    assert entry.was_late
    assert entry.intended_at == intended
    assert entry.generated_at is not None
    assert entry.generated_at > entry.intended_at


async def test_a_late_entry_is_not_the_same_status_as_a_punctual_one(
    application, clock
) -> None:
    """"Written at the time" and "written eventually" are different facts about
    how much of the day she still had in mind."""
    application.diary.begin_day()
    _make_exhausted(application, clock)
    _with_model(application, Writer())
    await application.runtime.tick()

    assert application.diaries.recent()[0].status == "written"
    assert not application.diaries.recent()[0].was_late


async def test_attempts_are_counted(application, clock) -> None:
    application.diary.begin_day()
    _make_exhausted(application, clock)
    _with_model(application, Writer(fail="raise"))
    await application.runtime.tick()

    assert application.diaries.pending()[0].attempts >= 1


# --- 26.5: free prose --------------------------------------------------------


def test_the_draft_schema_imposes_no_form() -> None:
    """固定フォーム禁止. No headings, no required sections, no minimum length —
    a schema that demanded three paragraphs would produce three paragraphs of
    invention."""
    fields = DiaryDraft.model_fields
    assert set(fields) == {"content", "summary", "felt", "about_memory_ids"}
    assert fields["content"].default == ""
    # Nothing forces a length.
    assert DiaryDraft(content="うん。").content == "うん。"


async def test_a_day_where_nothing_happened_is_allowed_to_be_short(
    application, clock
) -> None:
    application.diary.begin_day()
    _make_exhausted(application, clock)
    _with_model(
        application, Writer(DiaryDraft(content="なにもない日。", summary="なにもない", felt="quiet"))
    )

    await application.runtime.tick()

    entry = application.diaries.recent()[0]
    assert entry.content == "なにもない日。"
    assert entry.importance < 0.5, "an empty day should not be a significant one"


# --- 26.4: compressed, not dumped --------------------------------------------


async def test_the_context_is_compressed(application, clock) -> None:
    """重要度で圧縮し、全 Event dump はしない."""
    from app.diary.service import MAX_ACTIVITIES

    day = application.diary.begin_day()
    for index in range(MAX_ACTIVITIES + 6):
        activity, _ = application.world.start_activity(
            name=f"活動{index}", kind="leisure"
        )
        application.world.finish_activity(activity.activity_id, outcome="やった")

    context = application.diary._builder.build(day)  # type: ignore[attr-defined]

    assert len(context.activities) <= MAX_ACTIVITIES


async def test_an_empty_day_says_so(application, clock) -> None:
    day = application.diary.begin_day()

    context = application.diary._builder.build(day)  # type: ignore[attr-defined]

    assert context.is_empty
    assert "何もなかった" in context.render()


# --- 26.7: the diary is not memory -------------------------------------------


async def test_the_diary_text_is_never_copied_into_memory(
    application, clock
) -> None:
    """日記本文を自動的に episodic memory へコピーしない.

    Copying it would turn her account of a day into the day itself — the exact
    collapse 26.1 separates.
    """
    application.diary.begin_day()
    _make_exhausted(application, clock)
    _with_model(
        application,
        Writer(DiaryDraft(content="とても特徴的な日記本文です。", summary="特徴的", felt="good")),
    )
    before = application.memories.memory_count()

    await application.runtime.tick()

    after = application.memories.memory_count()
    summaries = [
        memory.summary for memory in application.memories.recent_memories(limit=50)
    ]
    assert not any("とても特徴的な日記本文" in summary for summary in summaries)
    assert after == before or "日記" not in " ".join(summaries)


async def test_only_what_she_wrote_about_is_marked_as_mentioned(
    application, clock
) -> None:
    """26.7 allows practice on what she actually looked at, and only that."""
    from app.diary.service import DailyDiaryContext

    day = application.diary.begin_day()
    entry = application.diaries.intend(
        life_day_id=day.life_day_id, intended_at=clock.now()
    )
    application.diaries.add_references(
        entry.diary_id,
        (
            DiaryReference(
                reference_type="memory", reference_id="mem_1", mentioned_in_text=True
            ),
            DiaryReference(
                reference_type="memory", reference_id="mem_2", mentioned_in_text=False
            ),
        ),
    )

    stored = application.diaries.get(entry.diary_id)

    assert len(stored.references) == 2
    assert [item.reference_id for item in stored.mentioned] == ["mem_1"]


def test_writing_it_is_an_experience() -> None:
    """DIARY_WRITTEN 自体は経験 — the act, not the text."""
    from app.events.model import payload_model

    assert payload_model(DIARY_WRITTEN, 1) is not None


# --- 26.8: reading it is a deliberate act ------------------------------------


async def test_reading_the_diary_is_its_own_event(application, clock) -> None:
    application.diary.begin_day()
    _make_exhausted(application, clock)
    _with_model(application, Writer())
    await application.runtime.tick()
    entry = application.diaries.recent()[0]

    clock.advance(days=3)
    read = await application.diary.read(entry.diary_id)

    assert read is not None
    events = [
        event
        for event in application.event_store.recent(limit=20)
        if event.event_type == DIARY_READ
    ]
    assert events
    assert events[0].payload.days_ago == 3


async def test_ordinary_recall_cannot_reach_the_diary(application) -> None:
    """26.8: 忘れた出来事を通常 Recall が勝手に diary からカンニングしてはならない.

    Structural: the memory subsystem holds no diary repository, so there is no
    path by which a forgotten day could be quietly recovered from the record
    she wrote about it.
    """
    import ast
    from pathlib import Path

    for module in Path("app/memory").glob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any(
            name.startswith("app.diary") or name.endswith("repositories.diary")
            for name in imported
        ), module.name


async def test_an_unwritten_entry_cannot_be_read(application, clock) -> None:
    day = application.diary.begin_day()
    entry = application.diaries.intend(
        life_day_id=day.life_day_id, intended_at=clock.now()
    )

    assert await application.diary.read(entry.diary_id) is None


# --- the debug view ----------------------------------------------------------


async def test_the_diary_command_is_finally_wired(application, clock) -> None:
    """Phase 5 pinned `diary` as the one command answering "not wired yet".
    This is the phase that was supposed to change that."""
    application.diary.begin_day()
    _make_exhausted(application, clock)
    _with_model(application, Writer())
    await application.runtime.tick()

    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} diary", author_id=OWNER, channel_id=CHANNEL
    )

    assert not outcome.result.failed, outcome.result.error
    assert "not wired yet" not in outcome.result.summary
    rows = [row for section in outcome.result.sections for row in section.rows]
    assert rows and rows[0]["status"] in ("written", "late_written")


async def test_the_days_view_reads_life_days(application, clock) -> None:
    application.diary.begin_day()

    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} diary days", author_id=OWNER, channel_id=CHANNEL
    )

    assert not outcome.result.failed, outcome.result.error
    rows = [row for section in outcome.result.sections for row in section.rows]
    assert rows


async def test_reading_the_diary_view_writes_nothing(application, clock) -> None:
    """It is a read-only command, so it must not fire the 26.8 read event —
    an operator looking at the table is not YUI going back over her diary."""
    application.diary.begin_day()
    _make_exhausted(application, clock)
    _with_model(application, Writer())
    await application.runtime.tick()
    before = application.event_store.count()

    for _ in range(5):
        await application.admin_router.route(
            text=f"{ADMIN_PREFIX} diary", author_id=OWNER, channel_id=CHANNEL
        )

    assert application.event_store.count() == before
