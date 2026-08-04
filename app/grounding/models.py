"""Claims, evidence and the context they are resolved against (rebuild spec 15).

The failure this exists for is not a model that lies. It is a model that says
「今日は本を読んだ」 because the sentence fits, and a system with no way to
notice that no Activity was ever completed. The reply reads perfectly. It is
simply not true, and once sent it becomes a turn in the history, then an
episode, then a memory — a fabricated past with a provenance chain.

So: before the reply is written, Python assembles what is *actually* known
(:class:`GroundingContext`, spec 15.1). After the draft exists, dangerous
factual claims are extracted from it (15.2) and each one is resolved against
that context (15.3). A claim with no evidence is unsupported, and an
unsupported claim is not sent.

GROUND-001 is the load-bearing rule: **the model's own prose is never
evidence**. Nothing YUI generated — this draft, an earlier draft, or a sentence
she already sent — can support a claim. A ``YUI_MESSAGE_SENT`` event proves she
said something; it does not make what she said so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping, Sequence

#: Rebuild spec 15.2. The kinds of statement that are dangerous to get wrong,
#: because each one asserts something outside the sentence itself.
ClaimKind = Literal[
    "yui_completed_action",
    "yui_experience_habit",
    "yui_perception",
    # Audit finding 4. Memory claims split in two, because they are grounded by
    # different things: 「去年の夏のこと覚えてる」 needs a recalled row, and
    # 「わたしは忘れることもある」 is a fact about how the Memory subsystem works.
    # Folding them into one kind is what forced a second reviewer to exist.
    "yui_specific_memory_recall",
    "yui_general_memory_capability",
    #: Pre-migration alias for specific recall. **Read-only.** It survives in
    #: this Literal so that Common Ground rows written before the split still
    #: parse, and nowhere else: the reviewer schema cannot produce it, and the
    #: resolver refuses it. See LEGACY_CLAIM_KINDS.
    "yui_memory_claim",
    "user_past_fact",
    "npc_fact",
    "tool_use",
    "external_knowledge_claim",
    "current_world_fact",
]

CLAIM_KINDS: tuple[ClaimKind, ...] = (
    "yui_completed_action",
    "yui_experience_habit",
    "yui_perception",
    "yui_specific_memory_recall",
    "yui_general_memory_capability",
    "yui_memory_claim",
    "user_past_fact",
    "npc_fact",
    "tool_use",
    "external_knowledge_claim",
    "current_world_fact",
)

#: Categories a *new* claim may be classified as.
#:
#: The distinction is the point. `yui_memory_claim` predates the split into
#: "she is recalling this particular thing" and "this is how her memory works",
#: and it accepted the union of both categories' evidence. A reviewer that
#: chose it therefore got a looser rule than either of the categories that
#: replaced it — 「去年の夏のこと覚えている」 could be settled by an objective
#: event, which proves the summer happened and nothing about whether she can
#: bring it to mind.
#:
#: Keeping the alias readable while making it unreachable is the whole design:
#: old rows still deserialize, new drafts cannot get near it.
LEGACY_CLAIM_KINDS: frozenset[str] = frozenset({"yui_memory_claim"})

RUNTIME_CLAIM_KINDS: tuple[ClaimKind, ...] = tuple(
    kind for kind in CLAIM_KINDS if kind not in LEGACY_CLAIM_KINDS
)

#: What can stand behind a claim. Every one of these is a row somebody else
#: wrote — an Activity the world service finished, an Event the store holds, a
#: memory the memory engine encoded, a tool call the Tool Manager reported.
EvidenceKind = Literal[
    "activity",
    "objective_event",
    "subjective_memory",
    "semantic_memory",
    "verified_user_fact",
    "tool_call",
    "npc_interaction",
    "goal",
    "world_state",
    "diary_entry",
    "memory_authority",
]

#: Rebuild spec 15.3. Which evidence answers which claim.
#:
#: This is structure rather than tuning, so it lives here and not in a policy
#: file: relaxing it is not a knob, it is a decision to let a different kind of
#: fabrication through. 「今日は本を読んだ」 needs a completed Activity or an
#: authoritative Event — a semantic memory that YUI likes reading does not make
#: today's reading real.
ACCEPTED_EVIDENCE: dict[ClaimKind, tuple[EvidenceKind, ...]] = {
    "yui_completed_action": ("activity", "objective_event"),
    # A repeated-experience claim ("reading calms me") is not today's
    # activity, but it still needs a life record or a memory/belief produced
    # from one.  The USER having just mentioned the same action is never
    # evidence that YUI has done it.
    "yui_experience_habit": (
        "activity",
        "objective_event",
        "subjective_memory",
        "semantic_memory",
    ),
    "yui_perception": ("activity", "objective_event", "world_state"),
    # Audit finding 2 (round 2). A concrete recollection is settled by one
    # thing only: a memory that was actually recalled on this turn.
    #
    # "the event happened" and "she is remembering it now" are different
    # claims, and the first does not imply the second. An objective event, a
    # diary entry or an archive row each prove something occurred; none of
    # them proves she can bring it to mind — which is exactly what
    # `memory.current_recall_required` says in AUTHORITATIVE_MEMORY_FACTS.
    #
    # `subjective_memory` alone, and the resolver additionally requires the row
    # to be one of *this turn's* recalls.
    "yui_specific_memory_recall": ("subjective_memory",),
    # A statement about how her memory behaves is settled by the Memory
    # subsystem's own immutable facts, and by nothing she happens to recall.
    "yui_general_memory_capability": ("memory_authority",),
    # The retired alias. Narrowed to match the category it was an alias *for*,
    # so that even if something reached it the rule would be no looser than
    # `yui_specific_memory_recall`. It accepted `objective_event` and
    # `diary_entry`, which is the path this round closed: nothing that merely
    # proves an event occurred may settle a claim about remembering it.
    "yui_memory_claim": ("subjective_memory",),
    "user_past_fact": ("objective_event", "verified_user_fact", "subjective_memory"),
    "npc_fact": ("npc_interaction", "objective_event"),
    "tool_use": ("tool_call",),
    "external_knowledge_claim": ("semantic_memory", "tool_call"),
    "current_world_fact": ("world_state", "activity", "objective_event"),
}


#: Whose doing the evidence records. A ``USER_MESSAGE_RECEIVED`` event is a
#: fact about the USER; it is not a fact about YUI, however much of its wording
#: a claim about YUI happens to share.
EvidenceSubject = Literal["yui", "user", "other", "world", "unknown"]

#: Claims about YUI's own life, which only YUI's own record can evidence. The
#: USER saying 「小説読むの好き」 does not mean she read one — and that is just as
#: true of a habit claim as of a completed action.
#:
#: ``yui_experience_habit`` and ``yui_memory_claim`` were missing here, and the
#: consequence was concrete: both accept ``objective_event``, and a
#: ``USER_MESSAGE_RECEIVED`` event is an objective event. So 「詠んだよ」 from the
#: USER could support 「わたしもよく詠むよ」 from her, because the ownership filter
#: only ran for two of the four kinds that need it.
SELF_CLAIM_KINDS: frozenset[str] = frozenset(
    {
        "yui_completed_action",
        "yui_perception",
        "yui_experience_habit",
        "yui_memory_claim",
    }
)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One thing that is actually known, and where it came from."""

    kind: EvidenceKind
    reference: str
    summary: str
    occurred_at: datetime | None = None
    subject: EvidenceSubject = "unknown"
    #: How the subject stands to the event (audit finding 1). ``actor`` is the
    #: default because almost every row records something its subject did; a
    #: world event that reached YUI has to be marked ``experiencer`` by the
    #: subsystem that actually knows it did.
    relation: str = "actor"

    @property
    def evidence_id(self) -> str:
        """The name a semantic reviewer may cite this by.

        Kind-prefixed so an identifier carries its own authority: a reviewer
        that cites ``activity:...`` for a memory claim has cited something of
        the wrong kind, and Python can see that without trusting the prose
        around it. Derived rather than stored, so it cannot drift from the
        evidence it names.
        """
        return f"{self.kind}:{self.reference}"

    @property
    def owner_label(self) -> str:
        """Who this is about, in words the realizer prompt can carry.

        The reason this exists: a summary alone loses the subject. 「詠んだよ」
        rendered as a bare fact reads as *hers*, and the model then writes a
        reply built on having done it.
        """
        return {
            "yui": "YUI",
            "user": "USER",
            "other": "他者",
            "world": "世界",
            "unknown": "出所不明",
        }[self.subject]

    def describe(self) -> str:
        """One line, attributed, for a prompt."""
        return f"[{self.evidence_id}] ({self.owner_label}) {self.summary}"

    @property
    def is_yuis_own(self) -> bool:
        """Whether this records something *she* did.

        No longer includes ``world``. Folding the two together is what let "it
        rained" support "I got rained on": a world event is not something she
        did, and whether it reached her is not recorded by the subject field.
        The permissive case now goes through the ownership matrix, which asks
        for an explicit ``experiencer`` relation.
        """
        return self.subject == "yui" and self.relation == "actor"

    def matches(self, text: str) -> bool:
        """Whether this evidence is about what the claim is about.

        Deliberately shallow: shared content words. Evidence resolution is a
        *gate*, and a gate that tries to be clever about paraphrase is a gate
        that can be talked past. When it cannot tell, it does not support.
        """
        return bool(_content_tokens(self.summary) & _content_tokens(text))


