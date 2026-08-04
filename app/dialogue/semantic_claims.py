"""Unified semantic claim review (Dialogue Understanding & Grounding v2).

The regex claim extractor answers "does this sentence look like a claim". That
question has no end: every real conversation produces a phrasing nobody listed,
and each one is one more pattern. Worse, it answers the wrong question — the
thing that matters is not the wording but the *proposition*, and the same
proposition arrives as 「読んだよ」, 「読んでた」, 「読み終えたところ」 or, in the
failure that started this, 「静かに過ごしていました」 in answer to 「今日は何してた？」.

So the division here is:

    the model     classifies the complete proposition, and proposes which
                  evidence identifiers would support it
    Python        decides whether those identifiers exist, whether they are of
                  an accepted kind, and whether they are about the right person

The model never creates evidence. It cites, and citing something that does not
exist resolves to nothing at all (GROUND-001). Its prose is not evidence
either — a confident sentence explaining why a claim is fine is still prose.

**Interpretation context is not evidence.** The reviewer is shown the USER's
message and the recent turns, because without them 「静かに過ごしていました」 is
not classifiable at all and 「19歳です」 looks like a memory claim. But nothing
the USER said can *support* a claim about YUI: the interpretation context tells
the reviewer what the sentence means, and the grounding context is the only
thing that decides whether it is true. Those are two different inputs on
purpose, and they are rendered under headings that say so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.grounding.models import (
    ACCEPTED_EVIDENCE,
    SELF_CLAIM_KINDS,
    Claim,
    ClaimKind,
    Evidence,
    GroundedClaim,
    GroundingContext,
)
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage

logger = logging.getLogger(__name__)

PROMPT_ID = "semantic_claim_review"
PURPOSE = "semantic_claim_review"

#: Whose life a claim is about. Kept separate from the claim kind because the
#: same predicate — 「詠んだ」 — is a different claim depending on who did it,
#: and losing that distinction is exactly how USER evidence ends up under a
#: YUI claim.
ClaimSubject = Literal["yui", "user", "npc", "world", "unknown"]

#: How strongly the sentence commits to the proposition. Only an assertion
#: needs evidence: a wish, a plan or a question asserts nothing.
Modality = Literal["assertion", "question", "hypothetical", "intention", "hedged"]

#: When the claim says it happened.
#:
#: Deliberately the *same* vocabulary the turn reading uses, plus "timeless".
#: Two temporal vocabularies under one name in one system is a trap: a reviewer
#: told the turn is about 「yesterday」 and given no such value to answer with
#: fails schema validation, which reads downstream as "the reviewer is
#: unavailable" and blocks a perfectly good reply.
TemporalScope = Literal[
    "now",
    "today",
    "yesterday",
    "recent_past",
    "distant_past",
    "habitual",
    "future",
    "unspecified",
    "timeless",
]


class SemanticClaimCandidate(BaseModel):
    """One proposition the reviewer found, as the reviewer understood it.

    Everything here is the model's reading. None of it is authoritative — the
    `supporting_ids` in particular are a *proposal*, resolved by Python before
    they mean anything.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: The proposition in plain words, not the surface sentence.
    proposition: str = Field(default="", max_length=300)
    #: The fragment of the draft this came from, for the trace and the repair.
    trigger: str = Field(default="", max_length=200)
    subject: ClaimSubject = "unknown"
    category: ClaimKind = "yui_completed_action"
    modality: Modality = "assertion"
    temporal_scope: TemporalScope = "timeless"
    #: Evidence the reviewer believes supports this. Identifiers only.
    supporting_ids: tuple[str, ...] = ()
    #: Evidence the reviewer believes this contradicts.
    contradicting_ids: tuple[str, ...] = ()


