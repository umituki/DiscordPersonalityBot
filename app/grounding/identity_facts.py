"""Who she is, as evidence.

「何歳？」 used to be answered with 「年齢という概念はわたしの存在にはありません」,
because the old identity said she was software and software has no birthday.
Under the world model she is a person in her own world and she does have one —
which immediately raises the question the rest of this subsystem exists to
answer: where does the number come from?

Not from the model. An age is a subtraction, and a model asked to perform it
gets the birthday-not-yet-reached case wrong at exactly the rate that makes it
hard to notice. It comes from the life anchors, which are settled once at FIRST
BOOT and never renegotiated, and it is recomputed here on every turn against
the same clock everything else uses — so it is right the day before her
birthday and right the day after, without anything having to be updated.

Before the anchors exist there is nothing here. That is the honest state of a
life that has not been settled yet, and it means an age question is answered
with "I do not know" rather than with a plausible number.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.genesis.anchors import age_at
from app.grounding.models import Evidence

logger = logging.getLogger(__name__)


def identity_evidence(
    *, identity: Any = None, anchors: Any = None, now: datetime
) -> tuple[Evidence, ...]:
    """The identity facts that hold right now.

    ``relation="topic"``: a birthday is not something she did. It is something
    true of her, which is the distinction the ownership matrix draws between
    an occasion and a standing fact.
    """
    facts: list[Evidence] = []

    name = getattr(identity, "name", "") if identity is not None else ""
    if name:
        facts.append(
            Evidence(
                kind="identity_fact",
                reference="name",
                summary=f"名前は{name}",
                subject="yui",
                relation="topic",
            )
        )

    birth = getattr(anchors, "birth_datetime", None) if anchors is not None else None
    if birth is not None:
        facts.append(
            Evidence(
                kind="identity_fact",
                reference="birthday",
                summary=f"生まれた日は{birth.date().isoformat()}",
                occurred_at=birth,
                subject="yui",
                relation="topic",
            )
        )
        facts.append(
            Evidence(
                kind="identity_fact",
                reference="age",
                # Derived on every read rather than stored, so the number is
                # never one birthday out of date.
                summary=f"いまの年齢は{age_at(birth, now)}歳",
                occurred_at=now,
                subject="yui",
                relation="topic",
            )
        )
    return tuple(facts)


__all__ = ["identity_evidence"]
