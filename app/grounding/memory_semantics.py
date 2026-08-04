"""Semantic grounding for claims about YUI's memory.

Memory claims are meaning categories, not a growing list of Japanese surface
forms.  A structured reviewer classifies the complete proposition and cites
only evidence identifiers supplied by this module.  Python then resolves those
identifiers against the current turn's recalled memories or immutable facts
about how the Memory subsystem actually behaves.

The reviewer's prose is never evidence.  It may cite an existing identifier;
it cannot create one (GROUND-001).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.grounding.claims import GroundingVerdict
from app.grounding.models import Claim, Evidence, GroundedClaim, GroundingContext
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage

PROMPT_ID = "memory_claim_review"
PURPOSE = "memory_claim_review"

MemorySemanticCategory = Literal[
    "specific_recall_affirmation",
    "general_memory_capability",
]


# These are implementation facts, not personality lore.  Their identifiers are
# stable so the semantic reviewer can cite them, while Python remains the
# authority on whether a cited identifier exists.
AUTHORITATIVE_MEMORY_FACTS: tuple[Evidence, ...] = (
    Evidence(
        kind="memory_authority",
        reference="memory.selective_fallible",
        summary=(
            "記憶の想起は選択的で不確実であり、保存された内容のすべてを"
            "必ず思い出せるわけではない"
        ),
        subject="world",
    ),
    Evidence(
        kind="memory_authority",
        reference="memory.dynamic_accessibility",
        summary=(
            "保存されている記憶でもアクセス可能性は変化し、思い出せなくなる"
            "ことや忘却されることがある。保存は永久想起を保証しない"
        ),
        subject="world",
    ),
    Evidence(
        kind="memory_authority",
        reference="memory.current_recall_required",
        summary=(
            "具体的な過去の出来事や事実を覚えていると肯定できるのは、"
            "その記憶が現在のターンで実際にrecalledされた場合だけである"
        ),
        subject="world",
    ),
)


class SemanticMemoryClaim(BaseModel):
    """One memory proposition classified by meaning, independent of wording."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: MemorySemanticCategory
    assertion: str = Field(min_length=1, max_length=300)
    supporting_ids: tuple[str, ...] = Field(default=(), max_length=8)
    contradicting_ids: tuple[str, ...] = Field(default=(), max_length=8)


class SemanticMemoryReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: tuple[SemanticMemoryClaim, ...] = Field(default=(), max_length=4)


class SemanticMemoryGroundingGuard:
    """Classify memory meaning, then resolve it against authoritative facts."""

    name = "semantic_memory_grounding_guard"

    def __init__(
        self,
        *,
        structured: StructuredGenerator,
        prompts: PromptRegistry,
    ) -> None:
        self._structured = structured
        self._prompts = prompts

    async def review(
        self,
        text: str,
        context: GroundingContext,
        *,
        user_text: str = "",
        recent_conversation: str = "",
        understanding: object | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> GroundingVerdict:
        """Classify the draft's memory claims, with enough context to do it.

        The interpretation context is new, and it closes a real failure. Shown
        only the draft, this reviewer read 「19歳です」 as a claim to *remember*
        being nineteen and demanded a recalled memory for it. Shown the question
        it answers — 「何歳？」 — it is a self fact, and memory recall has nothing
        to do with it.

        The context makes the sentence classifiable. It never supports it: what
        the USER said cannot be evidence for what YUI remembers, and the prompt
        renders the two under headings that say which is which.
        """
        template = self._prompts.get(PROMPT_ID)
        outcome = await self._structured.generate(
            SemanticMemoryReview,
            (
                LLMMessage(
                    role="user",
                    content=template.render(
                        candidate_reply=text,
                        user_message=user_text or "(なし)",
                        recent_conversation=recent_conversation or "(なし)",
                        turn_understanding=(
                            understanding.render()
                            if understanding is not None
                            else "(なし)"
                        ),
                        recalled_memories=_render_evidence(
                            context.recalled_subjective_memories
                        ),
                        authoritative_memory_facts=_render_evidence(
                            context.memory_authority_facts
                        ),
                    ),
                ),
            ),
            purpose=PURPOSE,
            run_id=run_id,
            event_id=event_id,
            temperature=0.0,
            max_tokens=500,
            priority="P0",
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if not outcome.accepted or outcome.value is None:
            # Classification is the only gate for this semantic category.  If
            # it is unavailable, accepting would silently disable grounding.
            return GroundingVerdict(
                claims=(
                    GroundedClaim(
                        claim=Claim(
                            kind="yui_memory_claim",
                            text=text,
                            trigger="semantic memory review unavailable",
                            severity="hard",
                        )
                    ),
                )
            )
        return resolve_semantic_memory_review(outcome.value, context)


def resolve_semantic_memory_review(
    review: SemanticMemoryReview,
    context: GroundingContext,
) -> GroundingVerdict:
    """Resolve only identifiers from authoritative inputs for this turn."""

    recalled = {item.reference: item for item in context.recalled_subjective_memories}
    authority = {item.reference: item for item in context.memory_authority_facts}
    grounded: list[GroundedClaim] = []

    for semantic in review.claims:
        source = (
            recalled
            if semantic.category == "specific_recall_affirmation"
            else authority
        )
        contradictions = tuple(
            authority[item_id]
            for item_id in semantic.contradicting_ids
            if item_id in authority
        )
        evidence = tuple(
            source[item_id]
            for item_id in semantic.supporting_ids
            if item_id in source
        )
        # A cited contradiction defeats a supporting citation.  Unknown IDs do
        # nothing: the reviewer cannot manufacture authority by naming it.
        if contradictions:
            evidence = ()
        grounded.append(
            GroundedClaim(
                claim=Claim(
                    kind="yui_memory_claim",
                    text=semantic.assertion,
                    trigger=semantic.category,
                    severity="hard",
                ),
                evidence=evidence,
            )
        )
    return GroundingVerdict(claims=tuple(grounded))


def _render_evidence(items: tuple[Evidence, ...]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- {item.reference}: {item.summary}" for item in items)


__all__ = [
    "AUTHORITATIVE_MEMORY_FACTS",
    "SemanticMemoryClaim",
    "SemanticMemoryGroundingGuard",
    "SemanticMemoryReview",
    "resolve_semantic_memory_review",
]