class SemanticClaimReview(BaseModel):
    """What the reviewer made of a draft."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    claims: tuple[SemanticClaimCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedClaim:
    """A claim after Python has had the last word.

    The reviewer's reading is kept alongside the resolution, because the
    difference between them is the interesting part: a claim the model thought
    was supported and Python could not resolve is exactly the case the whole
    mechanism exists for.
    """

    candidate: SemanticClaimCandidate
    evidence: tuple[Evidence, ...] = ()
    contradictions: tuple[Evidence, ...] = ()
    #: Why it failed, when it did. Named reasons rather than a boolean, because
    #: the repair prompt has to say something specific.
    refusals: tuple[str, ...] = ()

    @property
    def needs_evidence(self) -> bool:
        """Only an assertion claims anything."""
        return self.candidate.modality == "assertion"

    @property
    def supported(self) -> bool:
        return bool(self.evidence)

    @property
    def blocking(self) -> bool:
        return self.needs_evidence and not self.supported

    def as_claim(self) -> Claim:
        """The legacy claim shape, so existing verdict plumbing still works."""
        return Claim(
            kind=self.candidate.category,
            text=self.candidate.proposition or self.candidate.trigger,
            trigger=self.candidate.trigger or self.candidate.proposition,
        )

    def as_grounded(self) -> GroundedClaim:
        return GroundedClaim(claim=self.as_claim(), evidence=self.evidence)

    def describe(self) -> str:
        head = f"{self.candidate.category}「{self.candidate.trigger}」"
        if self.supported:
            return f"{head}: 裏づけあり"
        return f"{head}: {'; '.join(self.refusals) or '裏づけがない'}"


class EvidenceResolver:
    """Python's half of the contract: which citations actually stand.

    Four separate reasons a citation can fail, and they are kept apart because
    they mean different things to whoever reads the trace:

        unknown_evidence   the identifier does not exist. Invented.
        wrong_kind         it exists, but cannot answer this kind of claim.
        wrong_subject      it exists and is of an accepted kind, but it is
                           about somebody else.
        no_citation        the reviewer cited nothing at all.
    """

    name = "evidence_resolver"

    def resolve(
        self, candidate: SemanticClaimCandidate, context: GroundingContext | None
    ) -> ResolvedClaim:
        if context is None:
            return ResolvedClaim(
                candidate=candidate, refusals=("grounding_context_unavailable",)
            )
        if not candidate.supporting_ids:
            return ResolvedClaim(candidate=candidate, refusals=("no_citation",))

        accepted_kinds = ACCEPTED_EVIDENCE.get(candidate.category, ())
        kept: list[Evidence] = []
        refusals: list[str] = []
        for evidence_id in candidate.supporting_ids:
            found = context.by_id(evidence_id)
            if found is None:
                # The identifier was invented. This is the one that matters
                # most: a fluent model will cite something plausible-looking
                # rather than admit it has nothing.
                refusals.append(f"unknown_evidence:{evidence_id}")
                continue
            if found.kind not in accepted_kinds:
                refusals.append(f"wrong_kind:{evidence_id}")
                continue
            if candidate.category in SELF_CLAIM_KINDS and not found.is_yuis_own:
                # The ownership rule, applied to every claim about her own life
                # rather than to two of them.
                refusals.append(f"wrong_subject:{evidence_id}")
                continue
            if candidate.subject == "yui" and not found.is_yuis_own:
                refusals.append(f"wrong_subject:{evidence_id}")
                continue
            kept.append(found)

        contradictions = context.resolve_ids(candidate.contradicting_ids)
        if not kept and not refusals:
            refusals.append("no_citation")
        return ResolvedClaim(
            candidate=candidate,
            evidence=tuple(kept),
            contradictions=contradictions,
            refusals=tuple(refusals),
        )


@dataclass(frozen=True, slots=True)
class SemanticReviewOutcome:
    """Everything the review decided about one draft."""

    claims: tuple[ResolvedClaim, ...] = ()
    #: True when the reviewer could not be run at all. Not the same as "found
    #: nothing": an unavailable reviewer must not read as a clean draft.
    unavailable: bool = False
    detail: str = ""

    @property
    def blocking(self) -> tuple[ResolvedClaim, ...]:
        return tuple(claim for claim in self.claims if claim.blocking)

    @property
    def accepted(self) -> bool:
        return not self.unavailable and not self.blocking

    @property
    def supported(self) -> tuple[ResolvedClaim, ...]:
        return tuple(claim for claim in self.claims if claim.supported)

    def describe(self) -> str:
        if self.unavailable:
            return f"semantic review unavailable: {self.detail}"
        return "\n".join(claim.describe() for claim in self.claims) or "(claimなし)"


class SemanticClaimReviewer:
    """One model call that reads a draft for what it actually asserts."""

    name = "semantic_claim_reviewer"

    def __init__(
        self,
        *,
        prompts: PromptRegistry,
        structured: StructuredGenerator,
        resolver: EvidenceResolver | None = None,
    ) -> None:
        self._prompts = prompts
        self._structured = structured
        self._resolver = resolver or EvidenceResolver()

    async def review(
        self,
        text: str,
        context: GroundingContext | None,
        *,
        understanding: Any = None,
        user_text: str = "",
        recent_conversation: str = "",
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> SemanticReviewOutcome:
        """Classify the draft's propositions, then resolve the citations.

        `user_text` and `recent_conversation` are interpretation context. They
        make the sentence classifiable — 「静かに過ごしていました」 is only a claim
        about today because the question was 「今日は何してた？」 — and they are
        rendered under a heading that forbids using them as support.
        """
        template = self._prompts.get(PROMPT_ID)
        content = template.render(
            candidate_reply=text,
            user_message=user_text or "(なし)",
            recent_conversation=recent_conversation or "(なし)",
            turn_understanding=(
                understanding.render() if understanding is not None else "(なし)"
            ),
            available_evidence=render_evidence(context),
        )
        outcome = await self._structured.generate(
            SemanticClaimReview,
            (LLMMessage(role="user", content=content),),
            purpose=PURPOSE,
            run_id=run_id,
            event_id=event_id,
            # Classification, not composition. Nothing here should be creative.
            temperature=0.0,
            max_tokens=800,
            priority="P0",
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if not outcome.accepted or outcome.value is None:
            # Unavailable is not clean. Accepting on a failed classification
            # would silently switch grounding off exactly when the model is
            # having a bad time, which is when it fabricates most.
            return SemanticReviewOutcome(
                unavailable=True,
                detail=(
                    "no output"
                    if outcome.failure is None
                    else f"{outcome.failure.reason_code}: {outcome.failure.detail}"[:200]
                ),
            )
        resolved = tuple(
            self._resolver.resolve(candidate, context)
            for candidate in outcome.value.claims
        )
        return SemanticReviewOutcome(claims=resolved)


def render_evidence(context: GroundingContext | None, limit: int = 40) -> str:
    """Every citable identifier, attributed, grouped by section.

    Attributed because a summary without its owner is the bug this file exists
    to close, and grouped because the reviewer needs to see that a
    ``verified_user_fact`` is a fact about the USER before it cites one under a
    claim about YUI.
    """
    if context is None or context.is_empty:
        return "(このターンで確かなことは記録されていない)"
    from app.grounding.models import GROUNDING_SECTIONS

    lines: list[str] = []
    for section in GROUNDING_SECTIONS:
        items = getattr(context, section)
        if not items:
            continue
        lines.append(f"## {section}")
        for item in items[:limit]:
            lines.append(f"- {item.describe()}")
    return "\n".join(lines) or "(このターンで確かなことは記録されていない)"


__all__ = [
    "ClaimSubject",
    "EvidenceResolver",
    "Modality",
    "PROMPT_ID",
    "PURPOSE",
    "ResolvedClaim",
    "SemanticClaimCandidate",
    "SemanticClaimReview",
    "SemanticClaimReviewer",
    "SemanticReviewOutcome",
    "TemporalScope",
    "render_evidence",
]
