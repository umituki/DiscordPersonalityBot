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

from typing import Literal

from app.grounding.models import ClaimKind, EvidenceSubject

#: How the subject stands to the recorded event.
EvidenceRelation = Literal["actor", "experiencer", "topic"]

#: The default. Almost every row a subsystem writes records something its
#: subject *did*: an Activity YUI completed, a message the USER sent.
DEFAULT_RELATION: EvidenceRelation = "actor"


#: Audit finding 1. For each claim category, the subjects whose evidence may
#: support it — and nothing else.
#:
#: Read this as "a claim of kind K is about X's life, so only X's records can
#: settle it". The four rejections the audit named fall straight out:
#:
#:     yui   -> user_past_fact          not in {user}
#:     user  -> yui_*                   not in {yui}
#:     world -> yui_completed_action    not in {yui}
#:     other -> user_past_fact / yui_*  not in either
SUPPORTING_SUBJECTS: dict[ClaimKind, frozenset[EvidenceSubject]] = {
    # Her own life. Only her own record, whatever the world was doing.
    "yui_completed_action": frozenset({"yui"}),
    "yui_experience_habit": frozenset({"yui"}),
    "yui_specific_memory_recall": frozenset({"yui"}),
    # The one memory category the world may answer: these are facts about the
    # Memory subsystem itself, not about anything she did.
    "yui_general_memory_capability": frozenset({"world"}),
    "yui_memory_claim": frozenset({"yui"}),
    # Perception is the one place a world record can reach, and only through
    # the experiencer relation below. The subject alone does not open it.
    "yui_perception": frozenset({"yui", "world"}),
    # The USER's life. Her having done the same thing is not evidence that
    # they did — that is the mirror image of the bug this file exists for.
    "user_past_fact": frozenset({"user"}),
    # Somebody else's life.
    "npc_fact": frozenset({"other"}),
    # A tool call is hers: the Tool Manager records who asked.
    "tool_use": frozenset({"yui"}),
    # Knowledge she holds about the world. Held by her, about the world, so
    # both are legitimate sources.
    "external_knowledge_claim": frozenset({"yui", "world"}),
    # The state of the world is the world's to report.
    "current_world_fact": frozenset({"world"}),
}

#: Which relations may support each category, *per subject*.
#:
#: Per-subject rather than per-category because the two halves are not
#: independent: under ``yui_perception`` her own record may be ``actor`` — she
#: went and looked — while a world record must be ``experiencer``, meaning some
#: subsystem recorded that the event actually reached her. A single
#: per-category set cannot say that, and saying it wrongly is how "it rained"
#: supports "I heard the rain".
#:
#: A subject absent from a category's entry falls back to ``ACTOR_OR_TOPIC``;
#: a category absent altogether supports nothing, via `supporting_subjects`.
ACTOR_ONLY: frozenset[str] = frozenset({"actor"})
ACTOR_OR_TOPIC: frozenset[str] = frozenset({"actor", "topic"})

SUPPORTING_RELATIONS: dict[ClaimKind, dict[str, frozenset[str]]] = {
    "yui_completed_action": {"yui": ACTOR_ONLY},
    "yui_experience_habit": {"yui": ACTOR_ONLY},
    "yui_specific_memory_recall": {"yui": ACTOR_OR_TOPIC},
    "yui_general_memory_capability": {"world": ACTOR_OR_TOPIC},
    "yui_perception": {
        "yui": ACTOR_ONLY,
        # The earned case: only an explicit "this reached her" link.
        "world": frozenset({"experiencer"}),
    },
    "user_past_fact": {"user": ACTOR_OR_TOPIC},
    "npc_fact": {"other": ACTOR_OR_TOPIC},
    "tool_use": {"yui": ACTOR_ONLY},
    "external_knowledge_claim": {"yui": ACTOR_OR_TOPIC, "world": ACTOR_OR_TOPIC},
    "current_world_fact": {"world": ACTOR_OR_TOPIC},
}

#: The subject a claim is *about*, when the claim's own ``subject`` field is
#: unknown. Used only as a cross-check: a reviewer that says a
#: ``yui_completed_action`` is about the USER has contradicted itself, and the
#: category is the half we trust, because it is the half the matrix indexes.
CATEGORY_SUBJECT: dict[ClaimKind, EvidenceSubject] = {
    "yui_completed_action": "yui",
    "yui_experience_habit": "yui",
    "yui_specific_memory_recall": "yui",
    "yui_general_memory_capability": "world",
    "yui_memory_claim": "yui",
    "yui_perception": "yui",
    "user_past_fact": "user",
    "npc_fact": "other",
    "tool_use": "yui",
    "external_knowledge_claim": "yui",
    "current_world_fact": "world",
}


def supporting_subjects(category: str) -> frozenset[str]:
    """Which evidence subjects may support this category.

    An unknown category supports nothing. A category nobody has thought about
    is not a category with permissive defaults.
    """
    return SUPPORTING_SUBJECTS.get(category, frozenset())  # type: ignore[arg-type]


def supporting_relations(category: str, subject: str) -> frozenset[str]:
    """Which relations this subject may stand in, for this category."""
    per_subject = SUPPORTING_RELATIONS.get(category, {})  # type: ignore[arg-type]
    return per_subject.get(subject, ACTOR_OR_TOPIC)


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


__all__ = [
    "ACTOR_ONLY",
    "ACTOR_OR_TOPIC",
    "CATEGORY_SUBJECT",
    "DEFAULT_RELATION",
    "EvidenceRelation",
    "SUPPORTING_RELATIONS",
    "SUPPORTING_SUBJECTS",
    "may_support",
    "refusal_reason",
    "supporting_relations",
    "supporting_subjects",
]
