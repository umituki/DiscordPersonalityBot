"""The one place Genesis decides whether stored world data may be trusted.

Two ideas, and the second is the one this round is about.

**Genesis is before FIRST BOOT.** The USER is not in it — not met, not touched,
not spoken to, not heard from. That is stricter than the ordinary world rule,
which permits `shared_communication` between the two of them because in the
present they are, in fact, talking. `validate_interaction` is right about the
present and would be wrong here, so the Genesis-specific ban sits on top of it
rather than being folded into it.

**A checkpoint records that work finished, not that its output is still
trustworthy.** A resume that reads `year_critics_done` and skips the months has
confused the two: the checkpoint was written by an earlier version of these
rules, or by this one before somebody edited the database, and either way it is
a fact about the past rather than a proof about the present. So every read of
persisted Genesis data goes through here, every time, including on the paths
that "already ran".

Everything returns a named reason rather than raising, because the callers are
generation stages that have to stop cleanly and say why — an exception would be
observable only as a crash, and 「なぜ止まったか」 is what an operator needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from app.world.scope import (
    InteractionScope,
    is_current_world_model,
    validate_interaction,
)

#: Nobody from outside her world appears in the nineteen years.
#:
#: Deliberately not `validate_interaction`'s business. That function answers
#: "could this happen between these two people", and the honest answer for
#: YUI and the USER talking is yes — they are talking right now. What makes it
#: impossible *here* is when, not who: there was no conversation yet.
FORBIDDEN_IN_GENESIS: frozenset[str] = frozenset({"user"})


@dataclass(frozen=True, slots=True)
class ParsedWorldMetadata:
    """Metadata read strictly, or a reason it could not be."""

    participants: tuple[str, ...] = ()
    interaction_scope: str = ""
    refusal: str = ""

    @property
    def ok(self) -> bool:
        return not self.refusal


def parse_world_metadata(
    *, version: Any, participants_json: Any, interaction_scope: Any, prefix: str
) -> ParsedWorldMetadata:
    """Read provenance and metadata, refusing anything ambiguous.

    The old reader answered a malformed participant list with ``()`` — "nobody
    was involved" — which is the permissive value, and the one a hand-edited
    database gets for free. A blob that will not parse is not an empty blob.
    """
    if not is_current_world_model(_as_int(version)):
        return ParsedWorldMetadata(refusal=f"{prefix}_world_model_unverified")
    try:
        parsed = json.loads(participants_json or "[]")
    except Exception:  # noqa: BLE001
        return ParsedWorldMetadata(refusal=f"{prefix}_world_metadata_unreadable")
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        return ParsedWorldMetadata(refusal=f"{prefix}_world_metadata_unreadable")

    scope = str(interaction_scope or "")
    if not scope:
        return ParsedWorldMetadata(refusal=f"{prefix}_world_metadata_missing")
    try:
        InteractionScope(scope)
    except ValueError:
        # A structured reason rather than an exception out of the stage. Both
        # fail closed; only one tells the OWNER which row to look at.
        return ParsedWorldMetadata(refusal=f"{prefix}_world_metadata_invalid")

    return ParsedWorldMetadata(
        participants=tuple(parsed), interaction_scope=scope
    )


def validate_genesis_world(
    *,
    subject: str = "yui",
    participants: Sequence[str],
    interaction_scope: str,
) -> str:
    """The Genesis reading of a world claim: the ordinary rule, plus the USER.

    The USER check is first and is independent of scope. 「USERと話した」
    classified as `shared_communication` is structurally coherent — and is a
    conversation that had not happened yet.
    """
    forbidden = FORBIDDEN_IN_GENESIS.intersection(participants)
    if forbidden:
        return f"first_boot_boundary:{sorted(forbidden)[0]}"
    return validate_interaction(
        subject=subject, participants=participants, scope=interaction_scope
    )


def validate_genesis_month(row: Any) -> str:
    """Whether a stored month may be reused, reviewed, summarised or extracted.

    One function for all of those, because the Genesis USER ban copied into
    seven call sites is seven places for it to drift.
    """
    parsed = parse_world_metadata(
        version=_column(row, "world_model_version"),
        participants_json=_column(row, "participants_json"),
        interaction_scope=_column(row, "interaction_scope"),
        prefix="month",
    )
    if not parsed.ok:
        return parsed.refusal
    return validate_genesis_world(
        participants=parsed.participants,
        interaction_scope=parsed.interaction_scope,
    )


def month_metadata(row: Any) -> ParsedWorldMetadata:
    """The strict read, for a caller that needs the values after checking."""
    return parse_world_metadata(
        version=_column(row, "world_model_version"),
        participants_json=_column(row, "participants_json"),
        interaction_scope=_column(row, "interaction_scope"),
        prefix="month",
    )


def stale_months(rows: Sequence[Any]) -> list[tuple[str, str]]:
    """``(month_id, reason)`` for every month that may not be used."""
    return [
        (row["month_id"], refusal)
        for row in rows
        if (refusal := validate_genesis_month(row))
    ]


def validate_genesis_summary(
    *,
    participants: Sequence[str],
    interaction_scope: str,
    month_participants: Sequence[str] | None = None,
) -> str:
    """Whether an annual summary could describe the months it came from.

    A synthesis is a compression, not a generation stage — the same contract an
    extracted experience is held to. It had no world metadata at all until now,
    so a model could put the USER into a year whose months never had one, and
    the summary is what the next year reads as context.
    """
    refusal = validate_genesis_world(
        participants=participants, interaction_scope=interaction_scope
    )
    if refusal:
        return refusal
    if month_participants is not None:
        introduced = [
            role for role in participants if role not in tuple(month_participants)
        ]
        if introduced:
            return f"summary_participant_not_in_months:{introduced[0]}"
    return ""


def stored_summary_refusal(
    row: Any, *, month_participants: Sequence[str] | None = None
) -> str:
    """Whether a persisted `final_summary` may be reused or read as context."""
    parsed = parse_world_metadata(
        version=_column(row, "final_summary_world_model_version"),
        participants_json=_column(row, "final_summary_participants_json"),
        interaction_scope=_column(row, "final_summary_interaction_scope"),
        prefix="summary",
    )
    if not parsed.ok:
        return parsed.refusal
    return validate_genesis_summary(
        participants=parsed.participants,
        interaction_scope=parsed.interaction_scope,
        month_participants=month_participants,
    )


def _column(row: Any, name: str) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "FORBIDDEN_IN_GENESIS",
    "ParsedWorldMetadata",
    "month_metadata",
    "parse_world_metadata",
    "stale_months",
    "stored_summary_refusal",
    "validate_genesis_month",
    "validate_genesis_summary",
    "validate_genesis_world",
]
