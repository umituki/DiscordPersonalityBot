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
import json
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.grounding.models import (
    ACCEPTED_EVIDENCE,
    RUNTIME_CLAIM_KINDS,
    Claim,
    Evidence,
    GroundedClaim,
    GroundingContext,
)
from app.grounding.ownership import (
    declared_subject_is_consistent,
    may_support,
    refusal_reason,
)
from app.world.scope import (
    InteractionScope,
    is_reachable,
    validate_interaction,
)
from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage

logger = logging.getLogger(__name__)

PROMPT_ID = "semantic_claim_review"
PURPOSE = "semantic_claim_review"

#: Audit finding 2 (round 2). Categories that assert she is *currently*
#: remembering something. For these a stored memory row is not enough: the row
#: has to be one this turn's retrieval actually returned.
#:
#: Only ``yui_specific_memory_recall`` is here, and that is now exact rather
#: than approximate — it accepts ``subjective_memory`` and nothing else, so
#: "cited evidence" and "cited a memory" are the same statement. The retired
#: ``yui_memory_claim`` used to be in this set too, with a kind guard bolted on
#: because it accepted four kinds; it is unreachable now, which removes the
#: need for the guard and the hole the guard left.
RECALL_REQUIRED_CATEGORIES: frozenset[str] = frozenset(
    {"yui_specific_memory_recall"}
)

#: Whose life a claim is about. Kept separate from the claim kind because the
#: same predicate — 「詠んだ」 — is a different claim depending on who did it,
#: and losing that distinction is exactly how USER evidence ends up under a
#: YUI claim.
ClaimSubject = Literal["yui", "user", "npc", "world", "unknown"]

#: The categories a new draft's claims may be classified as.
#:
#: Spelled out rather than derived from ``RUNTIME_CLAIM_KINDS``, because a
#: ``Literal`` has to be a literal for the schema the model is shown to
#: constrain anything. The two are checked against each other at import time
#: below, so they cannot drift.
#:
#: `yui_memory_claim` is deliberately absent. A memory statement is either
#: 「この出来事を思い出せている」 (`yui_specific_memory_recall`, settled only by
#: a memory recalled on this turn) or 「記憶とはこういうものだ」
#: (`yui_general_memory_capability`, settled only by the Memory subsystem's own
#: facts). The alias was neither, and accepted the union of both — so choosing
#: it bought a looser rule than either real category allows.
RuntimeClaimKind = Literal[
    "yui_completed_action",
    "yui_experience_habit",
    "yui_perception",
    "yui_specific_memory_recall",
    "yui_general_memory_capability",
    "yui_identity_fact",
    "user_past_fact",
    "npc_fact",
    "tool_use",
    "external_knowledge_claim",
    "current_world_fact",
]

assert set(RuntimeClaimKind.__args__) == set(RUNTIME_CLAIM_KINDS), (  # type: ignore[attr-defined]
    "the reviewer schema and the runtime claim kinds have drifted apart"
)

#: How strongly the proposition is committed to (audit finding 5).
#:
#: The old set had a bare ``hedged``, and the rule was "hedged needs no
#: evidence". That is wrong in the direction that matters, because Japanese
#: softens assertions constantly:
#:
#:     「わたしなら好きかも」        a supposition about a situation that did
#:                                  not happen — nothing is being asserted
#:     「読むのが好きなようです」    a softened statement about her actual
#:                                  present preference — asserted, needs a
#:                                  record
#:     「前に読んだ気がする」        an uncertain claim about her actual past —
#:                                  asserted with low confidence, and the
#:                                  honest alternative is 「思い出せない」
#:
#: Surface hedging cannot tell these apart; the proposition can. So the
#: reviewer classifies what is being committed to, and the hedge words are not
#: consulted by Python at all.
Modality = Literal[
    # Committed to as fact.
    "assertion",
    # Committed to as fact, softened. Still needs evidence.
    "hedged_assertion",
    # Committed to as fact, with stated uncertainty about recall. Needs
    # evidence; without it the honest reply is that she cannot remember.
    "uncertain_recall",
    # Not committed to: counterfactual, supposition, "if it were me".
    "hypothetical",
    # Not committed to: a plan or a wish about the future.
    "intention",
    # Not committed to: asking, not telling.
    "question",
    # Legacy value from before the split. Accepted so older stored reviews
    # still parse, and treated as committing — see LEGACY_HEDGED.
    "hedged",
]

