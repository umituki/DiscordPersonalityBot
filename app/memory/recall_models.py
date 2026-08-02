"""What happens to a memory on its way to a reply (rebuild spec 17.5, §2J).

The old code had one word for five different things. A memory that matched a
full-text query was "retrieved"; being retrieved marked it used; being used
practised it. So searching strengthened memories, strengthened memories scored
higher, and higher-scoring memories were searched into every conversation. The
production database ended with memories pinned near accessibility 1.0 that had
never once been the answer to anything.

Five states, and the transitions between them are the whole fix::

    candidate            it was found. Nothing else is true of it yet.
    relevance_passed     Stage 2 judged it actually about the query.
    selected             Stage 3 judged it reachable, and put it in context.
    consciously_recalled she was trying to remember, and this is what came.
    used_in_reply        the reply she sent actually rests on it.

Only the last two practise (§2K). Being a candidate is worth zero, forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.memory.models import EpisodicMemory
from app.memory.recall_mode import RecallMode

#: Rebuild spec 17.5. Ordered from least to most committed.
RetrievalState = Literal[
    "candidate",
    "relevance_passed",
    "selected",
    "consciously_recalled",
    "used_in_reply",
]

RETRIEVAL_STATES: tuple[RetrievalState, ...] = (
    "candidate",
    "relevance_passed",
    "selected",
    "consciously_recalled",
    "used_in_reply",
)

#: States that mean the memory was actually remembered, not merely found.
#: These and only these practise (§2K).
PRACTISING_STATES: frozenset[str] = frozenset({"consciously_recalled", "used_in_reply"})

#: Rebuild spec 17.3 Stage 2 (§2E). A category, not a decimal: the model is
#: being asked a judgement it can defend, and a closed set removes the room
#: where a 0.31 quietly becomes a recall.
RelevanceLabel = Literal["irrelevant", "weak", "relevant", "strong"]

RELEVANCE_LABELS: tuple[RelevanceLabel, ...] = (
    "irrelevant",
    "weak",
    "relevant",
    "strong",
)

#: §2E: ``irrelevant → reject``, ``weak → 原則 reject``. Passing the gate takes
#: an affirmative judgement, never the absence of a negative one.
PASSING_LABELS: frozenset[str] = frozenset({"relevant", "strong"})


@dataclass(frozen=True, slots=True)
class Candidate:
    """A memory Stage 1 found, and why it was worth looking at (§2B).

    ``reasons`` is how it was reached — full-text match, autobiographical
    bridge, shared topic, recency. It is provenance for the debug inspector,
    and it is deliberately *not* a score: Stage 1 does not rank, because
    ranking here is what let accessibility create relevance.
    """

    memory: EpisodicMemory
    reasons: tuple[str, ...] = ()

    @property
    def memory_id(self) -> str:
        return self.memory.memory_id


@dataclass(frozen=True, slots=True)
class RelevanceJudgement:
    """Stage 2's verdict on one candidate (§2E)."""

    memory_id: str
    relevance: RelevanceLabel
    reason: str = ""
    #: ``llm`` when the model judged it, ``fallback`` when the model was
    #: unavailable and Python fell back to a conservative lexical check.
    source: Literal["llm", "fallback"] = "llm"

    @property
    def passed(self) -> bool:
        return self.relevance in PASSING_LABELS


@dataclass(frozen=True, slots=True)
class RecalledMemory:
    """A memory that survived every stage, with how sure she is of it (§2O).

    The three confidences travel with it into the conversation context so she
    can say 「たしか中学生くらいだったと思う」 instead of asserting a year she is
    not sure of. A low-confidence memory is still a memory; it is not a fact.
    """

    memory: EpisodicMemory
    relevance: RelevanceLabel
    relevance_reason: str = ""
    availability: float = 0.0
    state: RetrievalState = "selected"
    reasons: tuple[str, ...] = ()

    @property
    def memory_id(self) -> str:
        return self.memory.memory_id

    @property
    def content_confidence(self) -> float:
        return self.memory.content_confidence

    @property
    def temporal_confidence(self) -> float:
        return self.memory.temporal_confidence

    @property
    def source_confidence(self) -> float:
        return self.memory.source_confidence

    def as_context_line(self) -> str:
        """One line for the reply prompt, hedged to match how sure she is."""
        hedge = _hedge(self.content_confidence, self.temporal_confidence)
        return f"- {self.memory.summary}{hedge}"


