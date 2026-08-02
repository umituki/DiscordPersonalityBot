"""What an episode is made of (spec 10.4, patch spec 15.2).

Encoding legitimately reads raw material — that is how a memory forms. Recall
does not: :mod:`app.memory.retrieval` never comes here.

Patch spec 15.2: ``ConversationTranscriptSourceだけに依存しない``. Only
conversations had a source, so a simulated life produced episodes whose
material was always empty, every one of them was discarded as ``no_transcript``,
and nineteen simulated years yielded zero memories. Three kinds of life are
recognised here, and an episode is read by the source that owns its origin:

    real_discord   → ConversationEpisodeSource
    simulated_past → SimulationEpisodeSource
    virtual_life   → VirtualLifeEpisodeSource

Adding a fourth kind of life means adding a source, not touching the engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from app.events.model import Event
from app.events.store import EventStore
from app.memory.models import Episode
from app.storage.repositories.conversations import ConversationRepository

logger = logging.getLogger(__name__)

#: What kind of life the material came from. Kept on the material because the
#: encoding signals differ: a conversation has turns and a share of them are
#: the USER's, a lived day has neither.
MaterialKind = str


@dataclass(frozen=True, slots=True)
class EpisodeMaterial:
    """The raw text of one episode, with whatever structure it had."""

    text: str
    turn_count: int = 0
    user_turn_count: int = 0
    kind: MaterialKind = "conversation"

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class EpisodeMaterialSource(Protocol):
    """Reads the material of episodes from one kind of life."""

    #: Episode origins this source can read.
    origins: frozenset[str]

    def material_for(self, episode: Episode) -> EpisodeMaterial: ...


class ConversationEpisodeSource:
    """Material from the conversation projection."""

    origins = frozenset({"real_discord"})

    def __init__(
        self,
        conversations: ConversationRepository,
        *,
        user_label: str = "USER",
        yui_label: str = "YUI",
    ) -> None:
        self._conversations = conversations
        self._user_label = user_label
        self._yui_label = yui_label

    def material_for(self, episode: Episode) -> EpisodeMaterial:
        if episode.conversation_id is None or not episode.event_ids:
            return EpisodeMaterial(text="")

        wanted = set(episode.event_ids)
        turns = [
            turn
            for turn in self._conversations.recent_turns(
                episode.conversation_id, limit=max(len(wanted) * 2, 20)
            )
            if turn.event_id in wanted
        ]
        if not turns:
            return EpisodeMaterial(text="")

        lines = [turn.as_prompt_line(self._user_label, self._yui_label) for turn in turns]
        user_turns = sum(1 for turn in turns if turn.speaker == "user")
        return EpisodeMaterial(
            text="\n".join(lines),
            turn_count=len(turns),
            user_turn_count=user_turns,
            kind="conversation",
        )


class _EventTextSource:
    """Shared base: material is what the episode's own events said."""

    origins: frozenset[str] = frozenset()
    kind = "events"

    def __init__(self, events: EventStore, *, max_events: int = 40) -> None:
        self._events = events
        self._max_events = max_events

    def material_for(self, episode: Episode) -> EpisodeMaterial:
        lines = [
            line
            for line in (self._line(event) for event in self._events_of(episode))
            if line
        ]
        if not lines:
            return EpisodeMaterial(text="")
        return EpisodeMaterial(
            text="\n".join(lines),
            turn_count=len(lines),
            # Nobody spoke to her: a lived day has no USER share, and the
            # encoding gate must not read one where there is none.
            user_turn_count=0,
            kind=self.kind,
        )

    def _events_of(self, episode: Episode) -> Iterable[Event]:
        for event_id in list(episode.event_ids)[: self._max_events]:
            event = self._events.get(event_id)
            if event is not None:
                yield event

    def _line(self, event: Event) -> str:
        raise NotImplementedError


class SimulationEpisodeSource(_EventTextSource):
    """Material from the simulated past (patch spec 15.2).

    The summary a simulated experience already carries *is* the material.
    Nothing is invented here and nothing is written: this reads the objective
    archive so the Encoding Gate can decide, which is the only legitimate way
    a simulated experience becomes a memory (patch spec 15.3, prohibition 3).
    """

    origins = frozenset({"simulated_past"})
    kind = "simulated_past"

    def _line(self, event: Event) -> str:
        payload = event.payload
        text = str(getattr(payload, "summary", "") or getattr(payload, "text", "") or "")
        return text.strip()


class VirtualLifeEpisodeSource(_EventTextSource):
    """Material from a lived day that no one else was part of."""

    origins = frozenset({"virtual_life"})
    kind = "virtual_life"

    def _line(self, event: Event) -> str:
        payload = event.payload
        for field in ("summary", "text", "description", "activity"):
            value = getattr(payload, field, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""


class CompositeMaterialSource:
    """Routes an episode to the source that owns its origin.

    An origin with no source produces empty material and a warning rather than
    an exception: a missing source degrades encoding for one kind of life, and
    should not take down a run that is mostly another kind.
    """

    origins = frozenset()

    def __init__(self, sources: Sequence[EpisodeMaterialSource] = ()) -> None:
        self._by_origin: dict[str, EpisodeMaterialSource] = {}
        for source in sources:
            self.register(source)

    def register(self, source: EpisodeMaterialSource) -> None:
        for origin in source.origins:
            self._by_origin[origin] = source
        self.origins = frozenset(self._by_origin)

    def material_for(self, episode: Episode) -> EpisodeMaterial:
        source = self._by_origin.get(episode.origin)
        if source is None:
            logger.warning(
                "no episode material source for origin=%s episode_id=%s",
                episode.origin,
                episode.episode_id,
            )
            return EpisodeMaterial(text="")
        return source.material_for(episode)


class StaticMaterialSource:
    """Fixed material, for tests."""

    origins = frozenset({"real_discord", "simulated_past", "virtual_life"})

    def __init__(self, material: EpisodeMaterial) -> None:
        self._material = material

    def material_for(self, episode: Episode) -> EpisodeMaterial:
        return self._material


__all__ = [
    "CompositeMaterialSource",
    "ConversationEpisodeSource",
    "EpisodeMaterial",
    "EpisodeMaterialSource",
    "SimulationEpisodeSource",
    "StaticMaterialSource",
    "VirtualLifeEpisodeSource",
]
