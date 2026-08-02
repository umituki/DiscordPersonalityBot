"""Temperamental Seed from the initial questions (spec 22.1, 22.2).

    回答は Temperamental Seed のみ生成し、完成人格を設定しない。

Seven questions can bias where a life starts. They cannot decide who somebody
becomes, and this module is written so that they structurally cannot:

* the only output is a temperament — the bottom layer of spec 12.1;
* every answer moves a temperament dimension by at most
  ``max_temperament_shift`` from the neutral midpoint, so no answer can pin a
  dimension to an extreme;
* values, habits, personality traits above temperament, and the narrative
  identity have no representation here at all. They are what the simulation
  produces (spec 22.3).

``avoid`` is kept as a *constraint on generation*, not as a trait. Asking not
to become bitter is not the same as being given a bitterness score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Sequence

from app import ids
from app.clock import Clock, SystemClock
from app.simulation.models import TemperamentSeed
from app.simulation.policy import SeedRules

logger = logging.getLogger(__name__)

MODULE = "temperament_seed_builder"
SEED = "sed"

#: Spec 22.2's seven questions, and the temperament dimension each one biases.
#: ``None`` means the answer is kept as context and moves no dimension at all.
QUESTIONS: tuple[tuple[str, str | None], ...] = (
    ("overall_mood", "emotional_stability"),
    ("interpersonal_distance", "extraversion"),
    ("emotional_expression", "agreeableness"),
    ("independence", "assertiveness"),
    ("value_direction", "conscientiousness"),
    ("interests", None),
    ("avoid", None),
)

QUESTION_IDS: tuple[str, ...] = tuple(name for name, _ in QUESTIONS)

#: Answers are ordinal, not numeric, so an owner cannot type "0.97".
SCALE: Mapping[str, float] = {
    "very_low": -1.0,
    "low": -0.5,
    "neutral": 0.0,
    "high": 0.5,
    "very_high": 1.0,
}


class SeedError(ValueError):
    """Raised when the questionnaire answers cannot produce a seed."""


@dataclass(frozen=True, slots=True)
class SeedRequest:
    """The owner's answers. Deliberately coarse."""

    answers: Mapping[str, str]
    interests: Sequence[str] = ()
    avoid: Sequence[str] = ()


class SeedBuilder:
    name = MODULE

    def __init__(self, policy: SeedRules, *, clock: Clock | None = None) -> None:
        self._policy = policy
        self._clock = clock or SystemClock()

    def build(self, request: SeedRequest) -> TemperamentSeed:
        unknown = set(request.answers) - set(QUESTION_IDS)
        if unknown:
            raise SeedError(
                f"unknown question(s): {sorted(unknown)}; the questionnaire is "
                f"{list(QUESTION_IDS)} (spec 22.2)"
            )

        temperament: dict[str, float] = {}
        for question, dimension in QUESTIONS:
            if dimension is None:
                continue
            answer = request.answers.get(question, "neutral")
            if answer not in SCALE:
                raise SeedError(
                    f"answer {answer!r} for {question!r} is not one of {sorted(SCALE)}"
                )
            temperament[dimension] = round(
                self._policy.neutral + SCALE[answer] * self._policy.max_temperament_shift, 6
            )

        # Openness has no question of its own: what YUI becomes curious about
        # is an outcome of the life she lives, not something the owner sets.
        temperament.setdefault("openness", self._policy.neutral)

        seed = TemperamentSeed(
            seed_id=ids.new_id(SEED),
            answers=dict(request.answers),
            temperament=temperament,
            avoid=tuple(request.avoid),
            interests=tuple(request.interests),
            created_at=self._clock.now(),
        )
        logger.info(
            "temperament seed built dimensions=%d interests=%d",
            len(seed.temperament),
            len(seed.interests),
        )
        return seed

    @property
    def questions(self) -> tuple[str, ...]:
        return QUESTION_IDS

    def bounds(self) -> tuple[float, float]:
        """The only range any answer can reach (spec 22.2)."""
        return (
            self._policy.neutral - self._policy.max_temperament_shift,
            self._policy.neutral + self._policy.max_temperament_shift,
        )
