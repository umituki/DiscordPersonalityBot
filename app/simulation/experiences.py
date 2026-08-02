"""Experience classes and temporal compression (spec 22.4, 22.5).

Two rules from spec 22.4 that are easy to state and easy to violate:

    Major event をキャラクターの深みのために乱発しない。
    Trauma を性格形成の便利な説明装置にしない。

A generator that samples from a distribution will, over a long enough life,
produce a run of catastrophes — and each one will look individually plausible.
So the shares are not the whole rule: there are hard ceilings on major events
per year, on turning points in a whole life, and on how many of the major
events may be negative. When a ceiling is reached the class is *demoted*, not
skipped, so the life still has an event there; it is simply an ordinary one.

Deterministic given a seeded RNG, so a simulation can be replayed (spec 29).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from random import Random

from pydantic import BaseModel, Field

from app.simulation.models import EXPERIENCE_CLASSES, ExperienceClass
from app.simulation.policy import ExperienceRules

logger = logging.getLogger(__name__)

#: Cheaper classes come first, so demotion is a step down this list.
ORDER: tuple[ExperienceClass, ...] = EXPERIENCE_CLASSES

#: Spec 22.4: which classes justify which cost. Routine never reaches an LLM.
LLM_COST: dict[ExperienceClass, str] = {
    "routine": "none",
    "minor": "none",
    "meaningful": "light",
    "major": "detailed",
    "turning_point": "detailed",
}


class ExperienceNarration(BaseModel):
    """What the model is asked for when an experience is worth describing.

    Deliberately small: what happened, what it was about, and how much it
    landed. It cannot return a trait, a value or a personality change — those
    are produced by living through the event, not by narrating it (spec 22.6).
    """

    summary: str = Field(min_length=1)
    topics: list[str] = Field(default_factory=list)
    #: A signal, never the final importance (spec 2.6).
    felt_significance: float = Field(default=0.0, ge=0.0, le=1.0)
    involves_other_person: bool = False


@dataclass
class ExperienceBudget:
    """What a life has already spent, so ceilings can be enforced."""

    years: float
    major_count: int = 0
    turning_point_count: int = 0
    negative_major_count: int = 0
    demotions: list[str] = field(default_factory=list)

    @property
    def major_allowance(self) -> float:
        return self.years

    def negative_share(self) -> float:
        total = self.major_count + self.turning_point_count
        return 0.0 if total == 0 else self.negative_major_count / total


@dataclass(frozen=True, slots=True)
class Experience:
    """One sampled experience, before anything is generated for it."""

    experience_class: ExperienceClass
    valence: float
    demoted_from: ExperienceClass | None = None

    @property
    def llm_cost(self) -> str:
        return LLM_COST[self.experience_class]

    @property
    def is_significant(self) -> bool:
        return self.experience_class in ("major", "turning_point")


class ExperienceSampler:
    def __init__(self, policy: ExperienceRules, *, rng: Random | None = None) -> None:
        self._policy = policy
        self._rng = rng or Random()

    def sample(self, budget: ExperienceBudget) -> Experience:
        """Draw a class, then apply the ceilings that a draw cannot know about."""
        drawn = self._draw()
        valence = self._valence(drawn)
        allowed = self._enforce_ceilings(drawn, valence, budget)

        if allowed is not drawn:
            budget.demotions.append(f"{drawn}->{allowed}")
            logger.debug("experience demoted %s -> %s", drawn, allowed)
            valence = self._valence(allowed)

        if allowed == "major":
            budget.major_count += 1
            if valence < 0:
                budget.negative_major_count += 1
        elif allowed == "turning_point":
            budget.turning_point_count += 1
            if valence < 0:
                budget.negative_major_count += 1

        return Experience(
            experience_class=allowed,
            valence=valence,
            demoted_from=None if allowed is drawn else drawn,
        )

    def _draw(self) -> ExperienceClass:
        roll = self._rng.random()
        for name, boundary in self._policy.cumulative:
            if roll <= boundary:
                return name  # type: ignore[return-value]
        return "routine"

    def _valence(self, experience_class: ExperienceClass) -> float:
        """Ordinary life is mildly positive; significance cuts both ways."""
        if experience_class in ("routine", "minor"):
            return round(self._rng.uniform(-0.2, 0.4), 4)
        return round(self._rng.uniform(-0.9, 0.9), 4)

    def _enforce_ceilings(
        self, drawn: ExperienceClass, valence: float, budget: ExperienceBudget
    ) -> ExperienceClass:
        """Apply the ceilings, re-checking after every demotion.

        A turning point demoted to a major must still face the major ceiling —
        otherwise a life that has run out of turning points quietly overspends
        its major events instead, which is the same failure wearing a different
        name (spec 22.4).
        """
        policy = self._policy
        current = drawn

        while current in ("turning_point", "major"):
            if current == "turning_point":
                over_limit = budget.turning_point_count >= policy.max_turning_points_total
            else:
                over_limit = budget.major_count >= policy.max_major_per_year * max(
                    1.0, budget.years
                )
            hardship = valence < 0 and self._too_much_hardship(budget)
            if not over_limit and not hardship:
                return current
            current = self._demote(current)

        return current

    def _too_much_hardship(self, budget: ExperienceBudget) -> bool:
        """Spec 22.4: hardship is not a convenient explanation for a person."""
        total = budget.major_count + budget.turning_point_count
        if total < 2:
            return False
        return budget.negative_share() >= self._policy.max_negative_major_share

    @staticmethod
    def _demote(experience_class: ExperienceClass) -> ExperienceClass:
        index = ORDER.index(experience_class)
        return ORDER[max(0, index - 1)]
