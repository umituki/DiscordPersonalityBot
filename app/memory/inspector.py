"""Debug inspection of retrieval (Phase 2 §2Q, Debug Inspector).

``!yui memory find 本`` has to be able to show why a memory did or did not come
up — which stage stopped it, what its accessibility was, whether it was
selected — without any of that inspection changing what would happen next time.

A ``practise=False`` flag on ``recall()`` was considered and rejected. The
danger is not today's code; it is that ``recall()`` will keep growing, and one
future line that writes unconditionally turns every debug search into silent
practice. That is exactly how the original defect arrived. So the preview is a
different object with no writer at all: it holds the repository for reads and
never calls a method that writes, which is a property somebody can check by
looking rather than by remembering.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from app.clock import Clock, SystemClock
from app.memory.recall_mode import RecallMode, classify
from app.memory.recall_models import RetrievalReport
from app.memory.retrieval import MemoryRetriever


class MemoryInspector:
    """Read-only view of what retrieval would do (§2Q)."""

    name = "memory_inspector"

    def __init__(
        self, retriever: MemoryRetriever, *, clock: Clock | None = None
    ) -> None:
        self._retriever = retriever
        self._clock = clock or SystemClock()

    async def preview_retrieval(
        self,
        query_text: str,
        *,
        mode: RecallMode | None = None,
        now: datetime | None = None,
        origins: Sequence[str] | None = None,
        cues: Sequence[str] = (),
    ) -> RetrievalReport:
        """Run Stages 1-3 and report. Writes nothing, ever.

        No ``run_id`` or ``event_id`` is passed on: a debug search is not part
        of a run, and attaching it to one would put it in the trace as though
        the conversation had done it.
        """
        return await self._retriever.retrieve(
            query_text,
            mode=mode or classify(query_text),
            now=now or self._clock.now(),
            origins=origins,
            cues=cues,
        )

    async def describe(
        self,
        query_text: str,
        *,
        mode: RecallMode | None = None,
        now: datetime | None = None,
    ) -> str:
        """The human-readable inspector output."""
        report = await self.preview_retrieval(query_text, mode=mode, now=now)
        return report.describe()


__all__ = ["MemoryInspector"]
