"""Relationship lifecycle (spec 20.4).

    unmet / acquaintance / familiar / close / strained / distant / dormant
    / ended / reconnected

    接触がなくなることと嫌悪を分ける。

That last line is the whole reason this is a separate module. Silence and
conflict are different inputs and they must not collapse into one "how are we
doing" number:

* Not having spoken for months makes a relationship ``distant`` and then
  ``dormant``. It never makes it ``strained`` and it never ends it.
* Unresolved conflict makes it ``strained`` even when contact is daily.
* ``ended`` is a decision someone made. Nothing here derives it from time.

Pure functions: state in, stage out.
"""

from __future__ import annotations

from app.society.models import RelationshipStage
from app.society.policy import LifecycleRules


def stage_for(
    *,
    previous_stage: str,
    familiarity: float,
    closeness: float,
    conflict: float,
    days_since_contact: float | None,
    interaction_count: int,
    policy: LifecycleRules,
) -> RelationshipStage:
    """Where a relationship stands, given what has and has not happened."""
    if previous_stage == "ended":
        # Only an explicit reconnection reopens it; time does not.
        return "ended"
    if interaction_count <= 0:
        return "unmet"

    # Conflict is about what happened between them, so it outranks how long
    # ago it was. A strained relationship does not quietly become dormant.
    if conflict >= policy.strained_conflict:
        return "strained"

    if days_since_contact is not None:
        if days_since_contact >= policy.dormant_days:
            return "dormant"
        if days_since_contact >= policy.distant_days:
            return "distant"
        if (
            previous_stage in ("distant", "dormant")
            and days_since_contact <= policy.reconnected_within_days
        ):
            return "reconnected"

    if closeness >= policy.close_closeness:
        return "close"
    if familiarity >= policy.familiar_familiarity:
        return "familiar"
    return "acquaintance"


def is_absence(stage: str) -> bool:
    """True when the stage says "we have not spoken", not "we fell out"."""
    return stage in ("distant", "dormant")


def is_trouble(stage: str) -> bool:
    return stage in ("strained", "ended")
