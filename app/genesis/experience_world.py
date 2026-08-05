"""Whether a generated experience could have happened in the month it came from.

A month is validated before it is written. An experience is produced *from* a
validated month by a second model call — and that call could put somebody in
the story who was never in the month. The audit found exactly that: an
`ExperienceCandidate` with `actors=["USER"]` was accepted by the schema and
staged, from a month that contained no USER at all.

Extraction compresses what happened. It does not add to it. So this checks
three things, none of which reads a word of Japanese:

    the metadata agrees with itself   (the same world rule a claim goes through)
    it agrees with the month          (participants are a subset, not a superset)
    the USER is not in it             (Genesis predates FIRST BOOT entirely)

The third is not a special case of the second. A month whose participants
somehow included the USER would already have been refused, but the ban is
stated here anyway: her nineteen years happened before there was a conversation
to have, so `shared_communication` with them is not a lenient case — it is
future leakage wearing a scope that is valid everywhere else.
"""

from __future__ import annotations

from typing import Sequence

from app.world.scope import validate_interaction

#: Genesis covers the life before FIRST BOOT. The USER does not appear in it in
#: any capacity — not met, not touched, not spoken to.
FORBIDDEN_IN_GENESIS: frozenset[str] = frozenset({"user"})


def validate_experience(
    *,
    subject: str = "yui",
    participants: Sequence[str],
    interaction_scope: str,
    actor_subjects: Sequence[str] = (),
    month_participants: Sequence[str] | None = None,
) -> str:
    """A refusal reason, or ``""`` when the experience could have happened.

    ``month_participants`` is ``None`` for a caller with no month to compare
    against — the derivation check is skipped, and the other two still run.
    """
    stated = tuple(participants)
    actors = tuple(actor_subjects)

    forbidden = FORBIDDEN_IN_GENESIS.intersection(stated) | (
        FORBIDDEN_IN_GENESIS.intersection(actors)
    )
    if forbidden:
        return f"first_boot_boundary:{sorted(forbidden)[0]}"

    # An actor the participant list does not mention. The two answers come from
    # the same model call and disagreeing means one of them is wrong; which one
    # is not knowable here, so neither is believed.
    unlisted = [
        role
        for role in actors
        if role not in stated and role not in ("yui", "unknown")
    ]
    if unlisted:
        return f"actor_not_in_participants:{unlisted[0]}"

    refusal = validate_interaction(
        subject=subject, participants=stated, scope=interaction_scope
    )
    if refusal:
        return refusal

    if month_participants is not None:
        introduced = [
            role for role in stated if role not in tuple(month_participants)
        ]
        if introduced:
            # The extraction invented somebody. Whether they are dangerous is
            # beside the point — a month is the whole of what happened in it.
            return f"experience_participant_not_in_month:{introduced[0]}"

    return ""


__all__ = ["FORBIDDEN_IN_GENESIS", "validate_experience"]
