"""Writing the diary (rebuild spec 26 — Phase 10).

The bedtime flow of 26.3, with the one constraint that shapes all of it::

    ただし LLM failure が睡眠を永久に阻害してはならない。

A model that hangs must not keep her awake. That single sentence rules out the
obvious design — generate, then sleep — and forces the order used here: the
entry is marked *owed* before anything is generated, generation is attempted
with a bound, and sleep proceeds either way. A retry later produces a
``late_written`` entry that still carries the bedtime it was meant for.

26.6 is the other rule worth holding on to. The diary is authoritative about
one thing only: **that she wrote it**. What she wrote is her reading of the day,
and a reading can be wrong. Nothing downstream may treat diary prose as a fact
about the world, which is why nothing here writes to memory, and why 26.7
allows only a light practice on the memories she actually looked at.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.clock import Clock, SystemClock
from app.diary.events import (
    BEDTIME_REFLECTION_STARTED,
    DIARY_READ,
    DIARY_WRITTEN,
    BedtimeReflectionStartedPayload,
    DiaryReadPayload,
    DiaryWrittenPayload,
)
from app.diary.models import (
    FELT_TO_MOOD,
    DiaryDraft,
    DiaryEntry,
    DiaryReference,
    LifeDay,
)
from app.events.model import Event
from app.llm.types import LLMMessage

logger = logging.getLogger(__name__)

MODULE = "diary_service"
DIARY_PROMPT = "diary"

#: 26.3: ``diary attempt has bounded timeout``. Generous, because a diary is
#: not on anybody's critical path — and bounded, because sleep is.
GENERATION_TIMEOUT_S = 45.0

#: 26.4: 重要度で圧縮し、全 Event dump はしない.
MAX_ACTIVITIES = 8
MAX_CONVERSATIONS = 6
MAX_INTERACTIONS = 5
MAX_MEMORIES = 6


@dataclass(frozen=True, slots=True)
class DailyDiaryContext:
    """What the day looked like, compressed (26.4).

    Frozen because 26.3 says to freeze it at bedtime: a retry that ran at four
    in the morning must write about the day she meant to write about, not about
    whatever the database looks like by the time the retry happens.
    """

    life_day_id: str
    intended_at: datetime
    hours_awake: float = 0.0
    activities: tuple[str, ...] = ()
    conversations: tuple[str, ...] = ()
    interactions: tuple[str, ...] = ()
    recalled: tuple[tuple[str, str], ...] = ()
    goals: tuple[str, ...] = ()
    mood_valence: float = 0.0
    emotion_peak: str = ""
    references: tuple[DiaryReference, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        """A day where nothing happened. 26.5 says that is allowed to be short."""
        return not (
            self.activities or self.conversations or self.interactions or self.goals
        )

    def render(self) -> str:
        """The prose block the prompt gets. Never a dump of every event."""
        blocks: list[str] = []
        if self.activities:
            blocks.append("したこと:\n" + "\n".join(f"- {item}" for item in self.activities))
        if self.conversations:
            blocks.append("話したこと:\n" + "\n".join(f"- {item}" for item in self.conversations))
        if self.interactions:
            blocks.append("会った人:\n" + "\n".join(f"- {item}" for item in self.interactions))
        if self.goals:
            blocks.append("進めたこと:\n" + "\n".join(f"- {item}" for item in self.goals))
        if self.recalled:
            blocks.append(
                "思い出したこと:\n"
                + "\n".join(f"- {summary}" for _, summary in self.recalled)
            )
        if not blocks:
            return "とくに何もなかった。"
        return "\n\n".join(blocks)


class DiaryContextBuilder:
    """26.4. Reads widely, writes nothing, and compresses by importance."""

    name = "diary_context_builder"

    def __init__(
        self,
        *,
        world: Any,
        conversations: Any = None,
        society: Any = None,
        memories: Any = None,
        goals: Any = None,
        state: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._world = world
        self._conversations = conversations
        self._society = society
        self._memories = memories
        self._goals = goals
        self._state = state
        self._clock = clock or SystemClock()

    def build(self, day: LifeDay, *, now: datetime | None = None) -> DailyDiaryContext:
        moment = now or self._clock.now()
        references: list[DiaryReference] = []

        activities: list[str] = []
        for activity in self._world.completed_activities(limit=MAX_ACTIVITIES * 3):
            if activity.ended_at is None or activity.ended_at < day.started_at:
                continue
            activities.append(f"{activity.name}（{activity.outcome or '—'}）")
            references.append(
                DiaryReference(reference_type="activity", reference_id=activity.activity_id)
            )
            if len(activities) >= MAX_ACTIVITIES:
                break

        interactions: list[str] = []
        if self._society is not None:
            for interaction in self._society.recent_interactions(limit=MAX_INTERACTIONS * 3):
                if interaction.occurred_at < day.started_at:
                    continue
                interactions.append(interaction.summary or interaction.kind)
                references.append(
                    DiaryReference(reference_type="npc", reference_id=interaction.npc_id)
                )
                if len(interactions) >= MAX_INTERACTIONS:
                    break

        recalled: list[tuple[str, str]] = []
        if self._memories is not None:
            for memory in self._memories.recent_memories(limit=MAX_MEMORIES * 2):
                if memory.occurred_at < day.started_at:
                    continue
                recalled.append((memory.memory_id, memory.summary))
                references.append(
                    DiaryReference(reference_type="memory", reference_id=memory.memory_id)
                )
                if len(recalled) >= MAX_MEMORIES:
                    break

        goals: list[str] = []
        if self._goals is not None:
            for goal in self._goals.active_goals(limit=5):
                if goal.last_pursued_at is None or goal.last_pursued_at < day.started_at:
                    continue
                goals.append(goal.description)
                references.append(
                    DiaryReference(reference_type="goal", reference_id=goal.goal_id)
                )

        conversations: list[str] = []
        if self._conversations is not None:
            conversations = self._recent_conversation(day)

        return DailyDiaryContext(
            life_day_id=day.life_day_id,
            intended_at=moment,
            hours_awake=day.hours(moment),
            activities=tuple(activities),
            conversations=tuple(conversations),
            interactions=tuple(interactions),
            recalled=tuple(recalled),
            goals=tuple(goals),
            mood_valence=self._number("mood", "valence"),
            emotion_peak=self._peak_emotion(),
            references=tuple(references),
        )

    # --- internals -----------------------------------------------------------
    def _recent_conversation(self, day: LifeDay) -> list[str]:
        try:
            turns = self._conversations.recent_turns_across(limit=MAX_CONVERSATIONS * 2)
        except AttributeError:
            return []
        except Exception:  # noqa: BLE001
            logger.exception("could not read the day's conversation")
            return []
        lines: list[str] = []
        for turn in turns:
            if turn.occurred_at < day.started_at:
                continue
            who = "わたし" if turn.speaker == "yui" else "相手"
            lines.append(f"{who}: {turn.content[:80]}")
            if len(lines) >= MAX_CONVERSATIONS:
                break
        return lines

    def _number(self, domain: str, key: str, default: float = 0.0) -> float:
        if self._state is None:
            return default
        entry = self._state.get(domain, key)
        return default if entry is None or entry.numeric is None else float(entry.numeric)

    def _peak_emotion(self) -> str:
        if self._state is None:
            return ""
        try:
            values = self._state.list_domain("emotion")
        except Exception:  # noqa: BLE001
            return ""
        strongest = max(
            (value for value in values if value.numeric is not None),
            key=lambda value: float(value.numeric or 0.0),
            default=None,
        )
        return "" if strongest is None else strongest.key


class DiaryService:
    """The bedtime flow, and the deliberate act of reading back (26.3, 26.8)."""

    name = MODULE

    def __init__(
        self,
        *,
        days: Any,
        diaries: Any,
        builder: DiaryContextBuilder,
        processor: Any = None,
        structured: Any = None,
        prompts: Any = None,
        memory: Any = None,
        clock: Clock | None = None,
        timeout_s: float = GENERATION_TIMEOUT_S,
    ) -> None:
        self._days = days
        self._diaries = diaries
        self._builder = builder
        self._processor = processor
        self._structured = structured
        self._prompts = prompts
        self._memory = memory
        self._clock = clock or SystemClock()
        self._timeout = timeout_s

    # --- 26.2: the day she is living ----------------------------------------
    def begin_day(self, *, sleep_episode_id: str | None = None) -> LifeDay:
        return self._days.open_day(
            now=self._clock.now(), wake_sleep_id=sleep_episode_id
        )

    def current_day(self) -> LifeDay | None:
        return self._days.current()

    # --- 26.3: bedtime -------------------------------------------------------
    async def reflect_at_bedtime(
        self, *, sleep_episode_id: str | None = None
    ) -> DiaryEntry | None:
        """Look back at the day. Never blocks going to sleep.

        The entry is marked owed *before* the model is asked, so a generation
        that hangs or dies still leaves something for the retry to find. The
        caller goes to sleep whatever this returns, including ``None``.
        """
        day = self._days.current()
        if day is None:
            return None

        context = self._builder.build(day)
        entry = self._diaries.intend(
            life_day_id=day.life_day_id,
            intended_at=context.intended_at,
            sleep_episode_id=sleep_episode_id,
        )
        if entry.is_written:
            return entry

        await self._announce(day, context)
        written = await self._generate(entry, context, late=False)
        self._days.close_day(
            day.life_day_id,
            now=self._clock.now(),
            sleep_sleep_id=sleep_episode_id,
        )
        return written

    async def retry_pending(self, *, limit: int = 3) -> int:
        """26.3: retry during sleep/background.

        A late entry keeps the bedtime it was meant for and is recorded as
        ``late_written``, because "written at the time" and "written eventually"
        are different facts about how much of the day she still had in mind.
        """
        written = 0
        for entry in self._diaries.pending(limit=limit):
            day = self._days.get(entry.life_day_id)
            if day is None:
                continue
            context = self._builder.build(day, now=entry.intended_at)
            result = await self._generate(entry, context, late=True)
            if result is not None and result.is_written:
                written += 1
        return written

    # --- 26.8: reading it back ----------------------------------------------
    async def read(self, diary_id: str) -> DiaryEntry | None:
        """A deliberate act, with its own event and its own consequences.

        Ordinary recall never comes through here. 26.8 is explicit that a
        forgotten day must stay forgotten unless she goes and looks it up —
        otherwise the diary becomes a backdoor that quietly undoes forgetting.
        """
        entry = self._diaries.get(diary_id)
        if entry is None or not entry.is_written:
            return None
        days_ago = max(
            0, int((self._clock.now() - entry.intended_at).total_seconds() // 86400)
        )
        await self._emit(
            DIARY_READ,
            DiaryReadPayload(
                diary_id=entry.diary_id,
                life_day_id=entry.life_day_id,
                summary=entry.summary,
                days_ago=days_ago,
            ),
        )
        return entry

    # --- internals -----------------------------------------------------------
    async def _generate(
        self, entry: DiaryEntry, context: DailyDiaryContext, *, late: bool
    ) -> DiaryEntry | None:
        draft = await self._draft(context)
        if draft is None or not draft.content.strip():
            self._diaries.record_attempt(entry.diary_id)
            logger.info("diary attempt produced nothing; it stays pending")
            return None

        valence, arousal = FELT_TO_MOOD.get(draft.felt, (0.0, 0.3))
        written = self._diaries.write(
            entry.diary_id,
            content=draft.content.strip(),
            summary=(draft.summary or draft.content[:80]).strip(),
            importance=0.2 if context.is_empty else 0.5,
            mood_valence=valence,
            mood_arousal=arousal,
            prompt_version=self._prompt_version(),
            model_version="",
            now=self._clock.now(),
            late=late,
        )
        mentioned = frozenset(draft.about_memory_ids)
        self._diaries.add_references(
            entry.diary_id,
            tuple(
                reference.model_copy(
                    update={
                        "mentioned_in_text": reference.reference_type == "memory"
                        and reference.reference_id in mentioned
                    }
                )
                for reference in context.references
            ),
        )
        await self._practise(written)
        await self._emit(
            DIARY_WRITTEN,
            DiaryWrittenPayload(
                diary_id=written.diary_id,
                life_day_id=written.life_day_id,
                summary=written.summary,
                late=late,
                attempts=written.attempts,
                referenced_memories=len(written.mentioned),
            ),
        )
        return written

    async def _draft(self, context: DailyDiaryContext) -> DiaryDraft | None:
        if self._structured is None or self._prompts is None:
            return None
        try:
            template = self._prompts.get(DIARY_PROMPT)
            content = template.render(
                day=context.render(),
                hours_awake=f"{context.hours_awake:.1f}",
                mood=f"{context.mood_valence:+.2f}",
                emotion=context.emotion_peak or "-",
            )
            outcome = await asyncio.wait_for(
                self._structured.generate(
                    DiaryDraft,
                    (LLMMessage(role="user", content=content),),
                    purpose=DIARY_PROMPT,
                    temperature=0.85,
                    max_tokens=800,
                    prompt_id=DIARY_PROMPT,
                    prompt_version=template.prompt_version,
                ),
                timeout=self._timeout,
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            # 26.3. Whatever went wrong, sleep proceeds and the entry is owed.
            logger.warning("diary generation failed; it stays pending", exc_info=True)
            return None
        if not getattr(outcome, "ok", True):
            return None
        return outcome.value

    async def _practise(self, entry: DiaryEntry) -> None:
        """26.7: 日記で実際に振り返った memory だけ軽い recall practice.

        Only what she wrote about, and only lightly. The diary's *text* is
        never copied into memory — that would turn her account of a day into
        the day itself.
        """
        if self._memory is None:
            return
        for reference in entry.mentioned:
            if reference.reference_type != "memory":
                continue
            try:
                self._memory.practise_lightly(reference.reference_id)
            except AttributeError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("diary practice failed for %s", reference.reference_id)

    async def _announce(self, day: LifeDay, context: DailyDiaryContext) -> None:
        await self._emit(
            BEDTIME_REFLECTION_STARTED,
            BedtimeReflectionStartedPayload(
                life_day_id=day.life_day_id,
                intended_at=context.intended_at.isoformat(),
                hours_awake=round(context.hours_awake, 2),
            ),
        )

    async def _emit(self, event_type: str, payload: Any) -> None:
        if self._processor is None:
            return
        event = Event.create(
            event_type=event_type,
            category="internal",
            actor_type="yui",
            source_type=MODULE,
            origin="virtual_life",
            priority="P4",
            payload=payload,
            clock=self._clock,
        )
        await self._processor.process(event)

    def _prompt_version(self) -> str:
        if self._prompts is None:
            return ""
        try:
            return self._prompts.get(DIARY_PROMPT).prompt_version
        except Exception:  # noqa: BLE001
            return ""


__all__ = [
    "GENERATION_TIMEOUT_S",
    "DailyDiaryContext",
    "DiaryContextBuilder",
    "DiaryService",
]
