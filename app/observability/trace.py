"""End-to-end timing for one conversation turn (patch spec 19.2).

The per-call LLM telemetry of 19.1 says how long each model call took. It does
not say where the USER's wait went — and on 2026-08-02 the wait was four to
five minutes, spread across stages nobody could see separately.

One trace per inbound message, marked at the boundaries:

    received_at
    admitted_at
    typing_started_at
    appraisal_started / ended
    state_commit_started / ended
    memory_recall_started / ended
    social_interpretation_started / ended
    reference_retrieval_started / ended
    reply_started / ended
      realization_started / ended
    discord_send_started / ended
    outbound_projected_at
    typing_stopped_at

Patch spec 19.2 also asks for queue wait and inference to be told apart. They
are, and not by guessing: the ``llm_calls`` rows of the run already record
``queue_wait_ms`` separately from the model's own durations, so the trace sums
them from there rather than inferring anything from wall-clock gaps.

Marking is best-effort by design. A trace that failed to record must never take
down a reply, so every method here swallows its own errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app import ids
from app.clock import Clock, SystemClock

logger = logging.getLogger(__name__)

MODULE = "conversation_trace"
TRACE = "trc"

#: The marks of patch spec 19.2, in the order they happen.
STAGES: tuple[str, ...] = (
    "received_at",
    "admitted_at",
    "typing_started_at",
    "appraisal_started_at",
    "appraisal_ended_at",
    "state_commit_started_at",
    "state_commit_ended_at",
    "memory_recall_started_at",
    "memory_recall_ended_at",
    # Audit finding 7. These were marked by the conversation path and thrown
    # away, because `mark()` drops any stage not on this list and nobody added
    # them. Three of the most expensive steps in a turn were unmeasurable.
    "understanding_started_at",
    "understanding_ended_at",
    "semantic_grounding_started_at",
    "semantic_grounding_ended_at",
    "repair_started_at",
    "repair_ended_at",
    # Rebuild spec Phase 3 §47. ``dialogue_*`` is kept in the schema for rows
    # written before Phase 3, but nothing marks it now: the decision stage is
    # the social interpretation.
    "dialogue_started_at",
    "dialogue_ended_at",
    "social_interpretation_started_at",
    "social_interpretation_ended_at",
    # Rebuild spec 12.4, Phase 4. Typing does not start before this decides.
    "response_intent_started_at",
    "response_intent_ended_at",
    "reference_retrieval_started_at",
    "reference_retrieval_ended_at",
    "reply_started_at",
    "realization_started_at",
    "realization_ended_at",
    "reply_ended_at",
    "discord_send_started_at",
    "discord_send_ended_at",
    "outbound_projected_at",
    "typing_stopped_at",
)


#: Audit finding 7. The latencies an operator actually asks for, each as a
#: named pair of marks, so "where did this turn go" is answerable from one row
#: rather than by subtracting timestamps by hand.
LATENCIES: dict[str, tuple[str, str]] = {
    "understanding_ms": ("understanding_started_at", "understanding_ended_at"),
    "memory_recall_ms": ("memory_recall_started_at", "memory_recall_ended_at"),
    "realization_ms": ("realization_started_at", "realization_ended_at"),
    "semantic_grounding_ms": (
        "semantic_grounding_started_at",
        "semantic_grounding_ended_at",
    ),
    "repair_ms": ("repair_started_at", "repair_ended_at"),
}


@dataclass
class ConversationTrace:
    """One turn's timeline. Mutable on purpose: it is filled in as it happens."""

    trace_id: str
    received_at: datetime
    event_id: str | None = None
    run_id: str | None = None
    channel_id: str | None = None
    outcome: str = "unknown"
    marks: dict[str, datetime] = field(default_factory=dict)
    queue_wait_ms: int = 0
    inference_ms: int = 0
    model_calls: int = 0
    #: Dialogue v2, requirement 12. The size of the *final rendered* realizer
    #: input, which is not what the context builder accounted for: the template
    #: renders grounding, common ground, correction, references, style hints
    #: and the situation around the built items.
    prompt_chars: int = 0
    prompt_tokens: int = 0
    #: Audit finding 8. The configured budget this turn was measured against,
    #: and whether the rendered prompt exceeded it. Stored rather than derived
    #: so a later budget change does not rewrite history.
    prompt_budget_tokens: int = 0
    prompt_over_budget: bool = False
    detail: dict[str, object] = field(default_factory=dict)

    @classmethod
    def start(
        cls, *, clock: Clock | None = None, channel_id: str | None = None
    ) -> ConversationTrace:
        now = (clock or SystemClock()).now()
        trace = cls(trace_id=ids.new_id(TRACE), received_at=now, channel_id=channel_id)
        trace.marks["received_at"] = now
        return trace

    # --- marking ------------------------------------------------------------
    def mark(self, stage: str, *, clock: Clock | None = None) -> None:
        if stage not in STAGES:  # pragma: no cover - a typo should be loud in dev
            logger.debug("unknown conversation trace stage %r", stage)
            return
        self.marks[stage] = (clock or SystemClock()).now()

    def at(self, stage: str) -> datetime | None:
        return self.marks.get(stage)

    def latencies(self) -> dict[str, int]:
        """Every measurable stage duration, plus the total.

        Missing stages are simply absent rather than zero: a turn with no
        repair did not take zero milliseconds repairing, it did not repair.
        """
        measured = {
            name: self.elapsed_ms(start, end)
            for name, (start, end) in LATENCIES.items()
        }
        result = {name: value for name, value in measured.items() if value is not None}
        # `total_ms` is the existing end-to-end property, reused rather than
        # recomputed: two definitions of "how long did this take" is exactly
        # the kind of drift this audit is closing elsewhere.
        if self.total_ms is not None:
            result["total_ms"] = self.total_ms
        return result

    def elapsed_ms(self, start: str, end: str) -> int | None:
        first, last = self.marks.get(start), self.marks.get(end)
        if first is None or last is None:
            return None
        return int((last - first).total_seconds() * 1000)

    @property
    def total_ms(self) -> int | None:
        """From arrival to the reply being on screen, or to the decision not to."""
        for last in ("outbound_projected_at", "discord_send_ended_at", "reply_ended_at"):
            elapsed = self.elapsed_ms("received_at", last)
            if elapsed is not None:
                return elapsed
        return None

    @property
    def unaccounted_ms(self) -> int | None:
        """Wait that was neither queued nor inference — the part worth chasing."""
        total = self.total_ms
        if total is None:
            return None
        return max(0, total - self.queue_wait_ms - self.inference_ms)

    def add_model_time(self, *, queue_wait_ms: int, inference_ms: int) -> None:
        self.queue_wait_ms += max(0, queue_wait_ms)
        self.inference_ms += max(0, inference_ms)
        self.model_calls += 1

    def as_detail(self) -> dict[str, object]:
        return {
            "stages_ms": {
                name: value
                for name, value in (
                    ("admit", self.elapsed_ms("received_at", "admitted_at")),
                    (
                        "state_commit",
                        self.elapsed_ms("state_commit_started_at", "state_commit_ended_at"),
                    ),
                    (
                        "memory_recall",
                        self.elapsed_ms(
                            "memory_recall_started_at", "memory_recall_ended_at"
                        ),
                    ),
                    ("dialogue", self.elapsed_ms("dialogue_started_at", "dialogue_ended_at")),
                    ("reply", self.elapsed_ms("reply_started_at", "reply_ended_at")),
                    (
                        "discord_send",
                        self.elapsed_ms(
                            "discord_send_started_at", "discord_send_ended_at"
                        ),
                    ),
                    ("typing", self.elapsed_ms("typing_started_at", "typing_stopped_at")),
                )
                if value is not None
            },
            # Audit finding 7: the named latencies, in the detail structure as
            # well as the columns, so a reader that only has detail_json can
            # still answer "where did the turn go".
            "latencies_ms": self.latencies(),
            # Audit finding 8: the budget numbers for the prompt that was
            # actually sent, not for the subset the context builder assembled.
            "prompt_budget": {
                "chars": self.prompt_chars,
                "tokens": self.prompt_tokens,
                "budget_tokens": self.prompt_budget_tokens,
                "fraction": (
                    round(self.prompt_tokens / self.prompt_budget_tokens, 4)
                    if self.prompt_budget_tokens
                    else None
                ),
                "over_budget": self.prompt_over_budget,
            },
            "unaccounted_ms": self.unaccounted_ms,
            **self.detail,
        }


