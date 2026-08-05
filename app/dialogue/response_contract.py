"""The one turn-level contract shared by realization, quality and repair.

This is deliberately not a truth authority.  It is derived from the existing
TurnUnderstanding and SocialInterpretation and says what the reply must do,
not which facts are true.  Grounding remains the only factual authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.conversation.social_interpretation import SocialInterpretation
from app.conversation.text import looks_like_question
from app.dialogue.understanding import TurnUnderstanding
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage

logger = logging.getLogger(__name__)

PROMPT_ID = "response_contract_review"
PURPOSE = "response_contract_review"


class ResponseObligation(StrEnum):
    ANSWER = "answer"
    ACKNOWLEDGE = "acknowledge"
    SOCIAL_REPLY = "social_reply"
    OPTIONAL = "optional"
    SILENCE_ALLOWED = "silence_allowed"


class QuestionPolicy(StrEnum):
    FORBIDDEN = "forbidden"
    OPTIONAL = "optional"
    ENCOURAGED = "encouraged"
    REQUIRED = "required"


class AnswerMode(StrEnum):
    """*How* a reply responds to the question, not whether it is true.

    A direct answer is not "returns a value of the expected shape". It is
    "returns a conclusion about what was asked". On real hardware the reviewer
    invented the first rule for itself:

        USER: 何歳？
        YUI:  年齢という概念はわたしの存在にはありません。

    — which addresses the question completely, and was rejected as
    `direct_answer_missing` because it contained no number. Repaired, rejected
    again for the same reason, suppressed. The same shape took out
    「昔のことっていつまで思い出せる？」, where the honest answer is that there
    is no fixed limit and the reviewer wanted a duration.

    So the model no longer decides fulfilment. It classifies the *form* of the
    response against this closed set, and Python decides whether that form
    satisfies the contract.
    """

    #: A value, a yes/no, a name, a state, an explanation — the asked-for thing.
    VALUE_OR_PROPOSITION = "value_or_proposition"
    #: The concept in the question does not apply to its subject, said directly.
    NOT_APPLICABLE = "not_applicable"
    #: Directly says she does not know or cannot establish it.
    UNKNOWN = "unknown"
    #: Directly says the information needed to answer is not available.
    UNAVAILABLE = "unavailable"
    #: Directly says the current records cannot settle it.
    AUTHORITY_LIMITED = "authority_limited"
    #: Directly says there is no fixed value — it depends on conditions.
    CONDITIONAL_OR_VARIABLE = "conditional_or_variable"
    #: Asks back, changes the subject, greets, restates the question, or talks
    #: around the topic without reaching a conclusion.
    NON_ANSWER = "non_answer"


#: Forms that reach a conclusion about the question. Everything except the one
#: that does not, spelled as a set so adding a mode forces the decision.
ANSWERING_MODES: frozenset[AnswerMode] = frozenset(
    mode for mode in AnswerMode if mode is not AnswerMode.NON_ANSWER
)

#: Answering by declining to state the fact. Honest, and still an abstention —
#: so a contract that forbids abstaining is not satisfied by one.
#:
#: `not_applicable` and `conditional_or_variable` are deliberately *not* here.
#: 「年齢は存在しない」 and 「決まった上限はなく状況による」 are substantive
#: answers about the subject of the question; nothing is being withheld.
ABSTAINING_MODES: frozenset[AnswerMode] = frozenset(
    {AnswerMode.UNKNOWN, AnswerMode.UNAVAILABLE, AnswerMode.AUTHORITY_LIMITED}
)


class AnswerTarget(StrEnum):
    NONE = "none"
    DIRECT_USER_QUESTION = "direct_user_question"
    IDENTITY = "identity"
    GENERAL_MEMORY_CAPABILITY = "general_memory_capability"
    SPECIFIC_MEMORY_RECALL = "specific_memory_recall"
    USER_FACT = "user_fact"
    WORLD_FACT = "world_fact"
    OTHER = "other"


_QUESTION_POLICIES = {
    "none": QuestionPolicy.FORBIDDEN,
    "optional": QuestionPolicy.OPTIONAL,
    "useful": QuestionPolicy.ENCOURAGED,
    "necessary": QuestionPolicy.REQUIRED,
}

_TARGET_EVIDENCE: dict[AnswerTarget, tuple[str, ...]] = {
    AnswerTarget.IDENTITY: (),
    AnswerTarget.GENERAL_MEMORY_CAPABILITY: ("memory_authority",),
    AnswerTarget.SPECIFIC_MEMORY_RECALL: ("subjective_memory",),
    AnswerTarget.USER_FACT: ("verified_user_fact", "objective_event", "subjective_memory"),
    AnswerTarget.WORLD_FACT: ("world_state", "semantic_memory", "tool_call"),
    AnswerTarget.DIRECT_USER_QUESTION: (),
    AnswerTarget.OTHER: (),
    AnswerTarget.NONE: (),
}

_TARGET_SOURCES: dict[AnswerTarget, tuple[str, ...]] = {
    AnswerTarget.GENERAL_MEMORY_CAPABILITY: ("memory_authority_facts",),
    AnswerTarget.SPECIFIC_MEMORY_RECALL: ("recalled_subjective_memories",),
    AnswerTarget.USER_FACT: ("verified_user_facts", "recent_objective_events"),
    AnswerTarget.WORLD_FACT: ("current_world", "known_semantic_memories"),
}


@dataclass(frozen=True, slots=True)
class ResponseContract:
    """What this reply must accomplish, carried unchanged through the turn."""

    response_obligation: ResponseObligation = ResponseObligation.OPTIONAL
    primary_intent: str = "acknowledge"
    answer_target: AnswerTarget = AnswerTarget.NONE
    question_policy: QuestionPolicy = QuestionPolicy.FORBIDDEN
    factual_scope: str = "none"
    allowed_evidence_kinds: tuple[str, ...] = ()
    must_preserve: tuple[str, ...] = ()
    may_abstain: bool = True
    source_availability: tuple[tuple[str, str], ...] = ()

    @property
    def requires_answer(self) -> bool:
        return self.response_obligation is ResponseObligation.ANSWER

    @property
    def allows_question(self) -> bool:
        return self.question_policy is not QuestionPolicy.FORBIDDEN

    @property
    def acceptable_answer_modes(self) -> tuple[AnswerMode, ...]:
        """The response forms that discharge this contract.

        Carried on the contract rather than known only to the reviewer, so the
        realizer and the repair see the same list the review will apply. The
        failure this closes is a repair told only "you did not answer",
        concluding it must invent a value it does not have.
        """
        modes = ANSWERING_MODES
        if not self.may_abstain:
            modes = modes - ABSTAINING_MODES
        return tuple(mode for mode in AnswerMode if mode in modes)

    def render(self) -> str:
        evidence = ", ".join(self.allowed_evidence_kinds) or "none"
        preserve = "; ".join(self.must_preserve) or "none"
        availability = ", ".join(
            f"{name}={state}" for name, state in self.source_availability
        ) or "unknown"
        lines = [
            f"acceptable_answer_forms: "
            + ", ".join(mode.value for mode in self.acceptable_answer_modes)
        ] if self.requires_answer else []
        return "\n".join(
            (
                f"response_obligation: {self.response_obligation.value}",
                f"primary_intent: {self.primary_intent}",
                f"answer_target: {self.answer_target.value}",
                f"question_policy: {self.question_policy.value}",
                f"factual_scope: {self.factual_scope}",
                f"allowed_evidence_kinds: {evidence}",
                f"must_preserve: {preserve}",
                f"may_abstain: {'true' if self.may_abstain else 'false'}",
                f"source_availability: {availability}",
                *lines,
            )
        )


def build_response_contract(
    understanding: TurnUnderstanding,
    social: SocialInterpretation,
    *,
    user_text: str,
    grounding: Any = None,
) -> ResponseContract:
    """Derive the contract from the two existing turn readings.

    No prose matching decides memory intent here.  The discourse interpreter
    supplies the semantic category; Python only maps that closed category to a
    reply obligation and to the evidence kinds that the factual authority
    already accepts.
    """

    direct = (
        understanding.question_target not in ("none", "unclear")
        or social.primary_move == "answer"
        or looks_like_question(user_text)
    )
    if direct:
        obligation = ResponseObligation.ANSWER
    elif understanding.user_intent == "greet":
        obligation = ResponseObligation.SOCIAL_REPLY
    elif social.primary_move in ("acknowledge", "react", "support", "repair"):
        obligation = ResponseObligation.ACKNOWLEDGE
    elif social.wants_to_speak in ("silence", "leave_space"):
        obligation = ResponseObligation.SILENCE_ALLOWED
    else:
        obligation = ResponseObligation.OPTIONAL

    target = _answer_target(understanding, direct=direct)
    question_policy = _QUESTION_POLICIES.get(
        social.question, QuestionPolicy.FORBIDDEN
    )
    # A greeting is a semantic social category, not a phrase whitelist.  An
    # optional light question is allowed even when no question is required.
    if understanding.user_intent == "greet" and question_policy is QuestionPolicy.FORBIDDEN:
        question_policy = QuestionPolicy.OPTIONAL
    elif (
        understanding.user_intent != "greet"
        and question_policy is QuestionPolicy.OPTIONAL
        and social.initiative != "high"
    ):
        # Preserve the established low-pressure rule for tired/heavy turns.
        # The diagnostic false positive was specifically the greeting contract,
        # not a request to turn every optional social hint into a question.
        question_policy = QuestionPolicy.FORBIDDEN
    if social.is_repair:
        question_policy = QuestionPolicy.FORBIDDEN

    sources = _TARGET_SOURCES.get(target, ())
    availability = tuple(
        (name, _source_status(grounding, name)) for name in sources
    )
    must_preserve: list[str] = []
    if obligation is ResponseObligation.ANSWER:
        must_preserve.append("answer the USER's question directly")
    if target is AnswerTarget.GENERAL_MEMORY_CAPABILITY:
        must_preserve.extend(
            (
                "answer only about general memory capability",
                "do not add a specific recalled episode without current-turn recall",
            )
        )
    elif target is AnswerTarget.SPECIFIC_MEMORY_RECALL:
        must_preserve.append("use only memories recalled on this turn")

    return ResponseContract(
        response_obligation=obligation,
        primary_intent=social.primary_move,
        answer_target=target,
        question_policy=question_policy,
        factual_scope=_factual_scope(target),
        allowed_evidence_kinds=_TARGET_EVIDENCE[target],
        must_preserve=tuple(must_preserve),
        may_abstain=obligation is ResponseObligation.ANSWER,
        source_availability=availability,
    )


def _answer_target(
    understanding: TurnUnderstanding, *, direct: bool
) -> AnswerTarget:
    if understanding.question_target == "yui_identity":
        return AnswerTarget.IDENTITY
    if understanding.question_target == "yui_memory":
        if understanding.memory_query_intent in ("general_capability", "memory_range"):
            return AnswerTarget.GENERAL_MEMORY_CAPABILITY
        if understanding.memory_query_intent in ("specific_recall", "memory_content"):
            return AnswerTarget.SPECIFIC_MEMORY_RECALL
        # Unknown memory questions take the stricter path.  Treating one as a
        # general capability question would make authority facts answer a
        # concrete-recall claim.
        return AnswerTarget.SPECIFIC_MEMORY_RECALL
    if understanding.question_target == "user_fact":
        return AnswerTarget.USER_FACT
    if understanding.question_target == "world_fact":
        return AnswerTarget.WORLD_FACT
    if direct:
        return AnswerTarget.DIRECT_USER_QUESTION
    return AnswerTarget.NONE


def _factual_scope(target: AnswerTarget) -> str:
    return {
        AnswerTarget.IDENTITY: "identity",
        AnswerTarget.GENERAL_MEMORY_CAPABILITY: "memory_system",
        AnswerTarget.SPECIFIC_MEMORY_RECALL: "current_recall",
        AnswerTarget.USER_FACT: "user",
        AnswerTarget.WORLD_FACT: "world",
    }.get(target, "none")


def _source_status(grounding: Any, section: str) -> str:
    if grounding is None:
        return "unavailable"
    return str(getattr(grounding, "status", lambda _name: "unknown")(section))


class ResponseContractAssessment(BaseModel):
    """What the reply responds to, and in what form. Not whether that suffices.

    There is deliberately no ``fulfilled`` field. The model used to return one,
    and it wrote its own answer requirements into it — "no number, so not
    fulfilled" — which no prompt asked for and nothing could override. What is
    asked for now is a classification against two closed vocabularies, and
    Python reads the contract to decide what they add up to.

    ``extra="ignore"`` rather than ``forbid`` so a model that still emits
    ``fulfilled`` is not a schema failure: the key is simply dropped, which is
    the same thing as having no authority.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: What the reply actually addressed.
    addressed_target: AnswerTarget = AnswerTarget.OTHER
    #: How it responded to it.
    answer_mode: AnswerMode = AnswerMode.NON_ANSWER
    reason: str = Field(default="", max_length=240)


