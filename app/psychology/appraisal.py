"""Appraisal — how an event is read (spec 11.1, 9.5 phase 1).

``同じ Event から同じ Emotion を直接決めない``. Nothing maps an event type to an
emotion. An event is *appraised* first, in context, and emotions follow from the
appraisal. The same message can be read differently depending on the state it
lands in.

The model may propose the appraisal; Python owns what happens next, caps the
reported confidence (spec 24), and falls back to policy defaults when the model
is unavailable or its answer is invalid — a degraded reading, never a guess
dressed up as certainty.
"""

from __future__ import annotations

import logging
from typing import Sequence

from app.clock import Clock, SystemClock, to_iso
from app.conversation.models import ConversationTurn
from app.events.model import Event
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage
from app.orchestrator.run_view import Interpretation
from app.psychology.heuristic import appraise_event
from app.psychology.models import Appraisal, AppraisalCandidate
from app.psychology.policy import AppraisalPolicy
from app.resources.identity import Identity
from app.state.snapshot import StateSnapshot

logger = logging.getLogger(__name__)

PROMPT_ID = "appraisal"
PURPOSE = "appraisal"
NO_HISTORY = "(直近のやりとりはない)"

#: Events worth appraising. Everything else passes through uninterpreted.
APPRAISABLE_CATEGORIES = frozenset({"social", "world", "action", "knowledge"})

#: Patch spec 12.1: ``category="internal"`` だけで除外しない.
#:
#: A simulated experience is categorised ``internal`` because it is not
#: something that happened in the real world — and that categorisation silently
#: excluded the entire simulated past from appraisal. 238 experiences produced
#: zero appraisals, so no emotion, no needs, no evidence, no growth: a life
#: that was recorded but never lived. These types are appraisable regardless of
#: category, provided they carry the simulated-past origin.
APPRAISABLE_SIMULATED_TYPES = frozenset({"SIMULATED_EXPERIENCE"})
SIMULATED_ORIGIN = "simulated_past"

#: Patch spec 5.3: YUI's own outbound message is not something that happened
#: *to* her, and re-reading it through the appraisal prompt spends a model call
#: on a foregone conclusion while the USER waits. The psychological effect of
#: her own act belongs to the decision and to the USER's reaction, not to the
#: text she just produced.
SELF_AUTHORED_EVENT_TYPES = frozenset(
    {"YUI_MESSAGE_SENT", "YUI_REPLY_SUPPRESSED", "YUI_INTENTIONAL_SILENCE"}
)


class AppraisalEngine:
    """Produces the Layer 1 interpretation for a run."""

    name = "appraisal_engine"

    def __init__(
        self,
        *,
        identity: Identity,
        prompts: PromptRegistry,
        structured: StructuredGenerator,
        policy: AppraisalPolicy,
        clock: Clock | None = None,
    ) -> None:
        self._identity = identity
        self._prompts = prompts
        self._structured = structured
        self._policy = policy
        self._clock = clock or SystemClock()
        self._recent_turns: tuple[ConversationTurn, ...] = ()
        #: Patch spec 19.2: the turn's trace, supplied by the conversation path
        #: so the appraisal stage is visible separately in the timeline.
        self._trace = None

    def set_recent_turns(self, turns: Sequence[ConversationTurn]) -> None:
        """Context for the next appraisal, supplied by the conversation path."""
        self._recent_turns = tuple(turns)

    def set_trace(self, trace) -> None:
        """The trace to mark, or ``None`` to stop marking."""
        self._trace = trace

    async def interpret(self, event: Event, snapshot: StateSnapshot) -> Interpretation:
        if not self.is_appraisable(event):
            return Interpretation()
        if self.is_self_authored(event):
            return Interpretation()
        self._mark("appraisal_started_at")
        try:
            appraisal = await self.appraise(event, snapshot)
        finally:
            self._mark("appraisal_ended_at")
        return Interpretation(appraisal=appraisal)

    def _mark(self, stage: str) -> None:
        if self._trace is not None:
            self._trace.mark(stage)

    @staticmethod
    def is_appraisable(event: Event) -> bool:
        """Whether this event is read at all (patch spec 12.1)."""
        if event.category in APPRAISABLE_CATEGORIES:
            return True
        return (
            event.event_type in APPRAISABLE_SIMULATED_TYPES
            and event.origin == SIMULATED_ORIGIN
        )

    @staticmethod
    def is_self_authored(event: Event) -> bool:
        """Whether YUI is appraising her own outbound act (patch spec 5.3).

        A simulated experience is authored by ``yui`` too, but it is something
        that happened *to* her in the given past — not a message she just
        chose to send — so it is read like any other event.
        """
        return event.actor_type == "yui" and event.event_type in SELF_AUTHORED_EVENT_TYPES

    def uses_heuristic(self, event: Event) -> bool:
        """Whether this event is read in Python rather than by the model.

        Patch spec 12.3: routine and minor experiences are the bulk of a
        simulated life, and a model call for each is what makes decades
        unaffordable. The appraisal still happens.
        """
        rules = self._policy.heuristic
        experience_class = str(getattr(event.payload, "experience_class", "") or "")
        return bool(experience_class) and rules.covers(experience_class)

    async def appraise(self, event: Event, snapshot: StateSnapshot) -> Appraisal:
        if self.uses_heuristic(event):
            return appraise_event(event, self._policy.heuristic)

        situation = _describe(event)
        template = self._prompts.get(PROMPT_ID)
        content = template.render(
            identity=self._identity.render_for_prompt(),
            situation=situation,
            recent_conversation=self._render_history(),
            # The event's time is the time being appraised. This is identical
            # to wall time for an ordinary live message, and remains in the
            # correct historical period for simulation and replay.
            current_time=to_iso(event.occurred_at),
        )

        outcome = await self._structured.generate(
            AppraisalCandidate,
            (LLMMessage(role="user", content=content),),
            purpose=PURPOSE,
            event_id=event.event_id,
            priority="P1",  # immediate appraisal sits just behind the reply
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if not outcome.accepted or outcome.value is None:
            logger.info(
                "appraisal unavailable event_id=%s stage=%s; using policy defaults",
                event.event_id,
                None if outcome.failure is None else outcome.failure.stage.label,
            )
            return self._policy.defaults.as_appraisal(source="degraded")

        candidate = outcome.value
        # Rebuild spec APP-001: the model named the meaning; Python owns what
        # that name is worth. The scale is fixed policy and does not consult
        # the personality or the mood reading the event (APP-003).
        scales = self._policy.scales
        return Appraisal(
            **scales.as_dimensions(candidate),
            # Spec 24: the model's own confidence is a signal, and it is capped.
            confidence=min(
                scales.value("confidence", candidate.confidence),
                self._policy.max_trusted_confidence,
            ),
            source="llm",
            reason=candidate.reason[:200],
        )

    def _render_history(self) -> str:
        if not self._recent_turns:
            return NO_HISTORY
        return "\n".join(
            turn.as_prompt_line("USER", self._identity.name) for turn in self._recent_turns[-6:]
        )


def _describe(event: Event) -> str:
    """A neutral description of the event, without interpreting it."""
    text = getattr(event.payload, "text", None)
    actor = {
        "user": "USER",
        "yui": "自分",
        "npc": "NPC",
        "system": "システム",
        "world": "環境",
        "admin": "管理操作",
    }.get(event.actor_type, event.actor_type)

    if isinstance(text, str) and text.strip():
        return f"{actor} からのメッセージ: {text.strip()[:600]}"
    return f"{actor} による出来事: {event.event_type}"
