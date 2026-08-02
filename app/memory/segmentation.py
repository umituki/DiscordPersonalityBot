"""Episode segmentation (spec 8.4, 10.4).

A message is not a memory. Events are grouped into episodes first, and only an
episode can become a memory. Boundaries are Python rules — silence, size,
duration, a different conversation — not a model's opinion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.memory.models import Episode
from app.memory.policy import SegmentationPolicy


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    close: bool
    reason: str | None = None

    @classmethod
    def keep_open(cls) -> BoundaryDecision:
        return cls(close=False)


class EpisodeSegmenter:
    def __init__(self, policy: SegmentationPolicy) -> None:
        self._policy = policy

    def decide(
        self,
        episode: Episode,
        *,
        now: datetime,
        next_event_at: datetime | None = None,
        next_conversation_id: str | None = None,
        shutting_down: bool = False,
    ) -> BoundaryDecision:
        """Should this open episode be closed before the next event joins it?"""
        if episode.status != "open":
            return BoundaryDecision.keep_open()

        if shutting_down:
            return BoundaryDecision(True, "shutdown")

        last_activity = episode.ended_at or episode.started_at
        reference = next_event_at or now
        gap = reference - last_activity
        if gap >= timedelta(minutes=self._policy.max_gap_minutes):
            return BoundaryDecision(True, "silence_gap")

        if next_conversation_id is not None and episode.conversation_id is not None:
            if next_conversation_id != episode.conversation_id:
                return BoundaryDecision(True, "conversation_changed")

        if episode.event_count >= self._policy.max_events:
            return BoundaryDecision(True, "max_events")

        if reference - episode.started_at >= timedelta(
            minutes=self._policy.max_duration_minutes
        ):
            return BoundaryDecision(True, "max_duration")

        return BoundaryDecision.keep_open()

    def is_encodable(self, episode: Episode) -> bool:
        """Too small an episode is not worth remembering as an episode."""
        return episode.event_count >= self._policy.min_events_to_encode