@dataclass(frozen=True, slots=True)
class ContractReviewOutcome:
    fulfilled: bool
    detail: str = ""
    unavailable: bool = False
    #: The classification behind the verdict, for the trace. Absent when the
    #: reviewer did not run.
    addressed_target: AnswerTarget | None = None
    answer_mode: AnswerMode | None = None

    def describe(self) -> str:
        if self.addressed_target is None or self.answer_mode is None:
            return self.detail
        head = f"{self.addressed_target.value}/{self.answer_mode.value}"
        return f"{head}: {self.detail}" if self.detail else head


#: Targets that name a specific thing the reply has to be about.
SPECIFIC_ANSWER_TARGETS: frozenset[AnswerTarget] = frozenset(
    {
        AnswerTarget.IDENTITY,
        AnswerTarget.GENERAL_MEMORY_CAPABILITY,
        AnswerTarget.SPECIFIC_MEMORY_RECALL,
        AnswerTarget.USER_FACT,
        AnswerTarget.WORLD_FACT,
    }
)


def target_matches(contract: ResponseContract, addressed: AnswerTarget) -> bool:
    """Whether the reply addressed the thing the contract is about.

    ``direct_user_question`` is the fallback for "this answered the USER and
    does not sort into a narrower category". It used to be accepted against
    *any* contract, including the specific ones — so a memory answer under an
    identity contract passed as long as the model called it a direct answer.
    A contract that names a specific target now requires that target; the
    fallback only helps where nothing more specific was demanded.
    """
    if addressed is contract.answer_target:
        return True
    if contract.answer_target in SPECIFIC_ANSWER_TARGETS:
        return False
    # A generic contract — direct_user_question, other, none. Anything that
    # addressed something counts; `none` addressed nothing.
    return addressed is not AnswerTarget.NONE


