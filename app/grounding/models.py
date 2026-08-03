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

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

#: Rebuild spec 15.2. The kinds of statement that are dangerous to get wrong,
#: because each one asserts something outside the sentence itself.
ClaimKind = Literal[
    "yui_completed_action",
    "yui_experience_habit",
    "yui_perception",
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
    "yui_memory_claim",
    "user_past_fact",
    "npc_fact",
    "tool_use",
    "external_knowledge_claim",
    "current_world_fact",
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
    "yui_memory_claim": ("subjective_memory", "objective_event", "diary_entry"),
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

#: Claims about YUI's own doing and perceiving, which only YUI's own actions can
#: evidence. The USER saying 「小説読むの好き」 does not mean she read one.
SELF_CLAIM_KINDS: frozenset[str] = frozenset({"yui_completed_action", "yui_perception"})


@dataclass(frozen=True, slots=True)
class Evidence:
    """One thing that is actually known, and where it came from."""

    kind: EvidenceKind
    reference: str
    summary: str
    occurred_at: datetime | None = None
    subject: EvidenceSubject = "unknown"

    @property
    def is_yuis_own(self) -> bool:
        """Whether this records something *she* did or the world did to her.

        Unattributed evidence does not count. Requiring the attribution rather
        than assuming it is the same choice made everywhere else in grounding:
        a missing fact makes the gate stricter, never looser.
        """
        return self.subject in ("yui", "world")

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
    """A claim, and what was found for it."""

    claim: Claim
    evidence: tuple[Evidence, ...] = ()

    @property
    def supported(self) -> bool:
        return bool(self.evidence)

    def describe(self) -> str:
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
    #: When the context was assembled, for the trace.
    built_at: datetime | None = None

    def all_evidence(self) -> tuple[Evidence, ...]:
        collected: list[Evidence] = []
        for section in GROUNDING_SECTIONS:
            collected.extend(getattr(self, section))
        return tuple(collected)

    def of_kinds(self, kinds: tuple[EvidenceKind, ...]) -> tuple[Evidence, ...]:
        return tuple(item for item in self.all_evidence() if item.kind in kinds)

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
    "Claim",
    "ClaimKind",
    "Evidence",
    "EvidenceKind",
    "GroundedClaim",
    "GroundingContext",
]
