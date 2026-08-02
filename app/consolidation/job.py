"""Consolidation Job (spec 9.5, 12.3, 22.6, 23.3).

    Deep State は別 Consolidation Job で処理する。

This is that job. It is retrospective: it reads what has *already* been
committed, with its provenance, and asks what — if anything — that means about
who YUI is becoming.

::

    committed state changes (with provenance)
    → adaptation evidence           (Layer 3, spec 12.2)
    → deep update candidates        (the five conditions of spec 12.3)
    → DEEP_CONSOLIDATION_REVIEW event through the normal pipeline
    → growth / value engines propose
    → arbitration → single transaction
    → ledger, history and DEEP_UPDATE_APPLIED, from what was actually committed
    → narrative themes
    → drift monitor (measures, never clamps)

Two things it deliberately does not do: it never writes state directly, and it
never records a deep change that the transaction did not accept.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.clock import Clock, SystemClock, from_iso
from app.config import RuntimeMode
from app.consolidation.adaptations import AdaptationEngine, AdaptationEvidence
from app.consolidation.drift import DriftMonitor, DriftReport
from app.consolidation.events import (
    DEEP_CONSOLIDATION_REVIEW,
    DEEP_UPDATE_APPLIED,
    DRIFT_ANOMALY_DETECTED,
    ConsolidationReviewPayload,
    DeepUpdateAppliedPayload,
    DriftAnomalyPayload,
)
from app.consolidation.growth import DeepUpdate, GrowthEngine
from app.consolidation.models import ConsolidationRunRecord
from app.consolidation.policy import GrowthPolicy
from app.consolidation.values import ValueEngine, ValueShift
from app.events.model import Event
from app.events.store import EventStore
from app.memory.engine import MemoryEngine
from app.orchestrator.processor import EventProcessor, ProcessingOutcome
from app.storage.repositories.growth import (
    CandidateRepository,
    ConsolidationRepository,
    NarrativeRepository,
)
from app.storage.repositories.memory import MemoryRepository
from app.storage.repositories.state import StateRepository

logger = logging.getLogger(__name__)

MODULE = "consolidation_job"

MOOD_DOMAIN = "mood"
MOOD_VALENCE = "valence"


@dataclass
class _Tally:
    """What one consolidation run learned about a single adaptation."""

    evidence: list[AdaptationEvidence] = field(default_factory=list)

    @property
    def direction(self) -> int:
        total = sum(item.direction for item in self.evidence)
        return 0 if total == 0 else (1 if total > 0 else -1)

    @property
    def contexts(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.context for item in self.evidence))

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence)

    @property
    def outcome_weight(self) -> float:
        return round(sum(item.outcome_weight for item in self.evidence), 6)

    @property
    def mood_independent(self) -> int:
        return sum(1 for item in self.evidence if item.mood_independent)


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    record: ConsolidationRunRecord | None
    changes_read: int = 0
    adaptations_moved: int = 0
    candidates_raised: int = 0
    deep_updates: tuple[DeepUpdate, ...] = ()
    value_shifts: tuple[ValueShift, ...] = ()
    themes_updated: int = 0
    semantic_facts: int = 0
    drift: DriftReport | None = None
    outcome: ProcessingOutcome | None = None
    skipped_reason: str = ""

    @property
    def ran(self) -> bool:
        return not self.skipped_reason

    @property
    def deep_change_count(self) -> int:
        return len(self.deep_updates) + len(self.value_shifts)


class ConsolidationJob:
    name = MODULE

    def __init__(
        self,
        *,
        processor: EventProcessor,
        event_store: EventStore,
        state: StateRepository,
        consolidations: ConsolidationRepository,
        candidates: CandidateRepository,
        narratives: NarrativeRepository,
        memories: MemoryRepository,
        adaptations: AdaptationEngine,
        growth: GrowthEngine,
        values: ValueEngine,
        drift: DriftMonitor,
        memory: MemoryEngine,
        policy: GrowthPolicy,
        mood_baseline_valence: float = 0.55,
        clock: Clock | None = None,
    ) -> None:
        self._processor = processor
        self._events = event_store
        self._state = state
        self._consolidations = consolidations
        self._candidates = candidates
        self._narratives = narratives
        self._memories = memories
        self._adaptations = adaptations
        self._growth = growth
        self._values = values
        self._drift = drift
        self._memory = memory
        self._policy = policy
        self._mood_baseline = mood_baseline_valence
        self._clock = clock or SystemClock()

    def due(self, *, now: datetime | None = None) -> bool:
        """Consolidation is slow work; it does not run on every event."""
        moment = now or self._clock.now()
        last = self._consolidations.last_completed_at()
        if last is None:
            return True
        interval = timedelta(hours=self._policy.consolidation.min_hours_between_runs)
        return moment - last >= interval

    async def run(
        self,
        *,
        kind: str = "routine",
        force: bool = False,
        now: datetime | None = None,
        mode: RuntimeMode | None = None,
    ) -> ConsolidationResult:
        """Run one consolidation.

        ``now`` is the effective time (patch spec 13). A consolidation inside a
        simulated life happens at a simulated moment, and every window it reads
        — evidence age, candidate persistence, theme recency — has to be
        measured against that moment or a decade of life collapses into the
        instant the machine happened to run.
        """
        now = now or self._clock.now()
        if not force and not self.due(now=now):
            recent = self._consolidations.recent(limit=1)
            return ConsolidationResult(
                record=recent[0] if recent else None, skipped_reason="not_due"
            )

        record = self._consolidations.start(kind=kind, now=now)
        # Seeding writes the growth ledger only. What YUI expresses is state,
        # and state still has to be proposed and committed (spec 9.3).
        self._growth.ensure_seeded()
        self._values.ensure_seeded()

        # --- 1. read what has already been committed -----------------------
        tallies, changes_read = self._scan_changes(now=now)

        # --- 2. adaptations: Layer 3 moves before Layer 4 (spec 23.2) ------
        review_event = self._review_event(record.consolidation_id, kind=kind, now=now)
        adaptations_moved = self._apply_adaptation_evidence(tallies)

        # --- 3. candidates: accumulated evidence, not yet a change ---------
        candidates_raised = self._raise_candidates(tallies, now=now)
        self._expire_stale_candidates(now=now)

        # --- 4. what keeps being remembered becomes general ----------------
        themes_updated = self._update_themes(now=now)
        semantic_facts = self._consolidate_semantic()

        # --- 5. the deep review runs through the normal pipeline -----------
        outcome = await self._processor.process(review_event, mode=mode)
        committed = self._committed_values(outcome)
        deep_updates = tuple(
            self._growth.apply_committed(
                committed=committed, run_id=outcome.run.run_id, now=now
            )
        )
        value_shifts = tuple(
            self._values.apply_committed(
                committed=committed, run_id=outcome.run.run_id, now=now
            )
        )
        await self._record_applied(review_event, deep_updates)

        # --- 6. drift monitoring: observe and classify only ----------------
        drift = self._drift.measure(now=now)
        await self._record_anomalies(review_event, drift)

        finished = self._consolidations.finish(
            record.consolidation_id,
            status="completed",
            changes_read=changes_read,
            adaptations_moved=adaptations_moved,
            candidates_raised=candidates_raised,
            deep_updates=len(deep_updates) + len(value_shifts),
            semantic_facts=semantic_facts,
            detail={
                "themes_updated": themes_updated,
                "drift_anomalies": len(drift.anomalies),
                "run_id": outcome.run.run_id,
            },
            now=now,
        )
        logger.info(
            "consolidation complete changes=%d adaptations=%d candidates=%d deep=%d",
            changes_read,
            adaptations_moved,
            candidates_raised,
            len(deep_updates) + len(value_shifts),
        )
        return ConsolidationResult(
            record=finished,
            changes_read=changes_read,
            adaptations_moved=adaptations_moved,
            candidates_raised=candidates_raised,
            deep_updates=deep_updates,
            value_shifts=value_shifts,
            themes_updated=themes_updated,
            semantic_facts=semantic_facts,
            drift=drift,
            outcome=outcome,
        )

    # --- 1. reading committed history --------------------------------------
    def _scan_changes(self, *, now: datetime) -> tuple[dict[str, _Tally], int]:
        rules = self._policy.consolidation
        since = self._consolidations.last_completed_at()
        if since is None:
            since = now - timedelta(days=rules.lookback_days)
        rows = self._state.changes_since(since, limit=rules.max_changes_per_run)

        tallies: dict[str, _Tally] = {}
        mood_valence = self._current_mood_valence()
        for row in rows:
            domain, key = row["domain"], row["key"]
            if domain == MOOD_DOMAIN and key == MOOD_VALENCE:
                # Replaying mood in commit order tells us what mood was like at
                # the time of the evidence that follows it, so "explainable by
                # temporary mood" can actually be answered (spec 12.3).
                mood_valence = _numeric(row["new_value_json"], fallback=mood_valence)
                continue
            delta = row["delta"]
            if delta is None:
                continue
            evidence = self._adaptations.evidence_from_change(
                domain=domain,
                key=key,
                delta=float(delta),
                source_module=row["source_module"],
                change_id=row["change_id"],
                committed_at=from_iso(row["committed_at"]),
                mood_independent=self._growth.gate.mood_is_ordinary(
                    mood_valence, self._mood_baseline
                ),
            )
            if evidence is None:
                continue
            tallies.setdefault(evidence.adaptation, _Tally()).evidence.append(evidence)
        return tallies, len(rows)

    def _current_mood_valence(self) -> float | None:
        entry = self._state.get(MOOD_DOMAIN, MOOD_VALENCE)
        return None if entry is None else entry.numeric

    # --- 2. adaptation evidence --------------------------------------------
    def _apply_adaptation_evidence(self, tallies: dict[str, _Tally]) -> int:
        moved = 0
        for _, tally in sorted(tallies.items()):
            for evidence in tally.evidence:
                if self._adaptations.observe(evidence).moved:
                    moved += 1
        return moved

    # --- 3. deep update candidates -----------------------------------------
    def _raise_candidates(self, tallies: dict[str, _Tally], *, now: datetime) -> int:
        raised = 0
        for name, tally in sorted(tallies.items()):
            direction = tally.direction
            if direction == 0:
                continue
            adaptation = self._adaptations.get(name)
            if adaptation is None:
                continue
            for domain, key, weight in self._deep_targets(name):
                target_direction = direction if weight >= 0 else -direction
                candidate = self._candidates.open_candidate(
                    domain=domain, key=key, direction=target_direction
                )
                if candidate is None:
                    candidate = self._candidates.create(
                        domain=domain,
                        key=key,
                        direction=target_direction,
                        source_adaptation=name,
                        now=now,
                    )
                    raised += 1
                contexts = tuple(dict.fromkeys((*candidate.contexts, *tally.contexts)))
                evidence_ids = tuple(
                    dict.fromkeys((*candidate.evidence_ids, *tally.evidence_ids))
                )[-50:]
                self._candidates.accumulate(
                    candidate.candidate_id,
                    # One consolidation run is one observation of the pattern:
                    # a burst of changes in a single day is not "repeated".
                    pattern_count=candidate.pattern_count + 1,
                    contexts=contexts,
                    evidence_ids=evidence_ids,
                    outcome_weight=round(candidate.outcome_weight + tally.outcome_weight, 6),
                    mood_independent_count=(
                        candidate.mood_independent_count + tally.mood_independent
                    ),
                    magnitude=abs(adaptation.offset),
                    now=now,
                )
        return raised

    def _deep_targets(self, adaptation: str) -> list[tuple[str, str, float]]:
        targets: list[tuple[str, str, float]] = []
        trait = self._policy.personality.adaptation_to_trait.get(adaptation)
        if trait is not None:
            targets.append(("personality", trait[0], trait[1]))
        value = self._policy.values.adaptation_to_value.get(adaptation)
        if value is not None:
            targets.append(("values", value[0], value[1]))
        disposition = self._policy.disposition.adaptation_to_disposition.get(adaptation)
        if disposition is not None:
            targets.append(("attachment_disposition", disposition[0], disposition[1]))
        return targets

    def _expire_stale_candidates(self, *, now: datetime) -> int:
        """Evidence that stopped recurring stops being evidence of anything."""
        expired = 0
        for candidate in self._candidates.accumulating():
            idle_days = (now - candidate.last_seen_at).total_seconds() / 86400.0
            if self._growth.gate.is_expired(candidate, now_days_idle=idle_days):
                self._candidates.resolve(candidate.candidate_id, "expired", now=now)
                expired += 1
        return expired

    # --- 4. narrative and semantic memory ----------------------------------
    def _topics(self) -> dict[str, list[str]]:
        by_topic: dict[str, list[str]] = {}
        for memory in self._memories.all_memories(limit=500):
            if memory.status != "active":
                continue
            for topic in memory.topics:
                by_topic.setdefault(topic, []).append(memory.memory_id)
        return by_topic

    def _update_themes(self, *, now: datetime) -> int:
        """Topics that keep coming back become the story YUI tells (spec 12.4)."""
        rules = self._policy.narrative
        updated = 0
        for topic, memory_ids in sorted(self._topics().items()):
            if len(memory_ids) < rules.min_supporting_memories:
                continue
            theme = self._narratives.ensure(topic, statement="", now=now)
            # Saturating, so a theme strengthens quickly at first and then only
            # slowly — a story does not become twice as true twice as fast.
            strength = min(
                rules.max_strength, round(len(memory_ids) / (len(memory_ids) + 6.0), 6)
            )
            status = "established" if strength >= rules.established_strength else "emerging"
            self._narratives.update(
                topic,
                strength=strength,
                supporting_memory_ids=memory_ids[:50],
                status=status,
                now=now,
            )
            if abs(strength - theme.strength) > 1e-9:
                updated += 1
        return updated

    def _consolidate_semantic(self) -> int:
        """Repeated episodes become a general fact (spec 10.2)."""
        rules = self._policy.consolidation
        created = 0
        for topic, memory_ids in sorted(self._topics().items()):
            if created >= rules.max_semantic_per_run:
                break
            if len(memory_ids) < rules.semantic_min_supporting_memories:
                continue
            self._memory.note_semantic(
                f"{topic} はわたしにとって繰り返し起きていることだ",
                origin="virtual_life",
                topics=(topic,),
                source_memory_ids=memory_ids[:10],
            )
            created += 1
        return created

    # --- 5/6. events for what actually happened ----------------------------
    def _review_event(
        self, consolidation_id: str, *, kind: str, now: datetime | None = None
    ) -> Event:
        return Event.create(
            event_type=DEEP_CONSOLIDATION_REVIEW,
            category="system",
            actor_type="system",
            source_type=MODULE,
            origin="system",
            priority="P4",
            # The review happened when the life reached this point, which is
            # not when the machine got round to it (patch spec 13).
            occurred_at=now,
            payload=ConsolidationReviewPayload(consolidation_id=consolidation_id, kind=kind),
            clock=self._clock,
        )

    @staticmethod
    def _committed_values(outcome: ProcessingOutcome) -> dict[str, float]:
        """Targets the transaction really wrote, with the value it wrote."""
        if outcome.arbitration is None or outcome.commit is None:
            return {}
        committed = set(outcome.commit.committed_targets)
        return {
            change.target: float(change.new_value)
            for change in outcome.arbitration.accepted
            if change.target in committed
            and isinstance(change.new_value, (int, float))
            and not isinstance(change.new_value, bool)
        }

    async def _record_applied(self, parent: Event, updates: tuple[DeepUpdate, ...]) -> None:
        for update in updates:
            await self._append(
                parent.child(
                    event_type=DEEP_UPDATE_APPLIED,
                    category="system",
                    actor_type="system",
                    source_type=MODULE,
                    clock=self._clock,
                    priority="P4",
                    payload=DeepUpdateAppliedPayload(
                        candidate_id=update.candidate.candidate_id,
                        target_domain=update.candidate.target_domain,
                        target_key=update.candidate.target_key,
                        previous_value=update.previous_value,
                        new_value=update.new_value,
                        baseline_after=update.baseline_after,
                        satisfied_conditions=update.decision.satisfied,
                    ),
                )
            )

    async def _record_anomalies(self, parent: Event, report: DriftReport) -> None:
        for observation in report.anomalies:
            await self._append(
                parent.child(
                    event_type=DRIFT_ANOMALY_DETECTED,
                    category="system",
                    actor_type="system",
                    source_type=MODULE,
                    clock=self._clock,
                    priority="P3",
                    payload=DriftAnomalyPayload(
                        metric=observation.metric,
                        value=observation.value,
                        expected_max=observation.expected_max,
                        classification=observation.classification,
                        window_days=self._policy.drift.window_days,
                    ),
                )
            )

    async def _append(self, event: Event) -> None:
        await asyncio.to_thread(self._events.append, event)


def _numeric(raw: str | None, *, fallback: float | None) -> float | None:
    if raw is None:
        return fallback
    try:
        value = json.loads(raw)
    except ValueError:
        return fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value)
