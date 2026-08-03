"""Assembling what is actually known (rebuild spec 15.1).

This reads. It never writes, and it is never a place to put a fallback: a
source that is not wired yet contributes nothing, and contributing nothing is
the safe direction — a claim with no evidence is unsupported, so a missing
source makes the guard stricter, never looser.

GROUND-001 is enforced structurally rather than by a check. The builder's
inputs are repositories and the memories the retriever already returned. There
is no parameter through which a draft, a previous draft, or YUI's own sentences
could arrive, and the objective-event sweep drops her own utterances on the way
in: ``YUI_MESSAGE_SENT`` is evidence that she spoke, never that what she said
was so.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Sequence

from app.clock import Clock, SystemClock
from app.grounding.models import Evidence, GroundingContext

logger = logging.getLogger(__name__)

#: Event types that are YUI's own speech. Objectively they happened; what they
#: assert did not thereby become true (GROUND-001).
SELF_AUTHORED_EVENT_TYPES = frozenset(
    {"YUI_MESSAGE_SENT", "YUI_REPLY_SUPPRESSED", "SIMULATED_EXPERIENCE"}
)

#: Event types that carry an authoritative fact about what happened.
FACTUAL_EVENT_CATEGORIES = frozenset({"world", "action", "social", "knowledge"})

DEFAULT_EVENT_LIMIT = 40
DEFAULT_SEMANTIC_LIMIT = 30
DEFAULT_GOAL_LIMIT = 10


class GroundingContextBuilder:
    """Builds the §15.1 context from the rows other subsystems own.

    Every source is optional. A phase that has not landed yet simply has no
    repository to pass, and the corresponding section stays empty.
    """

    def __init__(
        self,
        *,
        events: Any | None = None,
        activities: Any | None = None,
        memories: Any | None = None,
        beliefs: Any | None = None,
        goals: Any | None = None,
        tools: Any | None = None,
        npcs: Any | None = None,
        state: Any | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._events = events
        self._activities = activities
        self._memories = memories
        self._beliefs = beliefs
        self._goals = goals
        self._tools = tools
        self._npcs = npcs
        self._state = state
        self._clock = clock or SystemClock()

    def build(
        self,
        *,
        now: datetime | None = None,
        recalled: Sequence[Any] = (),
        run_id: str | None = None,
    ) -> GroundingContext:
        moment = now or self._clock.now()
        return GroundingContext(
            current_world=self._world_state(),
            current_activity=self._current_activity(),
            completed_activities_today=self._completed_today(moment),
            recent_objective_events=self._objective_events(),
            recalled_subjective_memories=_from_recalled(recalled),
            verified_user_facts=self._user_facts(),
            known_semantic_memories=self._semantic_memories(),
            successful_tool_calls=self._tool_calls(run_id),
            npc_interactions=self._npc_interactions(),
            current_goals=self._goals_in_progress(),
            # Phase 10 owns the diary. Until then nothing has been read, which
            # is exactly what an empty tuple says.
            explicitly_read_diary_entries=(),
            built_at=moment,
        )

    # --- sections -----------------------------------------------------------
    def _world_state(self) -> tuple[Evidence, ...]:
        if self._state is None:
            return ()
        found: list[Evidence] = []
        for key in ("location", "weather", "season", "time_of_day"):
            value = _safe(lambda: self._state.get("world", key))
            if value is None:
                continue
            text = getattr(value, "value", value)
            if text in (None, ""):
                continue
            found.append(
                Evidence(
                    kind="world_state",
                    reference=f"world.{key}",
                    summary=str(text),
                    subject="world",
                )
            )
        return tuple(found)

    def _current_activity(self) -> tuple[Evidence, ...]:
        if self._activities is None:
            return ()
        activity = _safe(self._activities.ongoing)
        if activity is None:
            return ()
        return (
            Evidence(
                kind="activity",
                reference=activity.activity_id,
                summary=activity.name,
                occurred_at=activity.started_at,
                subject="yui",
            ),
        )

    def _completed_today(self, now: datetime) -> tuple[Evidence, ...]:
        if self._activities is None:
            return ()
        completed = _safe(lambda: self._activities.completed(limit=50)) or ()
        since = now - timedelta(hours=24)
        return tuple(
            Evidence(
                kind="activity",
                reference=activity.activity_id,
                summary=f"{activity.name}{'' if activity.outcome is None else ' ' + activity.outcome}",
                occurred_at=activity.ended_at,
                subject="yui",
            )
            # ``has_happened`` is the spec 18.2 distinction: a plan is not a
            # completed event, and only a completed event grounds 「した」.
            for activity in completed
            if activity.has_happened and activity.ended_at is not None
            and activity.ended_at >= since
        )

    def _objective_events(self) -> tuple[Evidence, ...]:
        if self._events is None:
            return ()
        events = _safe(lambda: self._events.recent(limit=DEFAULT_EVENT_LIMIT)) or ()
        found: list[Evidence] = []
        for event in events:
            if event.event_type in SELF_AUTHORED_EVENT_TYPES:
                continue  # GROUND-001
            if event.category not in FACTUAL_EVENT_CATEGORIES:
                continue
            text = getattr(event.payload, "text", None)
            summary = str(text).strip() if isinstance(text, str) and text.strip() else event.event_type
            found.append(
                Evidence(
                    kind="objective_event",
                    reference=event.event_id,
                    summary=summary,
                    occurred_at=event.occurred_at,
                    # Whose doing this records. A USER message is a fact about
                    # the USER; treating it as a fact about YUI is how 「小説
                    # 読むの好き」 came to support 「昨日わたしも小説を読んだよ」.
                    subject=_subject_of(event),
                )
            )
        return tuple(found)

    def _user_facts(self) -> tuple[Evidence, ...]:
        if self._beliefs is None:
            return ()
        held = _safe(lambda: self._beliefs.held(subject="user", limit=30)) or ()
        return tuple(
            Evidence(
                kind="verified_user_fact",
                reference=belief.belief_id,
                summary=belief.statement,
                occurred_at=belief.updated_at,
                subject="user",
            )
            for belief in held
        )

    def _semantic_memories(self) -> tuple[Evidence, ...]:
        if self._memories is None:
            return ()
        known = _safe(
            lambda: self._memories.active_semantic(limit=DEFAULT_SEMANTIC_LIMIT)
        ) or ()
        return tuple(
            Evidence(
                kind="semantic_memory",
                reference=item.semantic_id,
                summary=item.statement,
                occurred_at=item.updated_at,
            )
            for item in known
        )

    def _tool_calls(self, run_id: str | None) -> tuple[Evidence, ...]:
        if self._tools is None:
            return ()
        call_ids = _safe(lambda: self._tools.successful_call_ids(run_id=run_id)) or ()
        return tuple(
            Evidence(
                kind="tool_call",
                reference=str(call_id),
                summary=str(call_id),
                subject="yui",
            )
            for call_id in call_ids
        )

    def _npc_interactions(self) -> tuple[Evidence, ...]:
        if self._npcs is None:
            return ()
        npcs = _safe(lambda: self._npcs.all(limit=50)) or ()
        return tuple(
            Evidence(kind="npc_interaction", reference=npc.npc_id, summary=npc.name)
            for npc in npcs
        )

    def _goals_in_progress(self) -> tuple[Evidence, ...]:
        if self._goals is None:
            return ()
        goals = _safe(lambda: self._goals.active(limit=DEFAULT_GOAL_LIMIT)) or ()
        return tuple(
            Evidence(kind="goal", reference=goal.goal_id, summary=goal.description)
            for goal in goals
        )


#: Which actor an event's doing belongs to.
_ACTOR_SUBJECTS: dict[str, str] = {
    "yui": "yui",
    "user": "user",
    "npc": "other",
    "admin": "user",
    "world": "world",
    "system": "world",
}


def _subject_of(event: Any) -> str:
    return _ACTOR_SUBJECTS.get(str(getattr(event, "actor_type", "")), "unknown")


def _from_recalled(recalled: Sequence[Any]) -> tuple[Evidence, ...]:
    """The memories the retriever already returned for this turn.

    These are subjective: they ground 「覚えてる」, not 「今日やった」. The
    accepted-evidence table is what keeps that straight.
    """
    found: list[Evidence] = []
    for candidate in recalled:
        memory = getattr(candidate, "memory", candidate)
        summary = getattr(memory, "summary", None)
        if not summary:
            continue
        found.append(
            Evidence(
                kind="subjective_memory",
                reference=getattr(memory, "memory_id", ""),
                summary=str(summary),
                occurred_at=getattr(memory, "occurred_at", None),
                subject="yui",
            )
        )
    return tuple(found)


def _safe(read):
    """A source that fails must not take the reply down with it.

    It makes the guard stricter, not looser: the section ends up empty, so
    nothing it would have grounded can be claimed.
    """
    try:
        return read()
    except Exception:  # noqa: BLE001 - a broken read is a missing section
        logger.exception("grounding source failed; treating it as empty")
        return None


__all__ = ["GroundingContextBuilder", "SELF_AUTHORED_EVENT_TYPES"]
