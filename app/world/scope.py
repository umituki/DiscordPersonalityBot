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
    "SUBJECT_WORLDS",
    "InteractionScope",
    "WorldScope",
    "is_reachable",
    "refusal_reason",
    "world_of",
    "worlds_are_separate",
]