@dataclass(frozen=True, slots=True)
class Claim:
    """A dangerous factual assertion found in a draft (spec 15.2)."""

    kind: ClaimKind
    text: str
    trigger: str
    #: Only a hard claim can suppress a reply. A rule that is merely suggestive
    #: reports itself and does not hold the send (spec 16: hard guard only).
    severity: Literal["hard", "soft"] = "hard"

    @property
    def accepted_evidence(self) -> tuple[EvidenceKind, ...]:
        return ACCEPTED_EVIDENCE[self.kind]


@dataclass(frozen=True, slots=True)
class GroundedClaim:
    """A claim, and what was found for it.

    Round 3, finding 1. This used to derive `supported` from "is the evidence
    tuple non-empty", which is a *re-decision*: the semantic resolver had
    already weighed positive evidence against authoritative contradictions and
    concluded `contradicted`, and this adapter then looked at the surviving
    positive evidence alone and flipped the answer back to supported. A claim
    the record disagrees with reached the USER because the last object to touch
    it recomputed a verdict it was not the authority for.

    So the verdict travels. When a semantic resolver ran, its conclusion is
    carried here verbatim and nothing downstream recalculates it; when nothing
    authoritative ran — the legacy extractor path — `verdict` is empty and the
    old evidence-derived reading applies, because there was no better answer to
    preserve.
    """

    claim: Claim
    evidence: tuple[Evidence, ...] = ()
    #: ``supported`` / ``unsupported`` / ``contradicted`` from the resolver, or
    #: ``""`` when no semantic authority spoke for this claim.
    verdict: str = ""
    #: The authoritative records that disagree, carried so repair can say so.
    contradictions: tuple[Evidence, ...] = ()

    @property
    def contradicted(self) -> bool:
        return self.verdict == "contradicted" or bool(self.contradictions)

    @property
    def supported(self) -> bool:
        if self.verdict:
            return self.verdict == "supported"
        return bool(self.evidence)

    def describe(self) -> str:
        if self.contradicted:
            records = "、".join(item.summary for item in self.contradictions)
            return f"{self.claim.kind}: 「{self.claim.trigger}」は記録と矛盾する（{records}）"
        if self.supported:
            return f"{self.claim.kind}: 裏づけあり"
        return f"{self.claim.kind}: 「{self.claim.trigger}」の裏づけがない"


