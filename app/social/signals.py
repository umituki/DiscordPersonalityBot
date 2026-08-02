"""Social signals read from an event and its appraisal (spec 13, 14).

The engines below need to know what *kind* of social moment this was. Deriving
that here keeps the judgement in one place and keeps the engines honest about
what they are reacting to.

Spec 13.3 is the important one: disagreement, conflict, transgression and
betrayal are different things, not points on one scale, and none of them is an
automatic relationship penalty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.events.model import Event
from app.psychology.models import Appraisal

ConflictKind = Literal["none", "disagreement", "conflict", "transgression", "betrayal"]


@dataclass(frozen=True, slots=True)
class SocialSignals:
    """What one event says about the relationship it happened in."""

    is_contact: bool = False
    #: Spec 13.3 categories. ``none`` is the normal case.
    conflict_kind: ConflictKind = "none"
    apology: bool = False
    #: The other side did what they said they would.
    kept_commitment: bool = False
    #: Something personal was shared.
    disclosure: bool = False
    positive_affect: float = 0.0
    negative_affect: float = 0.0
    competence_shown: float = 0.0
    consistency: float = 0.0
    #: Days since the last contact, when this event follows a silence.
    separation_days: float = 0.0

    @property
    def has_conflict(self) -> bool:
        return self.conflict_kind != "none"

    @property
    def is_severe(self) -> bool:
        return self.conflict_kind in ("transgression", "betrayal")


#: Deliberately narrow: these are cheap surface cues, and every one of them is
#: only a *signal*. Nothing here decides a state change on its own.
_APOLOGY_MARKERS = ("ごめん", "すまな", "申し訳", "謝", "sorry", "apolog")
_DISCLOSURE_MARKERS = ("実は", "ほんとは", "本当は", "誰にも言", "はじめて話", "打ち明け")
_COMMITMENT_MARKERS = ("約束どおり", "約束通り", "言ったとおり", "ちゃんとやった", "できたよ")


def signals_from(
    event: Event,
    appraisal: Appraisal | None,
    *,
    separation_days: float = 0.0,
    declared_conflict: ConflictKind | None = None,
) -> SocialSignals:
    """Read an event and its appraisal into social signals."""
    text = getattr(event.payload, "text", "") or ""
    lowered = text.lower()
    is_contact = event.actor_type == "user"

    conflict_kind: ConflictKind = declared_conflict or "none"
    positive = 0.0
    negative = 0.0
    consistency = 0.0

    if appraisal is not None:
        positive = max(0.0, appraisal.social_meaning)
        negative = max(0.0, -appraisal.social_meaning)
        consistency = max(0.0, appraisal.certainty - 0.5) * 2.0
        if declared_conflict is None and negative > 0.5 and appraisal.goal_congruence < -0.3:
            # A negative social reading is a disagreement at most. Calling it a
            # transgression takes more than one bad-feeling message (spec 13.3).
            conflict_kind = "conflict" if negative > 0.8 else "disagreement"

    return SocialSignals(
        is_contact=is_contact,
        conflict_kind=conflict_kind,
        apology=any(marker in text or marker in lowered for marker in _APOLOGY_MARKERS),
        kept_commitment=any(marker in text for marker in _COMMITMENT_MARKERS),
        disclosure=any(marker in text for marker in _DISCLOSURE_MARKERS),
        positive_affect=positive,
        negative_affect=negative,
        competence_shown=0.0,
        consistency=consistency,
        separation_days=max(0.0, separation_days),
    )
