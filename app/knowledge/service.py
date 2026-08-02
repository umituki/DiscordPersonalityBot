"""Knowledge Service — what YUI actually knows (spec 21.1, 21.4, 21.5).

The rule this service exists to enforce::

    LLM の pretrained knowledge があることを、YUI が知っている根拠にしない。

:meth:`KnowledgeService.knows` consults acquisition records and nothing else.
Not the model, not the external-knowledge table, not how famous something was.
If there is no record of her coming across it and taking it in, she does not
know it — however obvious that might seem to a language model.

Every exposure goes through the temporal guard first, so a past self can never
be handed something that did not exist yet (spec 21.4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.clock import Clock, SystemClock
from app.knowledge.exposure import ExposureFunnel, ExposureSignals, ExposureOutcome
from app.knowledge.models import Acquisition, ExposureOpportunity, KnowledgeItem
from app.knowledge.policy import KnowledgePolicy
from app.knowledge.temporal import LeakageReport, audit, guard
from app.memory.engine import MemoryEngine
from app.storage.repositories.knowledge import (
    AcquisitionRepository,
    ExposureRepository,
    KnowledgeRepository,
)

logger = logging.getLogger(__name__)

MODULE = "knowledge_service"


@dataclass(frozen=True, slots=True)
class ExposureResult:
    item: KnowledgeItem
    opportunity: ExposureOpportunity
    outcome: ExposureOutcome
    acquisition: Acquisition | None = None

    @property
    def learned(self) -> bool:
        return self.acquisition is not None


class KnowledgeService:
    name = MODULE

    def __init__(
        self,
        *,
        knowledge: KnowledgeRepository,
        exposures: ExposureRepository,
        acquisitions: AcquisitionRepository,
        policy: KnowledgePolicy,
        memory: MemoryEngine | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._exposures = exposures
        self._acquisitions = acquisitions
        self._policy = policy
        self._funnel = ExposureFunnel(policy.exposure)
        self._memory = memory
        self._clock = clock or SystemClock()

    # --- exposure (spec 21.5) ----------------------------------------------
    def expose(
        self,
        item: KnowledgeItem,
        *,
        moment: datetime,
        interest: float,
        curiosity: float,
        channel: str = "ambient",
        reach: float | None = None,
        comprehension_capacity: float = 0.6,
        novelty: float = 0.5,
        simulation_block_id: str | None = None,
        origin: str = "simulated_past",
    ) -> ExposureResult:
        """Put something in front of YUI and see how far it gets.

        Raises :class:`~app.knowledge.temporal.TemporalLeakageError` if the
        information did not exist yet — that is a caller bug, not a candidate
        to filter out silently (spec 21.4).
        """
        guard(item, moment)

        signals = ExposureSignals(
            salience=item.salience,
            reach=item.salience if reach is None else reach,
            complexity=item.complexity,
            interest=interest,
            curiosity=curiosity,
            comprehension_capacity=comprehension_capacity,
            novelty=novelty,
        )
        outcome = self._funnel.evaluate(signals)
        opportunity = self._exposures.record(
            knowledge_id=item.knowledge_id,
            occurred_at=moment,
            channel=channel,
            salience=item.salience,
            reach=signals.reach,
            stage_reached=outcome.stage_reached,
            acquired=outcome.acquired,
            reason=outcome.reason,
            simulation_block_id=simulation_block_id,
            now=self._clock.now(),
        )

        if not outcome.acquired:
            # Spec 21.5: it happened near her and did not become knowledge.
            return ExposureResult(item, opportunity, outcome)

        semantic_id = None
        if self._memory is not None:
            semantic = self._memory.note_semantic(
                item.statement,
                origin=origin,  # type: ignore[arg-type]
                topics=(item.topic,) if item.topic else (),
            )
            semantic_id = semantic.semantic_id

        acquisition = self._acquisitions.record(
            knowledge_id=item.knowledge_id,
            opportunity_id=opportunity.opportunity_id,
            acquired_at=moment,
            comprehension=outcome.comprehension,
            retention=outcome.retention,
            semantic_memory_id=semantic_id,
            origin=origin,
        )
        logger.info(
            "knowledge acquired topic=%s retention=%.2f", item.topic, outcome.retention
        )
        return ExposureResult(item, opportunity, outcome, acquisition)

    def expose_available(
        self,
        *,
        moment: datetime,
        interest_by_topic: dict[str, float],
        curiosity: float,
        limit: int = 20,
        default_interest: float = 0.2,
        simulation_block_id: str | None = None,
        origin: str = "simulated_past",
    ) -> list[ExposureResult]:
        """Offer a slice of what the world had at ``moment``, one funnel each.

        Not simply the most salient items: ``ORDER BY salience DESC LIMIT n``
        offered the identical handful in every block of a decade, so anything
        she was actually interested in — which is usually not the loudest thing
        around — was never in front of her at all. What she seeks out and what
        is hard to miss both count, and something she already knows steps aside
        so the block's attention goes somewhere new.

        This changes what is *offered*. Whether any of it is taken in is still
        the funnel's decision, stage by stage (spec 21.5).
        """
        pool = self._knowledge.available_at(moment, limit=max(limit * 10, 50))
        results = []
        for item in self._select(pool, interest_by_topic, limit, default_interest):
            results.append(
                self.expose(
                    item,
                    moment=moment,
                    interest=interest_by_topic.get(item.topic, default_interest),
                    curiosity=curiosity,
                    simulation_block_id=simulation_block_id,
                    origin=origin,
                )
            )
        return results

    def _select(
        self,
        pool: list[KnowledgeItem],
        interest_by_topic: dict[str, float],
        limit: int,
        default_interest: float,
    ) -> list[KnowledgeItem]:
        """Which of the available items are put in front of her this time."""
        if limit <= 0 or not pool:
            return []

        def rank(item: KnowledgeItem) -> tuple[float, str]:
            interest = interest_by_topic.get(item.topic, default_interest)
            return (-(interest + item.salience), item.knowledge_id)

        ordered = sorted(pool, key=rank)
        known = {
            acquisition.knowledge_id
            for acquisition in self._acquisitions.all(status="retained", limit=1000)
        }
        fresh = [item for item in ordered if item.knowledge_id not in known]
        chosen = fresh[:limit]
        if len(chosen) < limit:
            already = {item.knowledge_id for item in chosen}
            chosen += [item for item in ordered if item.knowledge_id not in already][
                : limit - len(chosen)
            ]
        return chosen

    # --- what she knows (spec 21.1) ----------------------------------------
    def knows(self, knowledge_id: str) -> bool:
        """True only when there is a retained acquisition record."""
        acquisition = self._acquisitions.for_knowledge(knowledge_id)
        return acquisition is not None and acquisition.usable

    def knows_statement(self, statement: str) -> bool:
        item = self._knowledge.by_statement(statement)
        return False if item is None else self.knows(item.knowledge_id)

    def acquisition_for(self, knowledge_id: str) -> Acquisition | None:
        return self._acquisitions.for_knowledge(knowledge_id)

    def retained(self, *, limit: int = 200) -> list[Acquisition]:
        return self._acquisitions.all(status="retained", limit=limit)

    def retained_items(self, *, limit: int = 100) -> list[KnowledgeItem]:
        items = []
        for acquisition in self._acquisitions.all(status="retained", limit=limit):
            item = self._knowledge.knowledge(acquisition.knowledge_id)
            if item is not None:
                items.append(item)
        return items

    # --- forgetting (spec 10.5 applied to knowledge) ------------------------
    def apply_fading(self, *, now: datetime | None = None) -> int:
        """Knowledge fades with time, faster when it was never stable."""
        moment = now or self._clock.now()
        rules = self._policy.retention
        faded = 0
        for acquisition in self._acquisitions.all(limit=1000):
            if acquisition.status == "forgotten":
                continue
            item = self._knowledge.knowledge(acquisition.knowledge_id)
            if item is None:
                continue
            years = max(0.0, (moment - acquisition.acquired_at).total_seconds() / 31_557_600.0)
            if years <= 0:
                continue
            retention = max(
                0.0, acquisition.retention - rules.fade_for(item.stability) * years
            )
            if abs(retention - acquisition.retention) < 1e-9:
                continue
            status = "forgotten" if retention < rules.forgotten_below else "faded"
            self._acquisitions.set_status(
                acquisition.acquisition_id, status, retention=round(retention, 6)
            )
            faded += 1
        return faded

    # --- audit (spec 22.7) --------------------------------------------------
    def chronology_audit(self, *, until: datetime) -> LeakageReport:
        """Every acquisition must predate nothing (spec 21.4, 22.7).

        Checks each acquired item against the moment it was acquired, so a
        leak anywhere in a whole simulated life shows up as one report.
        """
        leaked: list[str] = []
        checked = 0
        for acquisition in self._acquisitions.all(limit=1000):
            item = self._knowledge.knowledge(acquisition.knowledge_id)
            if item is None:
                continue
            checked += 1
            if not item.existed_at(acquisition.acquired_at):
                leaked.append(item.knowledge_id)
        return LeakageReport(moment=until, checked=checked, leaked=tuple(leaked))

    def exposure_audit(self, moment: datetime) -> LeakageReport:
        return audit(self._knowledge.available_at(moment, limit=1000), moment)

    # --- counts for status --------------------------------------------------
    def counts(self) -> dict[str, int]:
        return {
            "knowledge": self._knowledge.count(),
            "opportunities": self._exposures.count(),
            "acquired": self._acquisitions.count(status="retained"),
        }
