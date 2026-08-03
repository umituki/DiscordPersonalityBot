"""Reply generation (rebuild spec 10-14, Phase 3).

One reply is produced in this order:

    social interpretation → what does this turn ask of me?     (one model call)
    surface plan          → how much room does the answer get? (Python only)
    style hints           → what have I been overusing lately?
    dialogue references   → how do people actually say this?   (no model call)
    realization           → the Japanese, plus the Output Guard
    quality + grounding   → would a person send this, and is it true?

The order is the point. Deciding the intention after the sentence exists would
make the decision a description of whatever the model wrote, and checking the
prose before the intention is known leaves nothing to check it against.

Where the line falls: everything above ``realization`` decides *meaning and
size*. None of it writes Japanese. Python does not append 「ね」 and does not
prepend 「うん、」 — rules of that shape produce text that is locally correct and
globally strange, and each fix needs another condition. The realizer writes the
sentence; the stages around it decide what the sentence is for and box in what
it is allowed to claim.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from app.clock import Clock, SystemClock, to_iso
from app.context.builder import BuiltContext, ContextBuilder, Requirement
from app.conversation.expression import ExpressionContext
from app.conversation.guard import OutputGuard
from app.conversation.models import ConversationTurn, ReplyDraft
from app.conversation.quality import ConversationQualityGuard, QualityVerdict
from app.conversation.references import (
    DialogueReference,
    DialogueReferenceProvider,
    DialogueReferenceQuery,
    NullReferenceProvider,
    render_references,
)
from app.conversation.repetition import StyleHints, SurfaceRepetitionMonitor
from app.conversation.response_intent import (
    IntentDecision,
    ResponseIntent,
    ResponseIntentGate,
)
from app.conversation.social_interpretation import (
    SocialInterpretation,
    SocialInterpreter,
)
from app.conversation.surface import RelationshipBand, SurfacePlan, SurfacePlanner
from app.conversation.text import looks_like_question
from app.grounding.claims import ClaimGroundingGuard, GroundingVerdict
from app.grounding.models import GroundingContext
from app.memory.recall_models import RecalledMemory
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


def _log_rejection(
    event_id: str | None, quality: QualityVerdict, grounded: GroundingVerdict
) -> None:
    if not quality.accepted:
        logger.info(
            "reply rejected by quality guard event_id=%s issues=%s",
            event_id,
            ",".join(quality.issues),
        )
    if not grounded.accepted:
        logger.warning(
            "reply makes unsupported claims event_id=%s kinds=%s",
            event_id,
            ",".join(item.claim.kind for item in grounded.blocking),
        )


PROMPT_ID = "conversation_reply"
PURPOSE = "conversation_reply"
ACT_PROMPT_ID = "dialogue_act"
ACT_PURPOSE = "dialogue_act"
REPAIR_PROMPT_ID = "conversation_repair"
REPAIR_PURPOSE = "conversation_repair"

NO_HISTORY = "(まだ記録されたやりとりはない)"
NO_MEMORIES = "(いま思い出せることはない)"
NO_COMMON_GROUND = "(この会話でまだ前提になっていることはない)"
NO_CORRECTION = "(訂正すべきことはない)"
NO_STYLE_HINTS = "(とくに繰り返しはない)"
NO_REFERENCES = "(参考にできる会話例はない)"
NO_GROUNDING = "(いま確かなことは特にない)"


@dataclass(frozen=True, slots=True)
class TurnPlan:
    """Everything decided before a word is written (Phase 3 §46, Phase 4).

    The intent is part of the plan rather than a later step, because §12.4
    requires it settled before the typing indicator goes up.
    """

    social: SocialInterpretation
    surface: SurfacePlan
    style_hints: StyleHints
    references: tuple[DialogueReference, ...]
    intent: IntentDecision

    @property
    def speaks(self) -> bool:
        return self.intent.speaks


@dataclass(frozen=True, slots=True)
class ReplyGeneration:
    """Everything one reply attempt produced, accepted or not."""

    outcome: StructuredOutcome[ReplyDraft]
    context: BuiltContext
    prompt_version: str
    #: Decided before the sentence was written (rebuild spec 10).
    social: SocialInterpretation = SocialInterpretation()
    #: How much room the answer got (rebuild spec 14). Python, no model call.
    surface: SurfacePlan = SurfacePlan()
    #: The examples handed to the realizer. Never memories (spec 13.3).
    references: tuple[DialogueReference, ...] = ()
    #: What she has been overusing lately. Advice, not a rule.
    style_hints: StyleHints = StyleHints()
    #: Phase 4: what was decided about speaking at all.
    intent: IntentDecision = IntentDecision(intent=ResponseIntent.NORMAL_REPLY)
    #: How she was, in words rather than numbers (patch spec 9).
    expression: ExpressionContext = ExpressionContext()
    #: The last quality verdict, on whichever text is being returned.
    quality: QualityVerdict | None = None
    #: Rebuild spec 15: what the claims in this text were resolved to.
    grounding: GroundingVerdict | None = None
    #: True when the first draft was rejected by the quality or grounding guard
    #: and a repair call produced the text being returned (patch spec 10,
    #: GROUND-002).
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
        grounding: ClaimGroundingGuard | None = None,
        interpreter: SocialInterpreter | None = None,
        planner: SurfacePlanner | None = None,
        references: DialogueReferenceProvider | None = None,
        repetition: SurfaceRepetitionMonitor | None = None,
        intent_gate: ResponseIntentGate | None = None,
        shadow: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._identity = identity
        self._prompts = prompts
        self._structured = structured
        self._guard = guard
        self._policy = policy
        #: Rebuild spec 10. The one social reading of the turn.
        self._interpreter = interpreter or SocialInterpreter(
            identity=identity, prompts=prompts, structured=structured
        )
        #: Rebuild spec 14. Python only — a second model call to decide "short
        #: and casual" would double the wait for a lookup (§32).
        self._planner = planner or SurfacePlanner()
        #: Spec 13.2. Optional by design: with no corpus the realizer runs on
        #: an empty tuple, and speaking must never depend on one (§28).
        self._references = references or NullReferenceProvider()
        self._repetition = repetition or SurfaceRepetitionMonitor()
        #: Rebuild spec 12. Python authority over whether a speech act happens
        #: at all; the model only ever proposes.
        self._intent_gate = intent_gate or ResponseIntentGate()
        #: Spec 47, Phase 14. Optional silence is one of the four shadowed
        #: capabilities. The mode is consulted *before* the reply is realized,
        #: because turning a silence into a reply afterwards would mean a
        #: second planning round for the commonest case.
        self._shadow = shadow
        #: Rebuild spec 15. Optional only so a caller that has no context to
        #: resolve against can still draft; when it is absent no claim is
        #: checked, which is why bootstrap always supplies one.
        self._grounding = grounding
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

    async def plan_turn(
        self,
        *,
        user_text: str,
        recent_turns: Sequence[ConversationTurn] = (),
        snapshot: StateSnapshot | None = None,
        grounding: GroundingContext | None = None,
        common_ground: str = "",
        correction: str = "",
        relationship_band: RelationshipBand = "acquaintance",
        run_id: str | None = None,
        event_id: str | None = None,
        trace: "ConversationTrace | None" = None,
    ) -> TurnPlan:
        """Everything decided before a word is written.

        Split out so the debug preview runs exactly this and nothing else
        (§46): a preview that took a different path would be a preview of a
        different system. It writes nothing and sends nothing.
        """
        _mark(trace, "social_interpretation_started_at")
        social = await self._interpreter.interpret(
            user_text=user_text,
            recent_conversation=self._render_history(recent_turns),
            state_summary=self._state_summary(snapshot),
            common_ground=common_ground,
            relationship_band=relationship_band,
            run_id=run_id,
            event_id=event_id,
        )
        # §7: Common Ground is the authority on correction. If the tracker has
        # already retracted a claim, this turn is a repair whatever the model
        # made of 「違うよ」 — the decision belongs where the evidence is.
        social = social.with_correction(correction)
        _mark(trace, "social_interpretation_ended_at")

        surface = self._planner.plan(
            social,
            user_text=user_text,
            band=relationship_band,
            # §22: an experience may only be disclosed as an experience when
            # something says it happened.
            grounded_experience=_has_lived_evidence(grounding),
        )
        hints = self._repetition.review(recent_turns)

        # Rebuild spec 12.4: decided here, before anything the USER can see.
        _mark(trace, "response_intent_started_at")
        intent = self._intent_gate.decide(
            social,
            user_text=user_text,
            correction=correction,
            silence_allowed=self._silence_allowed(),
        )
        _mark(trace, "response_intent_ended_at")
        self._record_silence_shadow(intent, user_text)
        if not intent.speaks:
            # Nothing downstream is needed: no references to calibrate against,
            # because there is no sentence to calibrate.
            return TurnPlan(
                social=social,
                surface=surface,
                style_hints=hints,
                references=(),
                intent=intent,
            )

        _mark(trace, "reference_retrieval_started_at")
        references = await self._retrieve_references(social, surface)
        _mark(trace, "reference_retrieval_ended_at")
        return TurnPlan(
            social=social,
            surface=surface,
            style_hints=hints,
            references=references,
            intent=intent,
        )

    # --- shadowed silence (rebuild spec 47) ---------------------------------
    def _silence_allowed(self) -> bool:
        """Whether an optional silence may actually happen.

        A missing controller means yes: the gate's own rules are the authority
        on silence, and Phase 14 only adds a way to hold them back.
        """
        if self._shadow is None:
            return True
        try:
            return self._shadow.live("intentional_silence")
        except Exception:  # noqa: BLE001 - a broken mode must not break a turn
            logger.exception("could not read the silence shadow mode")
            return True

    def _record_silence_shadow(self, intent, user_text: str) -> None:
        """Record the turns where the mode was the deciding factor.

        Narrower than "she thought about being quiet". A turn where the veto
        fired, or where nothing made silence natural, was decided by the gate
        before the mode was ever consulted — recording those as shadow
        decisions would credit the mode with restraint that was Python's, and
        bury the handful of rows the OWNER actually has to read.
        """
        if self._shadow is None:
            return
        from app.conversation.response_intent import ResponseIntent

        if intent.proposed is not ResponseIntent.INTENTIONAL_SILENCE:
            return
        if intent.source not in ("llm", "shadow"):
            return
        try:
            self._shadow.decide(
                "intentional_silence",
                would_act=True,
                subject=user_text[:120],
                reason=intent.reason,
                detail={"intent": intent.intent.value, "source": intent.source},
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record a silence shadow decision")

    async def _retrieve_references(
        self, social: SocialInterpretation, surface: SurfacePlan
    ) -> tuple[DialogueReference, ...]:
        """Spec 13.2, §28. A corpus that is missing or broken is not an outage."""
        query = DialogueReferenceQuery(
            relationship_band=surface.relationship_band,
            conversation_type=_conversation_type(social),
            primary_move=social.primary_move,
            tone=social.tone,
            length=surface.length,
            initiative=social.initiative,
        )
        try:
            return await self._references.retrieve(
                query, limit=self._policy.generation.reference_limit
            )
        except Exception:  # noqa: BLE001 - references are never load-bearing
            logger.exception("dialogue reference lookup failed; continuing without")
            return ()

    async def draft_reply(
        self,
        *,
        user_text: str,
        recent_turns: Sequence[ConversationTurn] = (),
        memories: Sequence[RecalledMemory] = (),
        snapshot: StateSnapshot | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        tool_success_ids: Sequence[str] = (),
        grounding: GroundingContext | None = None,
        common_ground: str = "",
        correction: str = "",
        relationship_band: RelationshipBand = "acquaintance",
        plan: TurnPlan | None = None,
        trace: "ConversationTrace | None" = None,
    ) -> ReplyGeneration:
        # The caller may have planned already — the conversation service does,
        # because it has to know whether she is speaking at all before the
        # typing indicator goes up (spec 12.4). Re-planning here would spend a
        # second interpretation call on a decision already made.
        if plan is None:
            plan = await self.plan_turn(
                user_text=user_text,
                recent_turns=recent_turns,
                snapshot=snapshot,
                grounding=grounding,
                common_ground=common_ground,
                correction=correction,
                relationship_band=relationship_band,
                run_id=run_id,
                event_id=event_id,
                trace=trace,
            )
        social, surface = plan.social, plan.surface
        hints, references = plan.style_hints, plan.references

        # Patch spec 9: the reply expresses the state the event produced, in
        # qualitative bands. Never a dump of the state table.
        expression = ExpressionContext.from_snapshot(snapshot)

        template = self._prompts.get(PROMPT_ID)
        context = self._build_context(
            user_text, recent_turns, memories, social, surface, expression
        )

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
            # Rebuild spec 15.1: what she may speak of as having happened.
            grounding_facts=_render_grounding(grounding),
            social_intent=social.render(),
            surface_plan=surface.render(),
            style_hints=hints.render() or NO_STYLE_HINTS,
            references=render_references(references) or NO_REFERENCES,
            # Rebuild spec 11. Empty is the honest reading when this
            # conversation has not asserted anything yet.
            common_ground=common_ground or NO_COMMON_GROUND,
            correction=correction or NO_CORRECTION,
            current_time=to_iso(self._clock.now()),
        )

        _mark(trace, "realization_started_at")
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
            # §34: warmer than the appraisal on purpose. The appraisal wants
            # stability; this wants a sentence that does not read like the last
            # one. Grounding is what keeps the extra freedom honest.
            temperature=self._policy.generation.realizer_temperature,
            max_tokens=self._policy.generation.max_tokens,
            priority="P0",  # a waiting USER outranks background work (spec 33)
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        _mark(trace, "realization_ended_at")

        generation = ReplyGeneration(
            outcome=outcome,
            context=context,
            prompt_version=template.prompt_version,
            social=social,
            surface=surface,
            references=references,
            style_hints=hints,
            intent=plan.intent,
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
            grounding=grounding,
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
        grounding: GroundingContext | None = None,
    ) -> ReplyGeneration:
        """Review the draft, and rewrite it at most once.

        Patch spec 10: ``Guard reject時のみ conversation_repair を最大1回``, and
        GROUND-002: ``Unsupported claim があれば 1 回のみ repair``. One budget
        covers both, because both are the same rewrite of the same intention —
        the dialogue decision is not re-run, since the decision was not what was
        wrong. Failing either check after that rewrite suppresses the send
        (GROUND-003).
        """
        text = generation.text or ""
        quality = self._quality.review(
            text,
            allows_question=generation.surface.allows_question,
            user_text=user_text,
            recent_turns=recent_turns,
        )
        grounded = self._check_grounding(text, grounding)
        if quality.accepted and grounded.accepted:
            return dataclasses.replace(generation, quality=quality, grounding=grounded)

        _log_rejection(event_id, quality, grounded)
        repaired = await self._repair(
            rejected_text=text,
            verdict=quality,
            grounded=grounded,
            social=generation.social,
            surface=generation.surface,
            user_text=user_text,
            recent_turns=recent_turns,
            expression=expression,
            snapshot=snapshot,
            run_id=run_id,
            event_id=event_id,
            tool_success_ids=tool_success_ids,
        )

        if repaired.accepted and repaired.value is not None:
            second_text = repaired.value.text.strip()
            second = self._quality.review(
                second_text,
                allows_question=generation.surface.allows_question,
                user_text=user_text,
                recent_turns=recent_turns,
            )
            second_grounded = self._check_grounding(second_text, grounding)
            if second.accepted and second_grounded.accepted:
                return dataclasses.replace(
                    generation,
                    outcome=repaired,
                    quality=second,
                    grounding=second_grounded,
                    repaired=True,
                )
            quality, grounded = second, second_grounded

        # One rewrite was the budget. Saying nothing is worse than a good
        # reply and better than one that contradicts its own decision
        # (prohibition 9, and the existing `on_rejection: suppress` posture) —
        # or than one that states something that never happened (GROUND-003).
        if not grounded.accepted:
            reason_code, detail, validator = (
                grounded.reason_code,
                grounded.describe(),
                self._grounding.name if self._grounding else "claim_grounding_guard",
            )
        else:
            reason_code = quality.issues[0] if quality.issues else "quality_rejected"
            detail, validator = quality.detail, self._quality.name

        return dataclasses.replace(
            generation,
            outcome=dataclasses.replace(
                generation.outcome,
                accepted=False,
                value=None,
                failure=ValidationFailure(
                    stage=Stage.SEMANTIC,
                    reason_code=reason_code,
                    detail=detail,
                    validator=validator,
                ),
            ),
            quality=quality,
            grounding=grounded,
            repaired=True,
        )

    def _check_grounding(
        self, text: str, context: GroundingContext | None
    ) -> GroundingVerdict:
        """Resolve this text's claims against what is known (spec 15.3).

        With no guard or no context there is nothing to resolve against, and an
        empty verdict accepts: the guard's job is to catch a claim that
        contradicts the record, not to refuse to speak when there is no record.
        """
        if self._grounding is None or context is None:
            return GroundingVerdict()
        return self._grounding.review(text, context)

    async def _repair(
        self,
        *,
        rejected_text: str,
        verdict: QualityVerdict,
        grounded: GroundingVerdict,
        social: SocialInterpretation,
        surface: SurfacePlan,
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
            dialogue_acts=social.render() + "\n" + surface.render(),
            user_message=user_text,
            rejected_reply=rejected_text,
            problems=self._describe_problems(verdict, grounded),
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

    def _describe_problems(
        self, verdict: QualityVerdict, grounded: GroundingVerdict
    ) -> str:
        """What was wrong, for the rewrite. Never what to say instead.

        An unsupported claim is described as unsupported, not corrected: the
        repair call must not be handed a fact to assert, because the system does
        not have one — that is the whole finding.
        """
        parts: list[str] = []
        if not verdict.accepted:
            parts.append(self._quality.describe(verdict))
        if not grounded.accepted:
            parts.append(
                "裏づけのない事実を書いている。書かないか、断定をやめること:\n"
                + grounded.describe()
            )
        return "\n".join(part for part in parts if part.strip())

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
        memories: Sequence[RecalledMemory] = (),
        social: SocialInterpretation | None = None,
        surface: SurfacePlan | None = None,
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
        if social is not None:
            builder.add(
                "social_intent",
                social.render() + ("\n" + surface.render() if surface else ""),
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


def _conversation_type(social: SocialInterpretation) -> str:
    """Which kind of exchange this is, for the reference lookup (§24)."""
    if social.is_repair:
        return "repair"
    if social.primary_move == "support":
        return "support"
    if social.primary_move == "answer":
        return "answer"
    return "smalltalk"


#: Evidence kinds that mean something actually happened to her, as opposed to
#: something she knows or intends. Only these let her speak from experience.
_LIVED_EVIDENCE = ("activity", "objective_event")


def _has_lived_evidence(grounding: GroundingContext | None) -> bool:
    """§22. 「わたしも昨日そうだった」 needs a yesterday that is on record."""
    if grounding is None:
        return False
    return bool(grounding.of_kinds(_LIVED_EVIDENCE))


def _render_grounding(grounding: GroundingContext | None, limit: int = 8) -> str:
    """The facts she may speak of as hers (spec 15.1).

    Deliberately short. This is not the whole grounding context — the guard
    resolves against that — it is the handful she can actually refer to.
    """
    if grounding is None:
        return NO_GROUNDING
    lines = [
        f"- {item.summary}"
        for item in grounding.of_kinds(_LIVED_EVIDENCE + ("world_state", "tool_call"))
        if item.summary.strip()
    ]
    return "\n".join(lines[:limit]) or NO_GROUNDING
