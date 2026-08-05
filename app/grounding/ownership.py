"""Who may evidence what (audit finding 1).

The previous rule was one-sided: claims about YUI were filtered to YUI-owned
evidence, and every other direction was unguarded. So the gate caught

    USER said 「詠んだ」  →  YUI claims 「詠んだ」        (blocked)

and missed

    YUI's activity row   →  claim about the USER's past  (allowed)
    a world event        →  YUI claims she did it        (allowed)
    an NPC interaction   →  claim about the USER         (allowed)

all of which are the same error pointed a different way. This module replaces
the one-sided filter with a matrix: for every claim category, exactly which
evidence *subjects* may stand behind it, and in which *relation* to the event.

**Subject is not enough on its own.** ``world`` was previously folded in with
``yui`` by :meth:`Evidence.is_yuis_own`, which made "it rained" support "I got
rained on". A world event is not something she did, and the fact that it
happened near her is not recorded by the subject field. So evidence also
carries a relation:

    actor        the subject brought this about — she read the book
    experiencer  the subject was affected by it — an explicit link, only ever
                 written by a subsystem that actually knows it
    topic        the record is *about* the subject without either of the above

A world event can support a claim that she perceived something only when some
subsystem has said, explicitly, that she was its experiencer. Nothing sets that
by default, which is the point: the permissive reading has to be earned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.grounding.models import ClaimKind, EvidenceSubject

#: How the subject stands to the recorded event.
EvidenceRelation = Literal["actor", "experiencer", "topic"]

#: The default. Almost every row a subsystem writes records something its
#: subject *did*: an Activity YUI completed, a message the USER sent.
DEFAULT_RELATION: EvidenceRelation = "actor"


#: Audit finding 1 (round 2). **One** table, indexed by claim category.
#:
#: There used to be three: ``SUPPORTING_SUBJECTS``, ``SUPPORTING_RELATIONS``
#: and ``CATEGORY_SUBJECT``. The first two were consulted and the third was
#: not, so a claim could declare ``subject="yui"`` under
#: ``category="user_past_fact"`` and be resolved against USER-owned evidence:
#: the evidence matched the *category*, nobody checked it matched the
#: *declared subject*, and the contradiction between the two went unnoticed.
#:
#: Each entry answers all three questions at once:
#:
#:     claims_about   the subject a claim of this category is necessarily
#:                    about. A claim declaring anything else has contradicted
#:                    itself and is refused before evidence is looked at.
#:     subjects       whose evidence may support it.
#:     relations      per evidence subject, how that subject must stand to the
#:                    recorded event.
@dataclass(frozen=True, slots=True)
class OwnershipRule:
    """Everything the resolver needs to know about one claim category."""

    claims_about: EvidenceSubject
    subjects: frozenset[str]
    relations: dict[str, frozenset[str]]

    def relations_for(self, subject: str) -> frozenset[str]:
        return self.relations.get(subject, ACTOR_OR_TOPIC)


ACTOR_ONLY: frozenset[str] = frozenset({"actor"})
ACTOR_OR_TOPIC: frozenset[str] = frozenset({"actor", "topic"})
EXPERIENCER_ONLY: frozenset[str] = frozenset({"experiencer"})

OWNERSHIP: dict[ClaimKind, OwnershipRule] = {
    # Her own life. Only her own record, whatever the world was doing.
    "yui_completed_action": OwnershipRule(
        claims_about="yui", subjects=frozenset({"yui"}), relations={"yui": ACTOR_ONLY}
    ),
    # A habit is not one act, so the record that establishes it need not be one
    # she performed. `topic` is admitted here and nowhere else on her side:
    # 「読書すると落ち着く傾向がある」 is a semantic memory *about* her,
    # generalised from her episodes, and requiring `actor` of it would demand
    # that a generalisation be an occasion. `yui_completed_action` stays
    # actor-only, which is where the strictness earns its keep — one specific
    # act does need a record of her performing it.
    "yui_experience_habit": OwnershipRule(
        claims_about="yui",
        subjects=frozenset({"yui"}),
        relations={"yui": ACTOR_OR_TOPIC},
    ),
    # Audit finding 2 (round 2): a recollection is settled by *recalling*, and
    # the accepted evidence kinds enforce the other half of that rule.
    "yui_specific_memory_recall": OwnershipRule(
        claims_about="yui",
        subjects=frozenset({"yui"}),
        relations={"yui": ACTOR_OR_TOPIC},
    ),
    # Who she is. `topic` because a birthday is not something she did — it is
    # something true of her, recorded once and derived from thereafter.
    "yui_identity_fact": OwnershipRule(
        claims_about="yui",
        subjects=frozenset({"yui"}),
        relations={"yui": ACTOR_OR_TOPIC},
    ),
    # Facts about how the Memory subsystem behaves. 「わたしにも忘れることは
    # ある」 is a statement about *her*, which is why ``claims_about`` is
    # ``yui`` — but nothing she recalls can settle it, which is why the only
    # evidence subject is the world. The two fields answer different questions
    # and this is the category where the difference is visible.
    "yui_general_memory_capability": OwnershipRule(
        claims_about="yui",
        subjects=frozenset({"world"}),
        relations={"world": ACTOR_OR_TOPIC},
    ),
    #: Legacy alias for specific recall.
    "yui_memory_claim": OwnershipRule(
        claims_about="yui",
        subjects=frozenset({"yui"}),
        relations={"yui": ACTOR_OR_TOPIC},
    ),
    # Perception is the one place a world record reaches, and only through the
    # experiencer relation: 「雨の音がしていた」 needs a record that the rain
    # actually reached her, not merely that it rained.
    "yui_perception": OwnershipRule(
        claims_about="yui",
        subjects=frozenset({"yui", "world"}),
        relations={"yui": ACTOR_ONLY, "world": EXPERIENCER_ONLY},
    ),
    # The USER's life. Her having done the same thing is not evidence they did.
    "user_past_fact": OwnershipRule(
        claims_about="user",
        subjects=frozenset({"user"}),
        relations={"user": ACTOR_OR_TOPIC},
    ),
    "npc_fact": OwnershipRule(
        claims_about="other",
        subjects=frozenset({"other"}),
        relations={"other": ACTOR_OR_TOPIC},
    ),
    # A tool call is hers: the Tool Manager records who asked.
    "tool_use": OwnershipRule(
        claims_about="yui", subjects=frozenset({"yui"}), relations={"yui": ACTOR_ONLY}
    ),
    # Knowledge she holds about the world. The proposition is about the world
    # — 「富士山は3776m」 — even though she is the one holding it, so a claim
    # declaring ``yui`` here has classified the wrong thing.
    "external_knowledge_claim": OwnershipRule(
        claims_about="world",
        subjects=frozenset({"yui", "world"}),
        relations={"yui": ACTOR_OR_TOPIC, "world": ACTOR_OR_TOPIC},
    ),
    "current_world_fact": OwnershipRule(
        claims_about="world",
        subjects=frozenset({"world"}),
        relations={"world": ACTOR_OR_TOPIC},
    ),
}

#: The reviewer's subject vocabulary and the evidence subject vocabulary are
#: not the same words for the same thing: a claim declares ``npc``, a row is
#: owned by ``other``. Translating in one place is the point — the pair was
#: compared directly at first, which silently refused every well-formed
#: ``npc_fact`` because "npc" is not "other".
SUBJECT_ALIASES: dict[str, EvidenceSubject] = {"npc": "other"}


def normalize_subject(subject: str) -> str:
    """The declared subject, in the evidence vocabulary."""
    return SUBJECT_ALIASES.get(subject, subject)  # type: ignore[return-value]


def declared_subject_is_consistent(category: str, subject: str) -> bool:
    """Whether the claim's declared subject agrees with its category.

    Fail closed. A reviewer that says a ``user_past_fact`` is about YUI has
    contradicted itself, and a self-contradictory classification is not a
    licence to pick whichever half is convenient — it is a reason to resolve
    nothing at all.

    ``unknown`` and the empty string are not contradictions: a reviewer that
    did not commit to a subject has asserted nothing about it, and the category
    still decides who the claim is about.
    """
    rule = OWNERSHIP.get(category)  # type: ignore[arg-type]
    if rule is None:
        return False
    if not subject or subject == "unknown":
        return True
    return normalize_subject(subject) == rule.claims_about


def claims_about(category: str) -> str:
    rule = OWNERSHIP.get(category)  # type: ignore[arg-type]
    return "" if rule is None else rule.claims_about


def supporting_subjects(category: str) -> frozenset[str]:
    """Which evidence subjects may support this category.

    An unknown category supports nothing. A category nobody has thought about
    is not a category with permissive defaults.
    """
    rule = OWNERSHIP.get(category)  # type: ignore[arg-type]
    return frozenset() if rule is None else rule.subjects


def supporting_relations(category: str, subject: str) -> frozenset[str]:
    """Which relations this subject may stand in, for this category."""
    rule = OWNERSHIP.get(category)  # type: ignore[arg-type]
    return frozenset() if rule is None else rule.relations_for(subject)


def may_support(
    category: str, *, subject: str, relation: str = DEFAULT_RELATION
) -> bool:
    """Whether evidence with this subject and relation can stand behind this claim.

    Both halves are required. ``world``/``actor`` is a fact about the world;
    ``world``/``experiencer`` is a fact about something that happened *to* her,
    and only the second can support a perception.
    """
    if subject not in supporting_subjects(category):
        return False
    return relation in supporting_relations(category, subject)


def refusal_reason(
    category: str, *, subject: str, relation: str = DEFAULT_RELATION
) -> str:
    """Why this evidence cannot support this claim, named for the repair prompt.

    "No evidence" and "that record is about somebody else" call for different
    rewrites, so the distinction survives all the way to the model.
    """
    if subject not in supporting_subjects(category):
        return "wrong_subject"
    if relation not in supporting_relations(category, subject):
        return "wrong_relation"
    return ""


#: Backwards-compatible views onto the one table. Derived, never edited: two
#: hand-maintained copies of the same rule is exactly what this round fixed.
SUPPORTING_SUBJECTS: dict[str, frozenset[str]] = {
    category: rule.subjects for category, rule in OWNERSHIP.items()
}
CATEGORY_SUBJECT: dict[str, str] = {
    category: rule.claims_about for category, rule in OWNERSHIP.items()
}


__all__ = [
    "ACTOR_ONLY",
    "ACTOR_OR_TOPIC",
    "CATEGORY_SUBJECT",
    "DEFAULT_RELATION",
    "EXPERIENCER_ONLY",
    "EvidenceRelation",
    "OWNERSHIP",
    "OwnershipRule",
    "SUBJECT_ALIASES",
    "SUPPORTING_SUBJECTS",
    "claims_about",
    "declared_subject_is_consistent",
    "may_support",
    "normalize_subject",
    "refusal_reason",
    "supporting_relations",
    "supporting_subjects",
]