#: The §15.1 field names, in the order the spec lists them.
GROUNDING_SECTIONS: tuple[str, ...] = (
    "current_world",
    "current_activity",
    "completed_activities_today",
    "recent_objective_events",
    "recalled_subjective_memories",
    "verified_user_facts",
    "known_semantic_memories",
    "successful_tool_calls",
    "npc_interactions",
    "current_goals",
    "explicitly_read_diary_entries",
    "memory_authority_facts",
)


@dataclass(frozen=True, slots=True)
class GroundingContext:
    """Everything that may stand behind a claim (rebuild spec 15.1).

    Built before the reply is generated, from rows other subsystems own. It is
    a read model: nothing here is written by the conversation path, and nothing
    generated by the model ever enters it (GROUND-001).
    """

    current_world: tuple[Evidence, ...] = ()
    current_activity: tuple[Evidence, ...] = ()
    completed_activities_today: tuple[Evidence, ...] = ()
    recent_objective_events: tuple[Evidence, ...] = ()
    recalled_subjective_memories: tuple[Evidence, ...] = ()
    verified_user_facts: tuple[Evidence, ...] = ()
    known_semantic_memories: tuple[Evidence, ...] = ()
    successful_tool_calls: tuple[Evidence, ...] = ()
    npc_interactions: tuple[Evidence, ...] = ()
    current_goals: tuple[Evidence, ...] = ()
    explicitly_read_diary_entries: tuple[Evidence, ...] = ()
    #: Immutable facts about the actual Memory subsystem. These support only
    #: general capability claims; concrete recall still requires a recalled row.
    memory_authority_facts: tuple[Evidence, ...] = ()
    #: Audit finding 9. Per-section: did the source answer, and did it have
    #: anything to say? ``_safe`` used to flatten an exception into an empty
    #: tuple, which made "the activity repository raised" indistinguishable
    #: from "she completed nothing today" — and the second is a fact about her
    #: day while the first is a fact about the database. Sections absent from
    #: this mapping were never read at all.
    availability: Mapping[str, str] = field(default_factory=dict)
    #: When the context was assembled, for the trace.
    built_at: datetime | None = None

    def all_evidence(self) -> tuple[Evidence, ...]:
        collected: list[Evidence] = []
        for section in GROUNDING_SECTIONS:
            collected.extend(getattr(self, section))
        return tuple(collected)

    def of_kinds(self, kinds: tuple[EvidenceKind, ...]) -> tuple[Evidence, ...]:
        return tuple(item for item in self.all_evidence() if item.kind in kinds)

    def status(self, section: str) -> str:
        """``available``, ``empty``, ``unavailable`` or ``unknown``.

        ``unknown`` means nothing tried to read it — a phase that has not
        landed. Distinct from ``unavailable``, which means somebody tried and
        the read failed.
        """
        recorded = self.availability.get(section)
        if recorded:
            return recorded
        if getattr(self, section, ()) if hasattr(self, section) else ():
            return "available"
        return "unknown"

    @property
    def unavailable_sections(self) -> tuple[str, ...]:
        """Sources that were read and failed. Never silently empty."""
        return tuple(
            sorted(
                name
                for name, state in self.availability.items()
                if state == "unavailable"
            )
        )

    @property
    def degraded(self) -> bool:
        return bool(self.unavailable_sections)

    def by_id(self, evidence_id: str) -> Evidence | None:
        """Resolve a cited identifier, or refuse it.

        The Python half of the semantic-claim contract: the model proposes
        identifiers and this decides whether they exist. An identifier that
        was invented — however plausible, however confidently cited — resolves
        to ``None`` and supports nothing (GROUND-001).
        """
        for item in self.all_evidence():
            if item.evidence_id == evidence_id:
                return item
        return None

    def resolve_ids(self, evidence_ids: Sequence[str]) -> tuple[Evidence, ...]:
        """Every cited identifier that turned out to be real, in cited order."""
        resolved: list[Evidence] = []
        for evidence_id in evidence_ids:
            found = self.by_id(evidence_id)
            if found is not None and found not in resolved:
                resolved.append(found)
        return tuple(resolved)

    @property
    def is_empty(self) -> bool:
        return not self.all_evidence()


#: Characters that carry no topic. Japanese has no spaces, so tokenising by
#: whitespace finds nothing; what works here is dropping the grammatical
#: scaffolding and comparing what is left.
_PARTICLES = frozenset("はがをにでとへもやのねよなかだですますましたたるらしいうくっ、。！？!?　 ")


def _content_tokens(text: str) -> frozenset[str]:
    """Character bigrams of the content-bearing part of a string."""
    kept = [char for char in text if char not in _PARTICLES]
    if len(kept) < 2:
        return frozenset(kept)
    return frozenset(
        "".join(kept[index : index + 2]) for index in range(len(kept) - 1)
    )


__all__ = [
    "ACCEPTED_EVIDENCE",
    "GROUNDING_SECTIONS",
    "CLAIM_KINDS",
    "LEGACY_CLAIM_KINDS",
    "RUNTIME_CLAIM_KINDS",
    "Claim",
    "ClaimKind",
    "Evidence",
    "EvidenceKind",
    "GroundedClaim",
    "GroundingContext",
]