class ConversationTracer:
    """Starts traces and persists them. Never raises into the reply path."""

    name = MODULE

    def __init__(self, repository, *, clock: Clock | None = None) -> None:
        self._repository = repository
        self._clock = clock or SystemClock()

    def start(self, *, channel_id: str | None = None) -> ConversationTrace:
        return ConversationTrace.start(clock=self._clock, channel_id=channel_id)

    def mark(self, trace: ConversationTrace | None, stage: str) -> None:
        if trace is not None:
            trace.mark(stage, clock=self._clock)

    def finish(self, trace: ConversationTrace | None, *, outcome: str) -> None:
        """Record the trace. A failure here degrades observability, nothing else."""
        if trace is None:
            return
        trace.outcome = outcome
        try:
            self._collect_model_time(trace)
            self._repository.record(trace)
        except Exception:  # noqa: BLE001 - observability must not break a reply
            logger.warning("conversation trace could not be recorded", exc_info=True)

    def _collect_model_time(self, trace: ConversationTrace) -> None:
        """Queue wait and inference, from the calls this run actually made.

        Patch spec 19.2: ``Queue waitとinferenceを区別する``. Read from the
        ``llm_calls`` rows rather than inferred from gaps in the timeline, so a
        slow queue and a slow model are never confused for each other.
        """
        if trace.run_id is None:
            return
        for row in self._repository.calls_for_run(trace.run_id):
            trace.add_model_time(
                queue_wait_ms=int(row["queue_wait_ms"] or 0),
                inference_ms=int(
                    row["model_total_duration_ms"] or row["latency_ms"] or 0
                ),
            )


__all__ = ["MODULE", "STAGES", "ConversationTrace", "ConversationTracer"]
