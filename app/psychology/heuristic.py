"""Python appraisal for events that do not justify a model call (patch spec 12.3).

Simulating decades means most of what happens is ordinary. Asking a model to
read every ordinary day is what makes a long simulation unaffordable — but
*skipping* appraisal is worse, because then nothing downstream ever runs and a
simulated life produces no psychology at all. That was the observed failure:
238 experiences and no appraisals.

So routine and minor experiences are appraised here, from what the experience
already carries: its valence, its felt significance, whether anyone else was
involved, and how novel its class is. Meaningful and above still go to the
model, because those are the ones where reading them wrongly matters.

This produces a real :class:`Appraisal` — ``source="heuristic"`` — and every
engine downstream treats it exactly like any other. Patch spec 12.3:
``重要なのは正式Appraisal objectが下流へ届くこと``.
"""

from __future__ import annotations

from app.events.model import Event
from app.psychology.models import Appraisal
from app.psychology.policy import HeuristicAppraisalRules

MODULE = "heuristic_appraisal"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def appraise_event(event: Event, rules: HeuristicAppraisalRules) -> Appraisal:
    """Read one event from what it carries, without a model.

    Nothing here maps an event *type* to an emotion — that is what spec 11.1
    forbids. The dimensions come from the event's own attributes, so two
    routine days with different valence are appraised differently and lead to
    different emotions.
    """
    payload = event.payload
    valence = float(getattr(payload, "valence", 0.0) or 0.0)
    significance = float(getattr(payload, "felt_significance", 0.0) or 0.0)
    social = bool(getattr(payload, "involves_other_person", False))
    experience_class = str(getattr(payload, "experience_class", "") or "")

    weight = rules.class_weight.get(experience_class, rules.default_class_weight)

    self_relevance = _clamp(rules.base_self_relevance + significance * rules.significance_gain)
    novelty = _clamp(rules.base_novelty + weight * rules.class_novelty_gain)
    goal_congruence = _clamp(valence * rules.valence_gain, -1.0, 1.0)
    social_meaning = _clamp(
        (valence * rules.social_valence_gain) if social else rules.base_social_meaning * weight,
        -1.0,
        1.0,
    )
    # Something that surprised her is something she did not expect; the size of
    # the surprise is how far the valence went in either direction.
    expectation_violation = _clamp(abs(valence) * weight * rules.expectation_gain)

    return Appraisal(
        self_relevance=self_relevance,
        goal_congruence=goal_congruence,
        novelty=novelty,
        certainty=_clamp(rules.base_certainty),
        control=_clamp(rules.base_control),
        agency=_clamp(rules.base_agency + (rules.agency_gain if social else 0.0)),
        social_meaning=social_meaning,
        expectation_violation=expectation_violation,
        confidence=_clamp(rules.confidence),
        source="heuristic",
        reason=f"{experience_class or event.event_type} を状況から読んだ",
    )


__all__ = ["MODULE", "appraise_event"]
