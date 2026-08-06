"""Which world a fact belongs to, and what that makes possible.

YUI lives her own life. The USER lives theirs. The two lives are not in the
same place, and no route runs between them except the one they are already
talking through.

That replaces an older framing in which YUI was software: no body, no age, no
sleep, "I cannot go outside because I am a program". It kept her safe by
keeping her from being anybody. Asked 「何歳？」 she answered that age did not
apply to her; asked what she had been doing she explained her own architecture.
The safety that mattered — she does not invent experiences — was never coming
from the digital framing. It comes from evidence, and evidence is untouched.

So the constraint is relocated rather than removed:

    old   YUI cannot act physically, because she is not physical
    new   YUI acts physically in her own world like anyone else, and nothing
          physical reaches across to the USER's

This module owns only that second sentence. It is not lore. There is no story
here about why the worlds are separate, how the channel between them works, or
what either world contains — because a story would be exactly the thing the
grounding rules exist to prevent, and 「別の世界」 must not become a licence to
invent portals, magic, or a hometown. What lives here is a reachability rule
and nothing else.

Ownership is untouched and still answers a different question. `subject` says
whose life a fact is about; `world` says where it holds; `relation` says how
the subject stands to it. A world scope never substitutes for a subject check.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Sequence


#: Which generation of the world model a stored row was produced under.
#:
#: Not the schema version. A migration adds columns; it cannot add meaning. The
#: audit's finding was that migration 36 gave every pre-existing month
#: `participants=[]` and `interaction_scope=local` — structurally identical to
#: a month the current validator had actually passed, and produced by a critic
#: that a paraphrase walked straight past. The row looked verified because the
#: columns existed, not because anything had checked it.
#:
#: So provenance is recorded rather than inferred. A row written by code that
#: ran the current validation carries this number; everything older carries 0,
#: which means "no claim was ever made about this under the current rules".
#: There is no safe way to backfill it, because inferring participants from
#: Japanese prose is the exact structure the world model replaced.
#:
#: Bump when the world semantics change in a way that invalidates stored
#: judgements. The resolution is a fresh rebuild, never a quiet reinterpretation.
#:
#: 1 — the digital-existence identity, retroactively. No row was ever tagged 1.
#: 2 — YUI is a person in her own world; nothing physical reaches the USER's.
#: 3 — the same, plus: the USER is absent from Genesis *entirely*, including
#:     `shared_communication`, because those years predate the conversation;
#:     the authoritative life is the one FIRST BOOT points at rather than the
#:     most recent run; a resume revalidates stored data instead of trusting
#:     the checkpoint that recorded the work; and an annual synthesis is a
#:     checked derivation of its months rather than free prose.
#:
#: v2 rows are stale under v3. Several of those changes turn what used to pass
#: into a refusal, so a life judged under v2 was judged by rules that no longer
#: hold — and there is no way to re-judge it that does not mean reading its
#: prose. Explicit rebuild, as always.
CURRENT_WORLD_MODEL_VERSION = 3

#: What a row carries when nothing recorded its provenance.
UNVERIFIED_WORLD_MODEL_VERSION = 0


def is_current_world_model(version: int | None) -> bool:
    """Whether a stored row was judged under the rules in force now."""
    return int(version or 0) == CURRENT_WORLD_MODEL_VERSION


class WorldScope(StrEnum):
    """Where a fact holds."""

    #: Where YUI lives. Her room, her days, the people she knows.
    YUI_WORLD = "yui_world"
    #: Where the USER lives. Reachable only through what they say.
    USER_WORLD = "user_world"
    #: The conversation itself — the one thing both worlds share.
    SHARED_COMMUNICATION = "shared_communication"
    #: Knowledge about neither life in particular.
    EXTERNAL_KNOWLEDGE = "external_knowledge"
    #: Not established. Grounds nothing, which is the safe direction.
    UNKNOWN = "unknown"


class InteractionScope(StrEnum):
    """What kind of contact a claim describes.

    The distinction that matters is *not* physical versus non-physical.
    「本を読んだ」 and 「USERの肩に触れた」 are both physical, and only the
    second is impossible — so classifying by physicality, as the old guard did,
    catches the wrong thing in both directions at once.
    """

    #: Everything the subject does inside their own world.
    LOCAL_TO_SUBJECT_WORLD = "local_to_subject_world"
    #: Talking to each other. The channel they are already using.
    SHARED_COMMUNICATION = "shared_communication"
    #: Being in the same place, touching, handing something over. No route
    #: exists for this, so no evidence can establish it.
    CROSS_WORLD_PHYSICAL = "cross_world_physical"


#: Whose world each evidence/claim subject lives in.
#:
#: NPCs are YUI's neighbours, not the USER's: an NPC is a person in her world,
#: so 「ミカと話した」 is local to her and 「USERがミカと会った」 is not something
#: this system can hold. `world` covers facts about the world YUI lives in
#: rather than a separate place.
SUBJECT_WORLDS: dict[str, WorldScope] = {
    "yui": WorldScope.YUI_WORLD,
    "npc": WorldScope.YUI_WORLD,
    "other": WorldScope.YUI_WORLD,
    "world": WorldScope.YUI_WORLD,
    "user": WorldScope.USER_WORLD,
    "unknown": WorldScope.UNKNOWN,
}


def world_of(subject: str) -> WorldScope:
    """The world a subject lives in. Unknown subjects stay unknown."""
    return SUBJECT_WORLDS.get(subject, WorldScope.UNKNOWN)


def is_reachable(scope: InteractionScope | str) -> bool:
    """Whether this kind of contact can happen at all.

    Not "whether it happened" — that is what evidence decides, and a local
    action still needs a record. This is the prior question: is there any state
    of the world in which the claim could be true.
    """
    return InteractionScope(scope) is not InteractionScope.CROSS_WORLD_PHYSICAL


def refusal_reason(scope: InteractionScope | str) -> str:
    """Why the contact is impossible, named for the trace and the repair."""
    return "" if is_reachable(scope) else "cross_world_physical"


#: The subject vocabulary a claim or a generated month may name.
#: Deliberately the same closed set the ownership matrix uses, so there is one
#: word for "the USER" everywhere rather than one per subsystem.
PARTICIPANT_SUBJECTS: frozenset[str] = frozenset(SUBJECT_WORLDS)


def worlds_are_separate(first: str, second: str) -> bool:
    """Whether two subjects live in different worlds.

    `unknown` is not "separate" — it is "not established", and refusing on it
    would block ordinary claims whose subject the reviewer left open. The
    subject-required rule already handles that case, in the place that owns it.
    """
    left, right = world_of(first), world_of(second)
    if WorldScope.UNKNOWN in (left, right):
        return False
    return left is not right


__all__ = [
    "CURRENT_WORLD_MODEL_VERSION",
    "SUBJECT_WORLDS",
    "UNVERIFIED_WORLD_MODEL_VERSION",
    "is_current_world_model",
    "InteractionScope",
    "WorldScope",
    "is_reachable",
    "refusal_reason",
    "world_of",
    "worlds_are_separate",
]


# --- the cross-check ---------------------------------------------------------
#
# The audit's finding: a single classified field is a single point of failure.
# `interaction_scope` said `local_to_subject_world` for 「YUIがUSERと直接会った」
# and, with a YUI-owned activity cited, the claim resolved as supported. The
# scope was the only thing standing there, and it was wrong.
#
# So the reviewer now states *who was involved* as well, and Python checks the
# two answers against each other. Neither is trusted alone: a scope that says
# "in her own world" while naming the USER as a participant is a contradiction
# the model produced without noticing, and Python can see it without reading a
# word of Japanese.
#
# This is not a claim to have solved semantic classification. A model that gets
# both fields wrong in the same direction still gets through here — see the
# layers after this one.


def validate_interaction(
    *, subject: str, participants: Sequence[str], scope: InteractionScope | str
) -> str:
    """Whether the stated subject, participants and scope can all be true.

    Returns a refusal reason, or ``""`` when they agree. Structural possibility
    only: "could this have happened", never "did it". Evidence answers the
    second question and this function does not touch it.
    """
    interaction = InteractionScope(scope)
    others = tuple(participants)

    if "unknown" in others:
        # An interaction whose counterparty was not identified. Which world it
        # crosses is exactly what is unknown, so there is nothing to check it
        # against — and a committing claim about meeting somebody unspecified
        # is not something to wave through.
        return "unknown_participant"

    unnamed = [name for name in others if name not in PARTICIPANT_SUBJECTS]
    if unnamed:
        return f"unknown_participant:{unnamed[0][:24]}"

    crossing = [name for name in others if worlds_are_separate(subject, name)]

    if interaction is InteractionScope.CROSS_WORLD_PHYSICAL:
        if not crossing:
            # 「ミカと同じ部屋にいた」 classified as a crossing. Everyone named
            # lives in the same world, so this is a misreading rather than an
            # impossible event — and letting it through as "correctly refused"
            # would hide a classifier that cannot tell her neighbours from the
            # USER.
            return "cross_world_scope_without_crossing"
        return "cross_world_physical"

    if interaction is InteractionScope.LOCAL_TO_SUBJECT_WORLD and crossing:
        # The audit's reproduction. Being local and involving somebody from
        # another world are not both possible.
        return f"interaction_scope_world_mismatch:{crossing[0]}"

    # `shared_communication` is the one scope that is *supposed* to span the
    # two worlds. It says they talked, which is the thing they can do — and it
    # supports nothing physical, because the evidence kinds decide that.
    return ""


__all__ += ["PARTICIPANT_SUBJECTS", "validate_interaction"]
