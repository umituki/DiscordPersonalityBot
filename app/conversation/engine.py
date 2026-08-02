"""Reply generation (spec 35 Phase 3).

Phase 3 is deliberately thin: static identity, recent history, one structured
model call, and the Output Guard. There is no memory retrieval, no appraisal
and no dialogue-act planning here — those belong to Phases 4, 5 and 7, and
faking them now would produce a second, simplified personality engine that the
architecture rules forbid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from app.clock import Clock, SystemClock, to_iso
from app.context.builder import BuiltContext, ContextBuilder, Requirement
from app.conversation.guard import OutputGuard
from app.conversation.models import ConversationTurn, DialogueAct, ReplyDraft
from app.memory.models import RetrievalCandidate
from app.conversation.policy import ConversationPolicy
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator, StructuredOutcome
from app.llm.types import LLMMessage
from app.llm.validation import ValidationContext, ValidationPipeline
from app.resources.identity import Identity
from app.state.snapshot import StateSnapshot

logger = logging.getLogger(__name__)

PROMPT_ID = "conversation_reply"
PURPOSE = "conversation_reply"
ACT_PROMPT_ID = "dialogue_act"
ACT_PURPOSE = "dialogue_act"

NO_HISTORY = "(まだ記録されたやりとりはない)"
NO_MEMORIES = "(いま思い出せることはない)"


@dataclass(frozen=True, slots=True)
class ReplyGeneration:
    """Everything one reply attempt produced, accepted or not."""

    outcome: StructuredOutcome[ReplyDraft]
    context: BuiltContext
    prompt_version: str
    #: Decided before the sentence was written (spec 16.1).
    acts: DialogueAct = DialogueAct.minimal()
    acts_source: str = "default"

    @property
    def accepted(self) -> bool:
        return self.outcome.accepted

    @property
    def text(self) -> str | None:
        return None if self.outcome.value is None else self.outcome.value.text.strip()


class ConversationEngine:
    def __init__(
        self,
        *,
        identity: Identity,
        prompts: PromptRegistry,
        structured: StructuredGenerator,
        guard: OutputGuard,
        policy: ConversationPolicy,
        clock: Clock | None = None,
    ) -> None:
        self._identity = identity
        self._prompts = prompts
        self._structured = structured
        self._guard = guard
        self._policy = policy
        self._clock = clock or SystemClock()

    @property
    def identity(self) -> Identity:
        return self._identity

    async def draft_reply(
        self,
        *,
        user_text: str,
        recent_turns: Sequence[ConversationTurn] = (),
        memories: Sequence[RetrievalCandidate] = (),
        snapshot: StateSnapshot | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        tool_success_ids: Sequence[str] = (),
    ) -> ReplyGeneration:
        # Spec 16.1: decide what kind of response this is *before* writing it.
        acts, acts_source = await self._choose_acts(
            user_text=user_text,
            recent_turns=recent_turns,
            snapshot=snapshot,
            run_id=run_id,
            event_id=event_id,
        )

        template = self._prompts.get(PROMPT_ID)
        context = self._build_context(user_text, recent_turns, memories, acts)

        system_content = template.render(
            identity=context.get("identity").content,
            recent_conversation=(
                context.get("recent_conversation").content
                if context.includes("recent_conversation")
                else NO_HISTORY
            ),
            relevant_memories=(
                context.get("relevant_memories").content
                if context.includes("relevant_memories")
                else NO_MEMORIES
            ),
            dialogue_acts=acts.render(),
            current_time=to_iso(self._clock.now()),
        )

        outcome = await self._structured.generate(
            ReplyDraft,
            (
                LLMMessage(role="system", content=system_content),
                LLMMessage(role="user", content=user_text),
            ),
            purpose=PURPOSE,
            pipeline=self._structured_pipeline(),
            context=ValidationContext(
                purpose=PURPOSE,
                run_id=run_id,
                event_id=event_id,
                snapshot=snapshot,
                extras={"tool_success_ids": list(tool_success_ids)},
            ),
            run_id=run_id,
            event_id=event_id,
            temperature=self._policy.generation.temperature,
            max_tokens=self._policy.generation.max_tokens,
            timeout_s=self._policy.generation.timeout_s,
            priority="P0",  # a waiting USER outranks background work (spec 33)
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )

        return ReplyGeneration(
            outcome=outcome,
            context=context,
            prompt_version=template.prompt_version,
            acts=acts,
            acts_source=acts_source,
        )

    # --- dialogue acts (spec 16.1) -----------------------------------------
    async def _choose_acts(
        self,
        *,
        user_text: str,
        recent_turns: Sequence[ConversationTurn],
        snapshot: StateSnapshot | None,
        run_id: str | None,
        event_id: str | None,
    ) -> tuple[DialogueAct, str]:
        template = self._prompts.get(ACT_PROMPT_ID)
        content = template.render(
            identity=self._identity.render_for_prompt(),
            recent_conversation=self._render_history(recent_turns) or NO_HISTORY,
            user_message=user_text,
            state_summary=self._state_summary(snapshot),
        )
        outcome = await self._structured.generate(
            DialogueAct,
            (LLMMessage(role="user", content=content),),
            purpose=ACT_PURPOSE,
            run_id=run_id,
            event_id=event_id,
            temperature=0.4,
            max_tokens=200,
            timeout_s=self._policy.generation.timeout_s,
            priority="P0",
            prompt_id=ACT_PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if outcome.accepted and outcome.value is not None and not outcome.value.is_empty:
            return outcome.value, "llm"
        # Without a decision, answer in the smallest defensible way rather than
        # inventing an intention (spec 28.3).
        return DialogueAct.minimal(), "default"

    @staticmethod
    def _state_summary(snapshot: StateSnapshot | None) -> str:
        """A short, factual reading of current state for the act decision."""
        if snapshot is None:
            return "(状態は不明)"
        parts: list[str] = []
        for domain in ("mood", "needs", "emotion"):
            values = snapshot.domain(domain)
            for key, value in sorted(values.items()):
                number = value.numeric
                if number is not None and number >= 0.4:
                    parts.append(f"{domain}.{key}={number:.2f}")
        return ", ".join(parts[:8]) or "(とくに強い状態はない)"

    # --- context ------------------------------------------------------------
    def _build_context(
        self,
        user_text: str,
        recent_turns: Sequence[ConversationTurn],
        memories: Sequence[RetrievalCandidate] = (),
        acts: DialogueAct | None = None,
    ) -> BuiltContext:
        builder = ContextBuilder()
        builder.add(
            "identity",
            self._identity.render_for_prompt(),
            requirement=Requirement.REQUIRED,
            priority=100,
            source="character/",
        )
        builder.add(
            "current_message",
            user_text,
            requirement=Requirement.REQUIRED,
            priority=90,
            source="discord",
        )
        history = self._render_history(recent_turns)
        builder.add(
            "recent_conversation",
            history,
            requirement=Requirement.IMPORTANT,
            priority=50,
            source="conversation_turns",
        )
        # Recalled memory is OPTIONAL: under context pressure YUI forgets
        # rather than losing her identity or the message she is answering
        # (spec 27.1, 27.2).
        builder.add(
            "relevant_memories",
            "\n".join(candidate.as_context_line() for candidate in memories),
            requirement=Requirement.OPTIONAL,
            priority=40,
            source="episodic_memory",
        )
        if acts is not None:
            builder.add(
                "dialogue_acts",
                acts.render(),
                requirement=Requirement.REQUIRED,
                priority=80,
                source="dialogue_act_engine",
            )
        return builder.build(self._policy.context.budget())

    def _render_history(self, turns: Sequence[ConversationTurn]) -> str:
        limit = self._policy.context.recent_turn_limit
        max_chars = self._policy.context.recent_turn_max_chars
        selected = list(turns)[-limit:] if limit else []
        lines = []
        for turn in selected:
            content = turn.content.strip()
            if len(content) > max_chars:
                content = content[:max_chars] + "…"
            label = "USER" if turn.speaker == "user" else self._identity.name
            lines.append(f"{label}: {content}")
        return "\n".join(lines)

    def _structured_pipeline(self) -> ValidationPipeline:
        return ValidationPipeline([self._guard])
