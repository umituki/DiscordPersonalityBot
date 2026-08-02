"""Reply generation (spec 35 Phase 3, patch spec 8-10).

One reply is produced in four steps, in this order:

    dialogue decision  → what kind of turn is this at all (spec 16.1)
    expression context → how she currently is, in words (patch spec 9)
    generation         → the sentence, plus the Output Guard
    quality guard      → is this a reply a person would send (patch spec 10)

The order is the point. Deciding the acts after the sentence exists would make
the decision a description of whatever the model wrote, and checking quality
before the acts are known would leave nothing to check the prose against.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Sequence

from app.clock import Clock, SystemClock, to_iso
from app.context.builder import BuiltContext, ContextBuilder, Requirement
from app.conversation.expression import ExpressionContext
from app.conversation.guard import OutputGuard
from app.conversation.models import ConversationTurn, DialogueAct, ReplyDraft
from app.conversation.quality import ConversationQualityGuard, QualityVerdict
from app.conversation.text import looks_like_question
from app.memory.models import RetrievalCandidate
from app.conversation.policy import ConversationPolicy
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator, StructuredOutcome
from app.llm.types import LLMMessage
from app.llm.validation import Stage, ValidationContext, ValidationFailure, ValidationPipeline
from app.observability.trace import ConversationTrace
from app.resources.identity import Identity
from app.state.snapshot import StateSnapshot

logger = logging.getLogger(__name__)


def _mark(trace, stage: str) -> None:
    """Patch spec 19.2. Timing must never be able to break a reply."""
    if trace is not None:
        trace.mark(stage)


PROMPT_ID = "conversation_reply"
PURPOSE = "conversation_reply"
ACT_PROMPT_ID = "dialogue_act"
ACT_PURPOSE = "dialogue_act"
REPAIR_PROMPT_ID = "conversation_repair"
REPAIR_PURPOSE = "conversation_repair"

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
    #: How she was, in words rather than numbers (patch spec 9).
    expression: ExpressionContext = ExpressionContext()
    #: The last quality verdict, on whichever text is being returned.
    quality: QualityVerdict | None = None
    #: True when the first draft was rejected by the quality guard and a repair
    #: call produced the text being returned (patch spec 10).
    repaired: bool = False

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
        quality: ConversationQualityGuard | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._identity = identity
        self._prompts = prompts
        self._structured = structured
        self._guard = guard
        self._policy = policy
        #: Patch spec 10: a stage separate from the Output Guard. The Output
        #: Guard decides whether this may be said at all; this decides whether
        #: it is a reply a person would send.
        self._quality = quality or ConversationQualityGuard(
            max_echo_ratio=policy.quality.max_echo_ratio,
            min_echo_chars=policy.quality.min_echo_chars,
            recent_question_window=policy.quality.recent_question_window,
        )
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
        trace: "ConversationTrace | None" = None,
    ) -> ReplyGeneration:
        # Spec 16.1: decide what kind of response this is *before* writing it.
        _mark(trace, "dialogue_started_at")
        acts, acts_source = await self._choose_acts(
            user_text=user_text,
            recent_turns=recent_turns,
            snapshot=snapshot,
            run_id=run_id,
            event_id=event_id,
        )
        _mark(trace, "dialogue_ended_at")

        # Patch spec 9: the reply expresses the state the event produced, in
        # qualitative bands. Never a dump of the state table.
        expression = ExpressionContext.from_snapshot(snapshot, acts=acts)

        template = self._prompts.get(PROMPT_ID)
        context = self._build_context(user_text, recent_turns, memories, acts, expression)

        system_content = template.render(
            identity=context.get("identity").content,
            expression=(
                context.get("expression").content
                if context.includes("expression")
                else expression.render()
            ),
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
            priority="P0",  # a waiting USER outranks background work (spec 33)
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )

        generation = ReplyGeneration(
            outcome=outcome,
            context=context,
            prompt_version=template.prompt_version,
            acts=acts,
            acts_source=acts_source,
            expression=expression,
        )
        if not outcome.accepted or outcome.value is None:
            return generation

        return await self._review_and_repair(
            generation,
            user_text=user_text,
            recent_turns=recent_turns,
            expression=expression,
            snapshot=snapshot,
            run_id=run_id,
            event_id=event_id,
            tool_success_ids=tool_success_ids,
        )

    # --- quality (patch spec 10) -------------------------------------------
    async def _review_and_repair(
        self,
        generation: ReplyGeneration,
        *,
        user_text: str,
        recent_turns: Sequence[ConversationTurn],
        expression: ExpressionContext,
        snapshot: StateSnapshot | None,
        run_id: str | None,
        event_id: str | None,
        tool_success_ids: Sequence[str],
    ) -> ReplyGeneration:
        """Review the draft, and rewrite it at most once.

        Patch spec 10: ``Guard reject時のみ conversation_repair を最大1回``. The
        repair is a rewrite of the same intention, not a second draft — the
        dialogue decision is not re-run, because the decision was not what was
        wrong.
        """
        text = generation.text or ""
        verdict = self._quality.review(
            text, acts=generation.acts, user_text=user_text, recent_turns=recent_turns
        )
        if verdict.accepted:
            return dataclasses.replace(generation, quality=verdict)

        logger.info(
            "reply rejected by quality guard event_id=%s issues=%s",
            event_id,
            ",".join(verdict.issues),
        )
        repaired = await self._repair(
            rejected_text=text,
            verdict=verdict,
            acts=generation.acts,
            user_text=user_text,
            recent_turns=recent_turns,
            expression=expression,
            snapshot=snapshot,
            run_id=run_id,
            event_id=event_id,
            tool_success_ids=tool_success_ids,
        )

        if repaired.accepted and repaired.value is not None:
            second = self._quality.review(
                repaired.value.text.strip(),
                acts=generation.acts,
                user_text=user_text,
                recent_turns=recent_turns,
            )
            if second.accepted:
                return dataclasses.replace(
                    generation, outcome=repaired, quality=second, repaired=True
                )
            verdict = second

        # One rewrite was the budget. Saying nothing is worse than a good
        # reply and better than one that contradicts its own decision
        # (prohibition 9, and the existing `on_rejection: suppress` posture).
        return dataclasses.replace(
            generation,
            outcome=dataclasses.replace(
                generation.outcome,
                accepted=False,
                value=None,
                failure=ValidationFailure(
                    stage=Stage.SEMANTIC,
                    reason_code=verdict.issues[0] if verdict.issues else "quality_rejected",
                    detail=verdict.detail,
                    validator=self._quality.name,
                ),
            ),
            quality=verdict,
            repaired=True,
        )

    async def _repair(
        self,
        *,
        rejected_text: str,
        verdict: QualityVerdict,
        acts: DialogueAct,
        user_text: str,
        recent_turns: Sequence[ConversationTurn],
        expression: ExpressionContext,
        snapshot: StateSnapshot | None,
        run_id: str | None,
        event_id: str | None,
        tool_success_ids: Sequence[str],
    ) -> StructuredOutcome[ReplyDraft]:
        template = self._prompts.get(REPAIR_PROMPT_ID)
        content = template.render(
            identity=self._identity.render_for_prompt(),
            expression=expression.render(),
            recent_conversation=self._render_history(recent_turns) or NO_HISTORY,
            dialogue_acts=acts.render(),
            user_message=user_text,
            rejected_reply=rejected_text,
            problems=self._quality.describe(verdict),
        )
        return await self._structured.generate(
            ReplyDraft,
            (LLMMessage(role="user", content=content),),
            purpose=REPAIR_PURPOSE,
            pipeline=self._structured_pipeline(),
            context=ValidationContext(
                purpose=REPAIR_PURPOSE,
                run_id=run_id,
                event_id=event_id,
                snapshot=snapshot,
                extras={"tool_success_ids": list(tool_success_ids)},
            ),
            run_id=run_id,
            event_id=event_id,
            temperature=self._policy.generation.temperature,
            max_tokens=self._policy.generation.max_tokens,
            priority="P0",
            prompt_id=REPAIR_PROMPT_ID,
            prompt_version=template.prompt_version,
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
            priority="P0",
            prompt_id=ACT_PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if outcome.accepted and outcome.value is not None and not outcome.value.is_empty:
            return outcome.value, "llm"
        # Without a decision, respond in the smallest defensible way rather
        # than inventing an intention (spec 28.3, patch spec 3.5). A direct
        # question is answered, because ignoring one is its own failure.
        return DialogueAct.minimal(direct_question=looks_like_question(user_text)), "default"

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
        expression: ExpressionContext | None = None,
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
        if expression is not None and not expression.is_empty:
            # IMPORTANT rather than REQUIRED: under context pressure she can
            # still answer without knowing her own mood, but not without her
            # identity or the message (spec 27.1, 27.2).
            builder.add(
                "expression",
                expression.render(),
                requirement=Requirement.IMPORTANT,
                priority=70,
                source="dynamic_state",
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