#: Modalities that commit to something being true, and therefore owe evidence.
#: Kept as an explicit set rather than ``!= "assertion"`` so that adding a
#: modality forces a decision about which side it falls on.
COMMITTING_MODALITIES: frozenset[str] = frozenset(
    {"assertion", "hedged_assertion", "uncertain_recall", "hedged"}
)

#: Legacy value, kept only so an older reviewer output still parses. Mapped to
#: the *strict* side: a bare "hedged" of unknown intent is treated as asserting
#: something, because the failure direction there is refusing to send rather
#: than fabricating.
LEGACY_HEDGED = "hedged"

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


#: What kind of contact the proposition describes.
#:
#: YUI lives in her own world and the USER lives in theirs. She reads, sleeps
#: and talks to the people around her like anyone else — that is not the
#: constraint. The constraint is that nothing physical crosses between the two
#: lives: they cannot be in the same room, and neither can hand the other
#: anything.
#:
#: Classified rather than pattern-matched, because 「本を読んだ」 and
#: 「USERの肩に触れた」 are both physical and only one is impossible. The old
#: guard sorted by physicality and therefore caught the wrong sentences in both
#: directions: it blocked her from having eaten lunch and had nothing to say
#: about her sitting next to the USER.
InteractionScopeLiteral = Literal[
    "local_to_subject_world",
    "shared_communication",
    "cross_world_physical",
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
    #: Restricted to the runtime kinds, so the retired alias cannot be chosen.
    #: A reviewer that returns it fails schema validation, which reads
    #: downstream as "the reviewer is unavailable" — the fail-closed direction.
    category: RuntimeClaimKind = "yui_completed_action"
    modality: Modality = "assertion"
    #: Where the described contact takes place. **Required, with no default.**
    #:
    #: It defaulted to `local_to_subject_world`, and that made the world
    #: boundary fail open: a reviewer that omitted the field — an older model,
    #: a truncated response, a schema it had not been shown — had
    #: 「YUIがUSERと直接会った」 silently classified as something happening in
    #: her own room, and a YUI-owned activity then supported it. A hard
    #: boundary whose absent value is its permissive value is not a boundary.
    #:
    #: Omitting it now fails schema validation, which reads downstream as "the
    #: reviewer is unavailable" — the fail-closed direction.
    interaction_scope: InteractionScopeLiteral
    #: Who else the proposition involves, in the same closed subject vocabulary.
    #: Empty for a single-subject action: 「本を読んだ」 involves nobody.
    #:
    #: Required for the same reason, and load-bearing for a different one: one
    #: classified field is one point of failure, so Python checks this against
    #: the scope rather than trusting either alone.
    participants: tuple[ClaimSubject, ...]
    temporal_scope: TemporalScope = "timeless"
    #: Evidence the reviewer believes supports this. Identifiers only.
    supporting_ids: tuple[str, ...] = ()
    #: Evidence the reviewer believes this contradicts.
    contradicting_ids: tuple[str, ...] = ()


class StoredSemanticClaim(SemanticClaimCandidate):
    """A claim read back from a Common Ground row.

    Same shape, different provenance, and that changes which fields may be
    absent. `interaction_scope` and `participants` are required of a *new*
    classification — a reviewer that omits them has not answered, and the world
    boundary must not read silence as permission. A row written before those
    fields existed is a different case entirely: it is being read, not
    classified, and refusing it would send the re-verification path back to the
    legacy regex parser for every claim recorded before this change.

    So the defaults live here and only here. Nothing the model produces is
    validated against this class.
    """

    interaction_scope: InteractionScopeLiteral = "local_to_subject_world"
    participants: tuple[ClaimSubject, ...] = ()


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
        """Whether this proposition commits to something being true.

        Decided by the classified modality, never by hedge words in the
        surface: 「〜のようです」 about her actual preference commits, and
        「わたしなら〜かも」 about a situation that did not happen does not.
        """
        return self.candidate.modality in COMMITTING_MODALITIES

    @property
    def contradicted(self) -> bool:
        """An authoritative record says this is not so (audit finding 3)."""
        return bool(self.contradictions)

    @property
    def supported(self) -> bool:
        """Backed, and not contradicted.

        Both halves. Positive evidence does not outvote a contradiction — a
        claim with one of each is not "supported with a caveat", it is a claim
        the record disagrees with.
        """
        return bool(self.evidence) and not self.contradicted

    @property
    def verdict(self) -> str:
        """``supported`` / ``contradicted`` / ``unsupported``.

        Three states rather than a boolean, because "we found nothing" and
        "we found the opposite" call for different repairs.
        """
        if self.contradicted:
            return "contradicted"
        if self.evidence:
            return "supported"
        return "unsupported"

    @property
    def blocking(self) -> bool:
        return self.needs_evidence and not self.supported

    def as_claim(self) -> Claim:
        """The legacy claim shape, so existing verdict plumbing still works.

        ``severity`` carries `blocking` across the boundary. `GroundingVerdict`
        holds a send for an unsupported claim only when it is ``hard``, which
        is the same question `needs_evidence` answers — so mapping one to the
        other makes `GroundingVerdict.blocking` reproduce `ResolvedClaim`'s
        answer exactly instead of approximating it in both directions: a
        contradicted assertion used to slip through, and an unsupported
        hypothetical used to hold a reply it had no business holding.
        """
        return Claim(
            kind=self.candidate.category,
            text=self.candidate.proposition or self.candidate.trigger,
            trigger=self.candidate.trigger or self.candidate.proposition,
            severity="hard" if self.needs_evidence else "soft",
        )

    def as_grounded(self) -> GroundedClaim:
        """The resolver's answer, not the ingredients for another one."""
        return GroundedClaim(
            claim=self.as_claim(),
            evidence=self.evidence,
            verdict=self.verdict,
            contradictions=self.contradictions,
        )

    def describe(self) -> str:
        head = f"{self.candidate.category}「{self.candidate.trigger}」"
        if self.supported:
            return f"{head}: 裏づけあり"
        return f"{head}: {'; '.join(self.refusals) or '裏づけがない'}"


@dataclass(frozen=True, slots=True)
class Admission:
    """Whether one cited identifier may speak to one claim category."""

    evidence: Evidence | None = None
    #: The named reason, ready to append to a claim's refusals. Empty when the
    #: evidence was admitted.
    refusal: str = ""


def admit(category: str, evidence_id: str, context: GroundingContext) -> Admission:
    """The one test for whether a citation has any authority over a category.

    Round 3, finding 2. There were two paths through these checks: supporting
    identifiers went through all four, and contradicting identifiers went
    through ownership alone — so a YUI-owned ``tool_call`` could refute
    「本を読んだ」, a claim it is not admissible evidence *for*. Authority to
    overturn a claim cannot be broader than authority to establish it, and the
    reliable way to say that is to run one function rather than two copies.

    Four ways to fail, kept apart because they mean different things to
    whoever reads the trace:

        unknown_evidence        the identifier does not exist. Invented.
        wrong_kind              it exists, but cannot answer this category.
        wrong_subject           right kind, somebody else's life.
        wrong_relation          right subject, wrong standing to the event.
        not_recalled_this_turn  a memory row nobody retrieved this turn.
    """
    normalized_id = normalize_evidence_id(evidence_id)
    found = context.by_id(normalized_id)
    if found is None:
        # The one that matters most: a fluent model will cite something
        # plausible-looking rather than admit it has nothing.
        return Admission(refusal=f"unknown_evidence:{evidence_id}")
    if found.kind not in ACCEPTED_EVIDENCE.get(category, ()):  # type: ignore[arg-type]
        return Admission(refusal=f"wrong_kind:{evidence_id}")
    # Audit finding 1: the full matrix, in both directions. Her record cannot
    # settle the USER's past any more than theirs can settle hers, and a world
    # event is not something she did.
    if not may_support(category, subject=found.subject, relation=found.relation):
        reason = refusal_reason(
            category, subject=found.subject, relation=found.relation
        )
        return Admission(refusal=f"{reason}:{evidence_id}")
    # Audit finding 2 (round 2). A recollection needs a row that was recalled
    # *on this turn*. A stored memory nobody retrieved is not something she is
    # remembering.
    if (
        category in RECALL_REQUIRED_CATEGORIES
        and found not in context.recalled_subjective_memories
    ):
        return Admission(refusal=f"not_recalled_this_turn:{evidence_id}")
    return Admission(evidence=found)


def normalize_evidence_id(evidence_id: str) -> str:
    """Remove display-only ASCII wrappers, and nothing semantic.

    The result is still resolved by exact lookup in the current context.  No
    fuzzy, substring or nearest-ID matching is performed, so an invented ID
    stays invented after normalization.
    """

    value = str(evidence_id).strip()
    wrappers = (("[", "]"), ('"', '"'), ("'", "'"), ("`", "`"))
    for _ in range(2):
        for left, right in wrappers:
            if len(value) >= 2 and value.startswith(left) and value.endswith(right):
                value = value[len(left) : len(value) - len(right)].strip()
                break
        else:
            break
    return value


class EvidenceResolver:
    """Python's half of the contract: which citations actually stand.

    The per-citation test lives in :func:`admit`, which both the supporting and
    the contradicting identifiers go through. What is left here is the shape of
    the claim itself — its category, its subject, and what a contradiction does
    to the verdict.
    """

    name = "evidence_resolver"

    def resolve(
        self, candidate: SemanticClaimCandidate, context: GroundingContext | None
    ) -> ResolvedClaim:
        if context is None:
            return ResolvedClaim(
                candidate=candidate, refusals=("grounding_context_unavailable",)
            )

        # The retired alias never resolves. The schema already prevents the
        # reviewer choosing it, and this is the second layer: whatever route a
        # legacy category arrives by — a widened Literal, a row deserialized
        # into a candidate, `model_construct` — it does not get evidence. Old
        # rows are read, not re-grounded.
        if candidate.category not in RUNTIME_CLAIM_KINDS:
            return ResolvedClaim(
                candidate=candidate,
                refusals=(f"legacy_category:{candidate.category}",),
            )

        # Where the interaction happened, and with whom, have to agree with each
        # other before either is believed. This is not "there is no record of
        # it" — it is that there is no arrangement of records under which the
        # claim could be true, so it runs before the citations are looked at,
        # the same way a self-contradictory category does.
        #
        # The audit's reproduction: scope said `local_to_subject_world` for
        # 「YUIがUSERと直接会った」, a YUI-owned activity was cited, and the
        # claim resolved as supported. The scope was the only thing standing
        # there. Now the participants stand beside it, and a scope that says
        # "in her own world" while naming the USER is a contradiction Python
        # can see without reading Japanese.
        #
        # Only for claims that commit to something. She may still wish they
        # could meet, or wonder what it would be like; a hypothetical is not an
        # assertion that it happened.
        if candidate.modality in COMMITTING_MODALITIES:
            refusal = validate_interaction(
                subject=candidate.subject,
                participants=candidate.participants,
                scope=candidate.interaction_scope,
            )
            if refusal:
                return ResolvedClaim(
                    candidate=candidate,
                    refusals=(f"{refusal}:{candidate.category}",),
                )

        # A claim that commits to something being true has to say whose life it
        # is about. `unknown` is not a neutral answer here: it is the one value
        # that agrees with every category, so leaving it open would let a
        # reviewer that could not decide between 「USERが読んだ」 and
        # 「YUIが読んだ」 have the category pick for it — and the category is
        # the half the model was least careful about. Non-committing claims may
        # leave it open, because they are not asserting anything about anyone.
        if candidate.modality in COMMITTING_MODALITIES and (
            not candidate.subject or candidate.subject == "unknown"
        ):
            return ResolvedClaim(
                candidate=candidate,
                refusals=(f"subject_required:{candidate.category}",),
            )

        # Audit finding 1 (round 2). The claim has to agree with itself before
        # any evidence is looked at. A `user_past_fact` declared to be about
        # YUI is a self-contradictory classification, and picking whichever
        # half suits the available evidence is how USER-owned rows ended up
        # under a claim about her. Fail closed.
        if not declared_subject_is_consistent(candidate.category, candidate.subject):
            return ResolvedClaim(
                candidate=candidate,
                refusals=(
                    f"category_subject_mismatch:{candidate.category}/{candidate.subject}",
                ),
            )

        if not candidate.supporting_ids:
            return ResolvedClaim(candidate=candidate, refusals=("no_citation",))

        kept: list[Evidence] = []
        refusals: list[str] = []
        for evidence_id in candidate.supporting_ids:
            admission = admit(candidate.category, evidence_id, context)
            if admission.evidence is None:
                refusals.append(admission.refusal)
                continue
            kept.append(admission.evidence)

        # Audit finding 3 (round 2). A contradiction is a verdict, not a note.
        # Having found something that supports a claim says nothing about the
        # thing that contradicts it, and reporting `supported` while an
        # authoritative record says otherwise is the worst of both.
        #
        # Round 3, finding 2: through the *same* admissibility test. Overriding
        # a claim is at least as consequential as supporting one, so a record
        # that could not have supported the claim cannot refute it either — a
        # tool call is not an opinion about whether she read a book. Sharing
        # `admit` rather than repeating three of its four checks is what keeps
        # the two directions from drifting again.
        contradictions = tuple(
            admission.evidence
            for admission in (
                admit(candidate.category, evidence_id, context)
                for evidence_id in candidate.contradicting_ids
            )
            if admission.evidence is not None
        )
        if contradictions:
            refusals.append(
                "contradicted:" + ",".join(item.evidence_id for item in contradictions)
            )
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

    Attributed because a summary without its owner is the bug this module
    exists to close, and grouped because the reviewer needs to see that a
    ``verified_user_fact`` is a fact about the USER before citing one under a
    claim about YUI.

    Audit finding 9: a section whose source could not be read says so. An empty
    section means the record is genuinely empty and a claim needing it is
    unsupported; an unreadable one means nothing is known either way, and the
    reviewer must not read the absence as a fact.
    """
    if context is None:
        return "(このターンの記録を読めなかった)"
    from app.grounding.models import GROUNDING_SECTIONS

    lines: list[str] = []
    unreadable: list[str] = []
    for section in GROUNDING_SECTIONS:
        if context.status(section) == "unavailable":
            unreadable.append(section)
            continue
        items = getattr(context, section)
        if not items:
            continue
        lines.append(f"## {section}")
        for item in items[:limit]:
            lines.append(
                json.dumps(
                    {
                        "id": item.evidence_id,
                        "kind": item.kind,
                        "owner": item.owner_label,
                        "subject": item.subject,
                        "relation": item.relation,
                        "summary": item.summary,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    if unreadable:
        lines.append("## 読めなかった情報源")
        lines.append(
            "- " + "、".join(unreadable) + " は読み取れなかった。"
            "「記録が無い」という意味ではないので、"
            "これらに関わる主張は裏づけ無しとして扱うこと。"
        )
    return "\n".join(lines) or "(このターンで確かなことは記録されていない)"


__all__ = [
    "ClaimSubject",
    "EvidenceResolver",
    "InteractionScopeLiteral",
    "Modality",
    "Admission",
    "PROMPT_ID",
    "PURPOSE",
    "RECALL_REQUIRED_CATEGORIES",
    "ResolvedClaim",
    "RuntimeClaimKind",
    "SemanticClaimCandidate",
    "SemanticClaimReview",
    "SemanticClaimReviewer",
    "StoredSemanticClaim",
    "SemanticReviewOutcome",
    "TemporalScope",
    "admit",
    "normalize_evidence_id",
    "render_evidence",
]
