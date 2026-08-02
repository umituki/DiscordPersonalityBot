"""Transcripts for encoding (spec 10.4).

Encoding legitimately reads raw material — that is how a memory forms. Recall
does not: :mod:`app.memory.retrieval` never comes here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.memory.models import Episode
from app.storage.repositories.conversations import ConversationRepository


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    turn_count: int = 0
    user_turn_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class TranscriptSource(Protocol):
    def transcript_for(self, episode: Episode) -> Transcript: ...


class ConversationTranscriptSource:
    """Builds an episode transcript from the conversation projection."""

    def __init__(self, conversations: ConversationRepository, *, user_label: str = "USER",
                 yui_label: str = "YUI") -> None:
        self._conversations = conversations
        self._user_label = user_label
        self._yui_label = yui_label

    def transcript_for(self, episode: Episode) -> Transcript:
        if episode.conversation_id is None or not episode.event_ids:
            return Transcript(text="")

        wanted = set(episode.event_ids)
        turns = [
            turn
            for turn in self._conversations.recent_turns(
                episode.conversation_id, limit=max(len(wanted) * 2, 20)
            )
            if turn.event_id in wanted
        ]
        if not turns:
            return Transcript(text="")

        lines = [turn.as_prompt_line(self._user_label, self._yui_label) for turn in turns]
        user_turns = sum(1 for turn in turns if turn.speaker == "user")
        return Transcript(
            text="\n".join(lines), turn_count=len(turns), user_turn_count=user_turns
        )


class StaticTranscriptSource:
    """Fixed transcript, for tests and for non-conversational episodes."""

    def __init__(self, transcript: Transcript) -> None:
        self._transcript = transcript

    def transcript_for(self, episode: Episode) -> Transcript:
        return self._transcript
