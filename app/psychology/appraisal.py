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

    def set_recent_turns(self, turns: Sequence[ConversationTurn]) -> None:
        """Context for the next appraisal, supplied by the conversation path."""
        self._recent_turns = tuple(turns)

    async def interpret(self, event: Event, snapshot: StateSnapshot) -> Interpretation:
        if event.category not in APPRAISABLE_CATEGORIES:
            return Interpretation()
        appraisal = await self.appraise(event, snapshot)
        return Interpretation(appraisal=appraisal)

    async def appraise(self, event: Event, snapshot: StateSnapshot) -> Appraisal:
        situation = _describe(event)
        template = self._prompts.get(PROMPT_ID)
        content = template.render(
            identity=self._identity.render_for_prompt(),
            situation=situation,
            recent_conversation=self._render_history(),
            current_time=to_iso(self._clock.now()),
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
        return Appraisal(
            self_relevance=candidate.self_relevance,
            goal_congruence=candidate.goal_congruence,
            novelty=candidate.novelty,
            certainty=candidate.certainty,
            control=candidate.control,
            agency=candidate.agency,
            social_meaning=candidate.social_meaning,
            expectation_violation=candidate.expectation_violation,
            # Spec 24: the model's own confidence is a signal, and it is capped.
            confidence=min(candidate.confidence, self._policy.max_trusted_confidence),
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
