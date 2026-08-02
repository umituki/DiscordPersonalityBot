"""NPC Relationship Engine — single writer of ``npc_relationships`` (spec 20).

This engine is where an NPC interaction *means* something. It keeps three
separations the specification insists on:

* **The USER is not an NPC** (spec 1.2, 2.10). The engine ignores every event
  whose actor is the USER, exactly as the USER relationship engine ignores
  every NPC event. Neither can ever write the other's state.
* **The profile is not the model** (spec 20.2). What an NPC is really like
  lives in ``npcs``; what YUI thinks they are like lives in ``npc_models`` and
  is allowed to be wrong. Only tier 2 people get a model at all (spec 20.1).
* **Silence is not conflict** (spec 20.4). Familiarity and closeness come from
  interactions, conflict comes from bad ones, and the lifecycle stage is
  derived from all three plus how long it has been — so drifting apart and
  falling out never produce the same answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.society.events import (
    NPC_INTERACTION,
    NPC_RELATIONSHIP_STAGE_CHANGED,
    NPCInteractionPayload,
    StageChangedPayload,
)
from app.society.lifecycle import stage_for
from app.society.models import NPCModel, NPCRelationship
from app.society.policy import SocietyPolicy
from app.state.proposal import StateChangeProposal
from app.storage.repositories.society import (
    NPCModelRepository,
    NPCRelationshipRepository,
    NPCRepository,
)

logger = logging.getLogger(__name__)

MODULE = "npc_relationship_engine"
DOMAIN = "npc_relationships"

FAMILIARITY = "familiarity"
CLOSENESS = "closeness"
CONFLICT = "conflict"

#: State keys are ``<npc_id>:<dimension>`` so one domain can hold a whole
#: social world without inventing a key per person in the registry.
SEPARATOR = ":"


def state_key(npc_id: str, dimension: str) -> str:
    return f"{npc_id}{SEPARATOR}{dimension}"


@dataclass(frozen=True, slots=True)
class InteractionOutcome:
    relationship: NPCRelationship
    previous_stage: str
    stage_changed: bool
    model: NPCModel | None
    proposals: tuple[StateChangeProposal, ...]
    events: tuple[Event, ...]


class NPCRelationshipEngine:
    name = MODULE

    def __init__(
        self,
        *,
        npcs: NPCRepository,
        relationships: NPCRelationshipRepository,
        models: NPCModelRepository,
        policy: SocietyPolicy,
        clock: Clock | None = None,
    ) -> None:
        self._npcs = npcs
        self._relationships = relationships
        self._models = models
        self._policy = policy
        self._clock = clock or SystemClock()

    # --- as a subscriber ---------------------------------------------------
    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        # Spec 2.10: an NPC event and a USER message are never the same thing,
        # and this engine only ever answers to the first.
        if event.actor_type != "npc" or event.event_type != NPC_INTERACTION:
            return SubscriberResult()
        payload = event.payload
        if not isinstance(payload, NPCInteractionPayload):
            return SubscriberResult()

        outcome = self.record_interaction(
            npc_id=payload.npc_id,
            valence=payload.valence,
            event=event,
        )
        return SubscriberResult(proposals=outcome.proposals, events=outcome.events)

    # --- the actual update -------------------------------------------------
    def record_interaction(
        self, *, npc_id: str, valence: float, event: Event
    ) -> InteractionOutcome:
        now = self._clock.now()
        npc = self._npcs.get(npc_id)
        if npc is None or not npc.keeps_relationship_state:
            # Spec 20.1: a tier 0 presence is not tracked. Nothing is written,
            # and nothing pretends a relationship exists.
            return InteractionOutcome(
                relationship=NPCRelationship(
                    relationship_id="", npc_id=npc_id, updated_at=now
                ),
                previous_stage="unmet",
                stage_changed=False,
                model=None,
                proposals=(),
                events=(),
            )

        current = self._relationships.ensure(npc_id, now=now)
        rules = self._policy.relationship
        limit = rules.max_change_per_interaction

        familiarity = _step(current.familiarity, rules.familiarity_per_interaction, limit)
        closeness = current.closeness
        conflict = current.conflict

        if valence >= rules.positive_valence_threshold:
            closeness = _step(closeness, rules.closeness_per_positive * valence, limit)
            conflict = _step(conflict, -rules.conflict_relief_per_positive, limit)
        elif valence <= rules.negative_valence_threshold:
            conflict = _step(conflict, rules.conflict_per_negative * abs(valence), limit)

        stage = stage_for(
            previous_stage=current.stage,
            familiarity=familiarity,
            closeness=closeness,
            conflict=conflict,
            days_since_contact=0.0,
            interaction_count=current.interaction_count + 1,
            policy=self._policy.lifecycle,
        )
        updated = self._relationships.update(
            npc_id,
            stage=stage,
            previous_stage=current.stage,
            familiarity=familiarity,
            closeness=closeness,
            conflict=conflict,
            interaction_count=current.interaction_count + 1,
            last_contact_at=now,
            first_met_at=current.first_met_at or now,
            now=now,
        )
        model = self._update_model(npc, valence=valence, now=now)

        proposals = tuple(
            StateChangeProposal.set_value(
                source_event_id=event.event_id,
                source_module=MODULE,
                target_domain=DOMAIN,
                target_key=state_key(npc_id, dimension),
                value=round(value, 6),
                reason_codes=("npc_interaction", stage),
                evidence_ids=(event.event_id,),
                clock=self._clock,
            )
            for dimension, value, previous in (
                (FAMILIARITY, familiarity, current.familiarity),
                (CLOSENESS, closeness, current.closeness),
                (CONFLICT, conflict, current.conflict),
            )
            if abs(value - previous) > 1e-9
        )

        events: tuple[Event, ...] = ()
        if stage != current.stage:
            events = (
                event.child(
                    event_type=NPC_RELATIONSHIP_STAGE_CHANGED,
                    category="social",
                    actor_type="npc",
                    source_type=MODULE,
                    clock=self._clock,
                    priority="P4",
                    payload=StageChangedPayload(
                        npc_id=npc_id,
                        previous_stage=current.stage,
                        stage=stage,
                        reason="interaction",
                        days_since_contact=0.0,
                    ),
                ),
            )
            logger.info(
                "npc relationship stage %s -> %s npc=%s", current.stage, stage, npc.name
            )

        return InteractionOutcome(
            relationship=updated,
            previous_stage=current.stage,
            stage_changed=stage != current.stage,
            model=model,
            proposals=proposals,
            events=events,
        )

    def _update_model(self, npc, *, valence: float, now: datetime) -> NPCModel | None:
        """Only a significant NPC gets a model of their own (spec 20.1, 20.2)."""
        if npc.tier < self._policy.npc.min_tier_for_model:
            return None
        rules = self._policy.npc
        current = self._models.ensure(npc.npc_id, now=now)
        direction = 1.0 if valence >= 0 else -1.0
        return self._models.update(
            npc.npc_id,
            # What YUI perceives moves toward what she has experienced, which
            # is not the same as what the profile says (spec 20.2).
            perceived_warmth=_clamp(
                current.perceived_warmth + direction * rules.model_step * abs(valence)
            ),
            perceived_reliability=_clamp(
                current.perceived_reliability + direction * rules.model_step * 0.5
            ),
            perceived_availability=_clamp(
                current.perceived_availability + rules.model_step * 0.25
            ),
            observation_count=current.observation_count + 1,
            confidence=min(
                rules.max_model_confidence,
                current.confidence + rules.model_confidence_per_observation,
            ),
            now=now,
        )

    # --- passage of time (spec 20.4) ---------------------------------------
    def review_stages(self, *, now: datetime | None = None) -> list[InteractionOutcome]:
        """Re-derive every stage from how long it has been. Writes no psychology.

        Contact fading is a fact about the calendar, so it is applied here
        rather than waiting for an interaction that may never come. It can only
        ever produce ``distant``/``dormant`` — never ``strained`` and never
        ``ended`` (spec 20.4).
        """
        moment = now or self._clock.now()
        changed: list[InteractionOutcome] = []
        for relationship in self._relationships.all():
            if relationship.stage == "ended":
                continue
            days = (
                None
                if relationship.last_contact_at is None
                else max(0.0, (moment - relationship.last_contact_at).total_seconds() / 86400.0)
            )
            stage = stage_for(
                previous_stage=relationship.stage,
                familiarity=relationship.familiarity,
                closeness=relationship.closeness,
                conflict=relationship.conflict,
                days_since_contact=days,
                interaction_count=relationship.interaction_count,
                policy=self._policy.lifecycle,
            )
            if stage == relationship.stage:
                continue
            updated = self._relationships.update(
                relationship.npc_id,
                stage=stage,
                previous_stage=relationship.stage,
                familiarity=relationship.familiarity,
                closeness=relationship.closeness,
                conflict=relationship.conflict,
                interaction_count=relationship.interaction_count,
                last_contact_at=relationship.last_contact_at,
                now=moment,
            )
            changed.append(
                InteractionOutcome(
                    relationship=updated,
                    previous_stage=relationship.stage,
                    stage_changed=True,
                    model=None,
                    proposals=(),
                    events=(),
                )
            )
        return changed

    # --- deliberate acts ----------------------------------------------------
    def end_relationship(self, npc_id: str, *, reason: str) -> NPCRelationship:
        """Ending is a decision. Time never does this on its own (spec 20.4)."""
        return self._relationships.end(npc_id, reason=reason, now=self._clock.now())

    def reconnect(self, npc_id: str) -> NPCRelationship:
        return self._relationships.reopen(npc_id, now=self._clock.now())

    # --- reads --------------------------------------------------------------
    def relationship(self, npc_id: str) -> NPCRelationship | None:
        return self._relationships.for_npc(npc_id)

    def model_of(self, npc_id: str) -> NPCModel | None:
        return self._models.for_npc(npc_id)

    def all_relationships(self) -> list[NPCRelationship]:
        return self._relationships.all()

    def close_others(self) -> list[NPCRelationship]:
        """People other than the USER that YUI is actually close to."""
        return [
            relationship
            for relationship in self._relationships.all()
            if relationship.stage in ("close", "familiar", "reconnected")
        ]


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 6)


def _step(current: float, delta: float, limit: float) -> float:
    bounded = max(-limit, min(limit, delta))
    return _clamp(current + bounded)