def _hedge(content: float, temporal: float) -> str:
    """§2O: a memory she is unsure of must not be offered as a fact."""
    weakest = min(content, temporal)
    if weakest >= 0.75:
        return ""
    if weakest >= 0.5:
        return "（たしかそうだったと思う）"
    return "（はっきりは覚えていない）"


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """A candidate that did not make it, and the stage that stopped it."""

    memory_id: str
    stage: Literal["relevance", "availability"]
    reason: str
    relevance: RelevanceLabel | None = None
    availability: float | None = None


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    """Everything one retrieval did, for the trace and the inspector.

    Observability is a Phase 2 requirement rather than a nicety: without it the
    phase lands in exactly the state it exists to escape — memory works, and
    nobody can say why that memory and not another one.
    """

    query: str
    mode: RecallMode
    candidates: tuple[Candidate, ...] = ()
    judgements: tuple[RelevanceJudgement, ...] = ()
    selected: tuple[RecalledMemory, ...] = ()
    rejected: tuple[RejectedCandidate, ...] = ()
    #: The LLM call that produced the judgements, when there was one.
    llm_call_id: str | None = None
    #: ``llm`` or ``fallback`` — which path Stage 2 actually took.
    relevance_source: str = "llm"
    retrieved_at: datetime | None = None
    #: Groups every row this retrieval wrote, so one recall is one story.
    group_id: str = ""
    #: Memories this retrieval actually practised. Empty for a preview, and
    #: empty until Stage 4 has run for a real recall.
    practised: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(candidate.memory_id for candidate in self.candidates)

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(item.memory_id for item in self.selected)

    @property
    def passed_ids(self) -> tuple[str, ...]:
        return tuple(item.memory_id for item in self.judgements if item.passed)

    @property
    def is_empty(self) -> bool:
        """§2I: recalling nothing is a normal outcome, not a failure."""
        return not self.selected

    def judgement_for(self, memory_id: str) -> RelevanceJudgement | None:
        for judgement in self.judgements:
            if judgement.memory_id == memory_id:
                return judgement
        return None

    def describe(self) -> str:
        """The debug inspector's view (§Debug Inspector)."""
        lines = [
            f"query: {self.query!r}",
            f"mode: {self.mode.value}",
            f"relevance source: {self.relevance_source}",
            f"candidates: {len(self.candidates)}  selected: {len(self.selected)}",
        ]
        for index, candidate in enumerate(self.candidates, start=1):
            judgement = self.judgement_for(candidate.memory_id)
            chosen = next(
                (item for item in self.selected if item.memory_id == candidate.memory_id),
                None,
            )
            rejection = next(
                (item for item in self.rejected if item.memory_id == candidate.memory_id),
                None,
            )
            lines.extend(
                [
                    "",
                    f"Candidate {index}  {candidate.memory_id}",
                    f"  found by: {', '.join(candidate.reasons) or '-'}",
                    f"  semantic relevance: {judgement.relevance if judgement else '-'}"
                    + (f"  ({judgement.reason})" if judgement and judgement.reason else ""),
                    f"  accessibility: {candidate.memory.accessibility:.2f}",
                    f"  selected: {'yes' if chosen else 'no'}"
                    + (f"  (rejected at {rejection.stage}: {rejection.reason})" if rejection else ""),
                    "  practice applied: "
                    + ("yes" if candidate.memory_id in self.practised else "no"),
                ]
            )
        return "\n".join(lines)


__all__ = [
    "PASSING_LABELS",
    "PRACTISING_STATES",
    "RELEVANCE_LABELS",
    "RETRIEVAL_STATES",
    "Candidate",
    "RecalledMemory",
    "RejectedCandidate",
    "RelevanceJudgement",
    "RelevanceLabel",
    "RetrievalReport",
    "RetrievalState",
]
