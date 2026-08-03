"""Durable cue -> decision -> associative recall runtime slice (spec 17.6).

The three authorities stay separate:

* the source notices objective cues and claims a durable offer window;
* the candidate builder only estimates whether recalling now is worthwhile;
* the selected action alone retrieves, practises and emits an event.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from app.agency.models import ActionCandidate
from app.clock import Clock, SystemClock
from app.events.model import Event
from app.memory.events import (
    MEMORY_SPONTANEOUSLY_RECALLED,
    MemorySpontaneouslyRecalledPayload,
)
from app.world.models import Opportunity

SPONTANEOUS_RECALL = "spontaneous_recall"
RECALL_FROM_CUE = "recall_from_cue"
OFFER_COOLDOWN = timedelta(hours=6)
CUE_MAX_AGE = timedelta(days=7)


class SpontaneousMemorySource:
    """Notice completed-world cues; never retrieve or create an event."""

    name = "spontaneous_memory"

    def __init__(
        self,
        *,
        cues: Any,
        activities: Any,
        interactions: Any,
        world: Any,
        clock: Clock | None = None,
        cooldown: timedelta = OFFER_COOLDOWN,
        max_age: timedelta = CUE_MAX_AGE,
    ) -> None:
        self._cues = cues
        self._activities = activities
        self._interactions = interactions
        self._world = world
        self._clock = clock or SystemClock()
        self._cooldown = cooldown
        self._max_age = max_age

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        if self._world.current_sleep() is not None:
            return ()
        if self._world.current_activity() is not None:
            return ()

        opportunities: list[Opportunity] = []
        for activity in self._activities.completed(limit=20):
            occurred = activity.ended_at or activity.started_at
            if now - occurred > self._max_age:
                continue
            cue = self._cues.offer(
                cue_type="completed_activity",
                source_kind="activity",
                source_id=activity.activity_id,
                detail=activity.name,
                cues=_unique(
                    (activity.name, activity.kind, activity.location or "", activity.outcome or "")
                ),
                salience=0.65 if activity.outcome else 0.45,
                now=now,
                cooldown=self._cooldown,
            )
            if cue is not None:
                opportunities.append(_opportunity(cue, now))

        for interaction in self._interactions.recent(limit=20):
            if now - interaction.occurred_at > self._max_age:
                continue
            cue = self._cues.offer(
                cue_type="npc_interaction",
                source_kind="npc_interaction",
                source_id=interaction.interaction_id,
                detail=interaction.summary,
                cues=_unique((interaction.summary, interaction.kind, interaction.npc_id)),
                salience=max(0.35, min(0.9, 0.45 + abs(float(interaction.valence)) * 0.4)),
                now=now,
                cooldown=self._cooldown,
            )
            if cue is not None:
                opportunities.append(_opportunity(cue, now))
        return tuple(opportunities)

    def next_due(self, now: datetime) -> datetime | None:
        # New world facts are discovered on the runtime's ordinary idle wake.
        # Existing cues enforce their own durable offer window in ``offer``.
        return None


class SpontaneousMemoryCandidates:
    """Read-only value estimate. It cannot reach MemoryEngine."""

    name = "spontaneous_memory_candidates"

    def __init__(self, *, cues: Any, world: Any, state: Any) -> None:
        self._cues = cues
        self._world = world
        self._state = state

    def build(self, opportunity: Opportunity, now: datetime) -> ActionCandidate | None:
        if self._world.current_sleep() is not None or self._world.current_activity() is not None:
            return None
        cue = self._cues.get(opportunity.detail)
        if cue is None or cue.consumed_at is not None:
            return None
        fatigue_entry = self._state.get("world", "mental_fatigue")
        fatigue = 0.0 if fatigue_entry is None else float(fatigue_entry.numeric or 0.0)
        emotion = max(
            (float(entry.numeric or 0.0) for entry in self._state.list_domain("emotion")),
            default=0.0,
        )
        return ActionCandidate(
            action=RECALL_FROM_CUE,
            route="reactive",
            expected_value=round(
                max(
                    0.05,
                    min(0.95, 0.18 + cue.salience * 0.7 + emotion * 0.1 - fatigue * 0.15),
                ),
                6,
            ),
            reason=cue.cue_id,
        )


class SpontaneousMemoryActions:
    """The sole runtime authority that turns an offered cue into a recall."""

    name = "spontaneous_memory_actions"

    def __init__(
        self,
        *,
        cues: Any,
        memory: Any,
        processor: Any,
        clock: Clock | None = None,
    ) -> None:
        self._cues = cues
        self._memory = memory
        self._processor = processor
        self._clock = clock or SystemClock()

    async def recall(self, candidate: ActionCandidate, now: datetime) -> bool:
        cue = self._cues.get(candidate.reason)
        if cue is None or cue.consumed_at is not None:
            return False

        report = await self._memory.associate(cue.cues, now=now)
        if not report.candidates:
            return self._no_recall(cue.cue_id, report, now, "no_candidates")
        if not report.passed_ids:
            return self._no_recall(cue.cue_id, report, now, "no_relevant_memory")
        if not report.selected:
            return self._no_recall(cue.cue_id, report, now, "relevant_but_inaccessible")

        practised = self._memory.mark_spontaneously_recalled(report, now=now)
        if not practised:
            return self._no_recall(cue.cue_id, report, now, "practice_already_committed")

        selected = tuple(item for item in report.selected if item.memory_id in practised)
        availability = tuple(item.availability for item in selected)
        event = Event.create(
            event_type=MEMORY_SPONTANEOUSLY_RECALLED,
            category="internal",
            actor_type="yui",
            target_type="yui",
            source_type=self.name,
            source_id=cue.cue_id,
            origin="virtual_life",
            occurred_at=now,
            clock=self._clock,
            objective=False,
            priority="P4",
            payload=MemorySpontaneouslyRecalledPayload(
                cue_id=cue.cue_id,
                cue_types=(cue.cue_type,),
                retrieval_group_id=report.group_id,
                primary_memory_id=selected[0].memory_id,
                memory_ids=tuple(item.memory_id for item in selected),
                selected_count=len(selected),
                minimum_availability=min(availability),
                maximum_availability=max(availability),
            ),
        )
        outcome = await self._processor.process(event)
        self._cues.complete(
            cue.cue_id,
            outcome="recalled" if outcome.status == "committed" else "processing_failed",
            reason=f"processor={outcome.status}; practised={len(practised)}",
            now=now,
            retrieval_group_id=report.group_id,
            primary_memory_id=selected[0].memory_id,
            event_id=event.event_id,
        )
        return outcome.status == "committed"

    def _no_recall(self, cue_id: str, report: Any, now: datetime, reason: str) -> bool:
        self._cues.complete(
            cue_id,
            outcome="no_recall",
            reason=reason,
            now=now,
            retrieval_group_id=report.group_id or None,
        )
        return False

    def register(
        self,
        registry: Any,
        *,
        source: SpontaneousMemorySource,
        candidates: SpontaneousMemoryCandidates,
    ) -> None:
        registry.add_source(source)
        registry.add_builder(SPONTANEOUS_RECALL, candidates.build)
        registry.add_handler(RECALL_FROM_CUE, self.recall)


def _opportunity(cue: Any, now: datetime) -> Opportunity:
    return Opportunity(
        kind=SPONTANEOUS_RECALL,
        detail=cue.cue_id,
        urgency=cue.salience,
        created_at=now,
    )


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip()))


__all__ = [
    "RECALL_FROM_CUE",
    "SPONTANEOUS_RECALL",
    "SpontaneousMemoryActions",
    "SpontaneousMemoryCandidates",
    "SpontaneousMemorySource",
]