def fulfils(contract: ResponseContract, assessment: ResponseContractAssessment) -> bool:
    """Python's decision, from the model's classification and the contract.

    Three questions, in order, and none of them is "did the reply contain a
    value":

        did it reach a conclusion at all
        was the conclusion about the right thing
        is this kind of conclusion one the contract accepts
    """
    if assessment.answer_mode is AnswerMode.NON_ANSWER:
        return False
    if not target_matches(contract, assessment.addressed_target):
        return False
    if assessment.answer_mode in ABSTAINING_MODES and not contract.may_abstain:
        # 「分からない」 is honest and it is still not the fact that was asked
        # for. A turn that forbids abstaining is not discharged by one.
        return False
    return True


class ResponseContractReviewer:
    """Checks answer fulfilment without deciding whether any fact is true."""

    name = "response_contract_reviewer"

    def __init__(
        self, *, prompts: PromptRegistry, structured: StructuredGenerator
    ) -> None:
        self._prompts = prompts
        self._structured = structured

    async def review(
        self,
        text: str,
        *,
        user_text: str,
        contract: ResponseContract,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> ContractReviewOutcome:
        if not contract.requires_answer:
            return ContractReviewOutcome(fulfilled=True)
        template = self._prompts.get(PROMPT_ID)
        content = template.render(
            user_message=user_text,
            response_contract=contract.render(),
            candidate_reply=text,
        )
        outcome = await self._structured.generate(
            ResponseContractAssessment,
            (LLMMessage(role="user", content=content),),
            purpose=PURPOSE,
            run_id=run_id,
            event_id=event_id,
            temperature=0.0,
            max_tokens=180,
            priority="P0",
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if not outcome.accepted or outcome.value is None:
            logger.warning("response contract review unavailable event_id=%s", event_id)
            return ContractReviewOutcome(
                fulfilled=False,
                detail="response contract review unavailable",
                unavailable=True,
            )
        value = outcome.value
        return ContractReviewOutcome(
            fulfilled=fulfils(contract, value),
            detail=value.reason[:240],
            addressed_target=value.addressed_target,
            answer_mode=value.answer_mode,
        )


__all__ = [
    "ABSTAINING_MODES",
    "ANSWERING_MODES",
    "AnswerMode",
    "AnswerTarget",
    "ContractReviewOutcome",
    "SPECIFIC_ANSWER_TARGETS",
    "PROMPT_ID",
    "PURPOSE",
    "QuestionPolicy",
    "ResponseContract",
    "ResponseContractAssessment",
    "ResponseContractReviewer",
    "ResponseObligation",
    "build_response_contract",
    "fulfils",
    "target_matches",
]
