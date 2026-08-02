"""Self Engine — single writer of the self model (spec 9.3, 12.4, 23.2).

The self-concept is not personality and not a mirror of behaviour:

* **It is allowed to be wrong.** Nothing here forces a schema to agree with what
  YUI actually did (spec 12.4).
* **It lags.** Behaviour evidence accumulates in ``pending_evidence`` and only
  moves the schema once enough has built up, so the self-concept changes after
  the behaviour, not with it (spec 23.2).
* **Mixed evidence costs clarity, not truth.** Conflicting observations lower
  self-concept clarity rather than flipping the schema back and forth.

Possible selves are kept separately from who YUI thinks she is now.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.clock import Clock, SystemClock
from app.social.belief_policy import SelfSchemaRules
from app.social.models import PossibleSelf, SelfSchema
from app.storage.repositories.social import SelfRepository

logger = logging.getLogger(__name__)

MODULE = "self_engine"

SUPPORTS = "supports"
CONTRADICTS = "contradicts"


@dataclass(frozen=True, slots=True)
class SelfUpdate:
    schema: SelfSchema
    previous_strength: float
    #: True when this observation actually moved the self-concept.
    strength_changed: bool
    pending_evidence: float

    @property
    def lagged(self) -> bool:
        """Evidence was recorded but the schema has not caught up yet."""
        return not self.strength_changed


class SelfEngine:
    name = MODULE

    def __init__(
        self,
        repository: SelfRepository,
        policy: SelfSchemaRules,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()

    def observe_behaviour(
        self,
        *,
        name: str,
        statement: str,
        stance: str,
        event_id: str | None = None,
        weight: float = 1.0,
    ) -> SelfUpdate:
        """Record behaviour that supports or contradicts a self schema."""
        now = self._clock.now()
        schema = self._repository.ensure(
            name=name,
            statement=statement,
            strength=self._policy.initial_strength,
            now=now,
        )
        previous_strength = schema.strength

        supporting = schema.supporting_count + (1 if stance == SUPPORTS else 0)
        contradicting = schema.contradicting_count + (1 if stance == CONTRADICTS else 0)

        # Evidence pushes in a direction but is held back until enough of it
        # has accumulated (spec 23.2).
        direction = 1.0 if stance == SUPPORTS else -1.0
        pending = schema.pending_evidence + direction * weight

        strength = schema.strength
        strength_changed = False
        if abs(pending) >= self._policy.evidence_before_change:
            step = self._policy.strength_step * (1.0 if pending > 0 else -1.0)
            step = max(
                -self._policy.max_change_per_update,
                min(self._policy.max_change_per_update, step),
            )
            strength = max(0.0, min(1.0, schema.strength + step))
            # Only the evidence that was "spent" is consumed; the remainder
            # carries forward.
            pending -= self._policy.evidence_before_change * (1.0 if pending > 0 else -1.0)
            strength_changed = abs(strength - schema.strength) > 1e-9

        clarity = self._clarity(supporting, contradicting)

        updated = self._repository.update(
            schema_id=schema.schema_id,
            strength=strength,
            clarity=clarity,
            supporting_count=supporting,
            contradicting_count=contradicting,
            pending_evidence=pending,
            last_behaviour_at=now,
            now=now,
        )
        if event_id is not None:
            self._repository.connect_event(
                schema_id=schema.schema_id,
                event_id=event_id,
                relation=stance,
                now=now,
            )

        if strength_changed:
            logger.info(
                "self schema moved name=%s %.3f -> %.3f", name, previous_strength, strength
            )
        return SelfUpdate(updated, previous_strength, strength_changed, pending)

    def _clarity(self, supporting: int, contradicting: int) -> float:
        """Clarity falls as evidence becomes mixed (spec 12.4)."""
        total = supporting + contradicting
        if total == 0:
            return 0.5
        consistency = abs(supporting - contradicting) / total
        clarity = self._policy.clarity_from_consistency * consistency
        return round(max(self._policy.clarity_floor, min(1.0, clarity)), 6)

    # --- reads -------------------------------------------------------------
    def schema(self, name: str) -> SelfSchema | None:
        return self._repository.by_name(name)

    def active_schemas(self, *, limit: int = 20) -> list[SelfSchema]:
        return self._repository.active(limit=limit)

    def self_concept_clarity(self) -> float | None:
        """Average clarity across active schemas, or ``None`` when unknown."""
        schemas = self._repository.active(limit=100)
        if not schemas:
            return None
        return round(sum(schema.clarity for schema in schemas) / len(schemas), 6)

    def connections(self, name: str):
        schema = self._repository.by_name(name)
        return [] if schema is None else self._repository.connections(schema.schema_id)

    # --- possible selves (spec 12.4) ---------------------------------------
    def imagine(
        self, *, name: str, statement: str, valence: str, salience: float = 0.3
    ) -> PossibleSelf:
        """Record who YUI might become. Separate from who she thinks she is."""
        return self._repository.add_possible_self(
            name=name,
            statement=statement,
            valence=valence,
            salience=salience,
            now=self._clock.now(),
        )

    def possible_selves(self) -> list[PossibleSelf]:
        return self._repository.possible_selves()

    def last_behaviour_at(self, name: str) -> datetime | None:
        schema = self._repository.by_name(name)
        return None if schema is None else schema.last_behaviour_at
