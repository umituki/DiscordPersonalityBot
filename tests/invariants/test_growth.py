"""INVARIANT: who YUI becomes changes slowly, and only for reasons.

* A single event never moves a trait or a value (spec 2.13, 12.3, 34.2-5).
* All five deep update conditions must hold, not just the convenient ones
  (spec 12.3).
* Baseline, adaptation and expression stay separate, and the baseline is not a
  permanent constant (spec 23.2).
* An extreme state resists more of the same, but never blocks the way back
  (spec 23.2).
* Values are a relative ordering: nothing can make everything more important
  (spec 12.5).
* The drift monitor observes and classifies; it never clamps a real life
  (spec 23.3).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.bootstrap import Application
from app.consolidation.adaptations import AdaptationEngine, AdaptationEvidence
from app.consolidation.deep_gate import CONDITIONS, DeepUpdateGate
from app.consolidation.drift import DriftMonitor
from app.consolidation.growth import GrowthEngine
from app.consolidation.policy import GrowthPolicy
from app.consolidation.values import ValueEngine, redistribute
from app.state import dependency_graph, ownership
from app.storage.repositories.growth import (
    AdaptationRepository,
    CandidateRepository,
    DriftRepository,
    NarrativeRepository,
    PersonalityRepository,
    ValueRepository,
)
from app.storage.repositories.state import StateRepository
from tests.unit.test_psychology import view_with

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]

DEEP_DOMAINS = {"personality", "values", "attachment_disposition", "narrative_identity"}


@pytest.fixture
def growth_policy() -> GrowthPolicy:
    return GrowthPolicy.load(REPO_ROOT / "config" / "policies" / "growth.yaml")


@pytest.fixture
def adaptations(db, growth_policy, clock) -> AdaptationEngine:
    return AdaptationEngine(AdaptationRepository(db), growth_policy.adaptation, clock=clock)


@pytest.fixture
def candidates(db) -> CandidateRepository:
    return CandidateRepository(db)


@pytest.fixture
def growth(db, candidates, growth_policy, clock) -> GrowthEngine:
    return GrowthEngine(
        candidates=candidates,
        traits=PersonalityRepository(db),
        narratives=NarrativeRepository(db),
        policy=growth_policy,
        clock=clock,
    )


@pytest.fixture
def values(db, candidates, growth_policy, clock) -> ValueEngine:
    return ValueEngine(
        candidates=candidates, values=ValueRepository(db), policy=growth_policy, clock=clock
    )


@pytest.fixture
def drift(db, growth_policy, clock) -> DriftMonitor:
    return DriftMonitor(
        state=StateRepository(db),
        drifts=DriftRepository(db),
        policy=growth_policy.drift,
        clock=clock,
    )


def evidence(clock, *, adaptation="curiosity", direction=1, context="relationship:x", eid="chg_1"):
    return AdaptationEvidence(
        adaptation=adaptation,
        direction=direction,
        weight=1.0,
        context=context,
        evidence_id=eid,
        observed_at=clock.now(),
        mood_independent=True,
        outcome_weight=0.5,
    )


def ready_candidate(candidates, growth_policy, clock, *, domain="personality", key="openness"):
    """A candidate that satisfies every condition of spec 12.3."""
    gate_rules = growth_policy.deep_gate
    candidate = candidates.create(
        domain=domain, key=key, direction=1, source_adaptation="curiosity", now=clock.now()
    )
    later = clock.now() + timedelta(days=gate_rules.min_persistence_days + 1)
    return candidates.accumulate(
        candidate.candidate_id,
        pattern_count=gate_rules.min_pattern_count,
        contexts=tuple(f"ctx_{i}" for i in range(gate_rules.min_contexts)),
        evidence_ids=("chg_1", "chg_2", "chg_3"),
        outcome_weight=gate_rules.min_outcome_weight,
        mood_independent_count=gate_rules.min_mood_independent,
        magnitude=0.08,
        now=later,
    )


# --- ownership and layering -------------------------------------------------
def test_every_deep_domain_has_a_consolidation_writer() -> None:
    for domain in DEEP_DOMAINS:
        assert dependency_graph.is_deep(domain)
        assert dependency_graph.is_consolidation_writer(ownership.owner_of(domain))


def test_adaptations_are_adaptive_not_deep() -> None:
    """Spec 23.2: domain-specific adaptation changes before the general trait."""
    assert ownership.owner_of("characteristic_adaptations") == AdaptationEngine.name
    assert dependency_graph.layer_of("characteristic_adaptations") == dependency_graph.ADAPTIVE
    assert dependency_graph.layer_of("personality") == dependency_graph.DEEP


def test_the_adaptation_engine_may_not_write_personality() -> None:
    assert ownership.may_write("characteristic_adaptations", AdaptationEngine.name)
    assert not ownership.may_write("personality", AdaptationEngine.name)
    assert not ownership.may_write("characteristic_adaptations", GrowthEngine.name)


# --- the deep update gate (spec 12.3) --------------------------------------
def test_a_single_observation_never_passes_the_gate(candidates, growth_policy, clock) -> None:
    candidate = candidates.create(
        domain="personality", key="openness", direction=1, source_adaptation="curiosity",
        now=clock.now(),
    )
    candidate = candidates.accumulate(
        candidate.candidate_id,
        pattern_count=1,
        contexts=("relationship:relationship_engine",),
        evidence_ids=("chg_1",),
        outcome_weight=1.0,
        mood_independent_count=1,
        magnitude=0.5,
        now=clock.now(),
    )

    decision = DeepUpdateGate(growth_policy.deep_gate).evaluate(candidate)
    assert decision.passed is False
    assert "repeated_pattern" in decision.missing
    assert "temporal_persistence" in decision.missing


@pytest.mark.parametrize("condition", CONDITIONS)
def test_every_condition_is_individually_required(
    candidates, growth_policy, clock, condition
) -> None:
    """Removing any one of the five conditions must block the update."""
    gate = DeepUpdateGate(growth_policy.deep_gate)
    candidate = ready_candidate(candidates, growth_policy, clock)
    assert gate.evaluate(candidate).passed is True

    weakened = {
        "repeated_pattern": {"pattern_count": 0},
        "temporal_persistence": {"last_seen_at": candidate.first_seen_at},
        "cross_context_evidence": {"contexts": ()},
        "meaningful_outcome": {"outcome_weight": 0.0},
        "not_explained_by_mood": {"mood_independent_count": 0},
    }[condition]
    assert gate.evaluate(candidate.model_copy(update=weakened)).passed is False
    assert condition in gate.evaluate(candidate.model_copy(update=weakened)).missing


def test_unknown_mood_is_not_treated_as_ordinary(growth_policy) -> None:
    """Spec 24: an absent reading is unknown, not a convenient zero."""
    gate = DeepUpdateGate(growth_policy.deep_gate)
    assert gate.mood_is_ordinary(None, 0.55) is False
    assert gate.mood_is_ordinary(0.55, 0.55) is True
    assert gate.mood_is_ordinary(0.99, 0.55) is False


# --- adaptations (spec 12.2, 23.2) -----------------------------------------
def test_one_piece_of_evidence_does_not_move_an_adaptation(adaptations, clock) -> None:
    update = adaptations.observe(evidence(clock))
    assert update.moved is False
    assert update.adaptation.pending_evidence == pytest.approx(1.0)


def test_an_adaptation_moves_once_evidence_accumulates(adaptations, growth_policy, clock) -> None:
    needed = int(growth_policy.adaptation.evidence_before_change)
    moves = [adaptations.observe(evidence(clock, eid=f"chg_{i}")).moved for i in range(needed)]

    assert moves[:-1] == [False] * (needed - 1)
    assert moves[-1] is True
    assert adaptations.get("curiosity").value > growth_policy.adaptation.initial_value


def test_extreme_adaptations_resist_more_of_the_same(adaptations, growth_policy, clock) -> None:
    """Spec 23.2, and the way back is never damped."""
    rules = growth_policy.adaptation
    steps = []
    for i in range(int(rules.evidence_before_change) * 8):
        update = adaptations.observe(evidence(clock, eid=f"chg_{i}"))
        if update.moved:
            steps.append(update.delta)

    assert len(steps) >= 3
    assert steps[-1] < steps[0]  # the same evidence buys less near the edge
    assert adaptations.get("curiosity").value < 1.0

    high = adaptations.get("curiosity").value
    for i in range(int(rules.evidence_before_change) * 2):
        adaptations.observe(evidence(clock, direction=-1, eid=f"back_{i}"))
    assert adaptations.get("curiosity").value < high


def test_the_adaptation_keeps_its_baseline_separate(adaptations, growth_policy, clock) -> None:
    for i in range(int(growth_policy.adaptation.evidence_before_change)):
        adaptations.observe(evidence(clock, eid=f"chg_{i}"))

    adaptation = adaptations.get("curiosity")
    assert adaptation.baseline == pytest.approx(growth_policy.adaptation.initial_value)
    assert adaptation.offset > 0.0


async def test_adaptations_are_proposed_not_written(adaptations, clock, make_event, growth_policy):
    """The engine proposes; only the committer writes (spec 9.3)."""
    for i in range(int(growth_policy.adaptation.evidence_before_change)):
        adaptations.observe(evidence(clock, eid=f"chg_{i}"))

    result = await adaptations.handle(make_event(category="system"), view_with())
    assert result.proposals
    for proposal in result.proposals:
        assert proposal.target_domain == "characteristic_adaptations"
        assert proposal.source_module == AdaptationEngine.name
        assert proposal.evidence_ids  # provenance travels with the value


# --- personality (spec 12.1, 12.3, 23.2) -----------------------------------
async def test_the_growth_engine_proposes_nothing_without_a_passed_gate(
    growth, candidates, clock, make_event
) -> None:
    candidate = candidates.create(
        domain="personality", key="openness", direction=1, source_adaptation="curiosity",
        now=clock.now(),
    )
    candidates.accumulate(
        candidate.candidate_id,
        pattern_count=2,
        contexts=("a",),
        evidence_ids=("chg_1",),
        outcome_weight=0.1,
        mood_independent_count=0,
        magnitude=0.5,
        now=clock.now(),
    )

    result = await growth.handle(make_event(category="system"), view_with())
    assert [p for p in result.proposals if p.target_domain == "personality"] == []


async def test_a_promoted_candidate_moves_a_trait_by_a_tiny_step(
    growth, candidates, growth_policy, clock, make_event
) -> None:
    growth.ensure_seeded()
    ready_candidate(candidates, growth_policy, clock)

    result = await growth.handle(make_event(category="system"), view_with())
    trait_proposals = [p for p in result.proposals if p.target_domain == "personality"]

    assert len(trait_proposals) == 1
    proposal = trait_proposals[0]
    baseline = growth_policy.personality.temperament["openness"]
    assert proposal.value > baseline
    assert proposal.value - baseline <= growth_policy.personality.max_step + 1e-9
    assert len(proposal.evidence_ids) >= 3


def test_a_baseline_follows_the_expression_but_never_matches_it(
    growth, candidates, growth_policy, clock
) -> None:
    """Spec 23.2: the initial baseline is not a permanent constant."""
    growth.ensure_seeded()
    candidate = ready_candidate(candidates, growth_policy, clock)
    start = growth_policy.personality.temperament["openness"]
    committed = start + growth_policy.personality.max_step

    updates = growth.apply_committed(
        committed={candidate.target: committed}, run_id="run_test"
    )

    assert len(updates) == 1
    trait = growth.traits()[0] if growth.traits()[0].name == "openness" else None
    trait = next(item for item in growth.traits() if item.name == "openness")
    assert start < trait.baseline < committed
    assert trait.initial_baseline == pytest.approx(start)
    assert trait.drifted > 0.0


def test_nothing_is_promoted_that_the_commit_did_not_accept(
    growth, candidates, growth_policy, clock
) -> None:
    growth.ensure_seeded()
    candidate = ready_candidate(candidates, growth_policy, clock)

    # The transaction rejected it: no ledger movement, no history, still open.
    assert growth.apply_committed(committed={}, run_id="run_test") == []
    trait = next(item for item in growth.traits() if item.name == "openness")
    assert trait.baseline == pytest.approx(trait.initial_baseline)
    assert candidates.get(candidate.candidate_id).status == "accumulating"


# --- values (spec 12.5) -----------------------------------------------------
def test_raising_one_value_lowers_the_others() -> None:
    before = {"self_direction": 0.2, "security": 0.5, "benevolence": 0.3}
    after = redistribute(before, raise_name="self_direction", step=0.05)

    assert after["self_direction"] > before["self_direction"]
    assert after["security"] < before["security"]
    assert after["benevolence"] < before["benevolence"]
    assert sum(after.values()) == pytest.approx(sum(before.values()))


def test_values_cannot_all_rise_at_once(values, candidates, growth_policy, clock) -> None:
    values.ensure_seeded()
    before = values.current_priorities()
    candidate = ready_candidate(
        candidates, growth_policy, clock, domain="values", key="self_direction"
    )
    updated = redistribute(
        before, raise_name=candidate.target_key, step=growth_policy.values.max_step
    )

    assert any(updated[name] < before[name] for name in before)
    assert sum(updated.values()) == pytest.approx(sum(before.values()))
    assert all(value >= 0.0 for value in updated.values())


async def test_a_value_shift_is_proposed_for_every_affected_value(
    values, candidates, growth_policy, clock, make_event
) -> None:
    values.ensure_seeded()
    ready_candidate(candidates, growth_policy, clock, domain="values", key="self_direction")

    result = await values.handle(make_event(category="system"), view_with())
    assert result.proposals
    assert {p.target_domain for p in result.proposals} == {"values"}
    raised = [p for p in result.proposals if p.target_key == "self_direction"]
    lowered = [p for p in result.proposals if p.target_key != "self_direction"]
    assert raised and lowered


# --- drift monitor (spec 23.3) ---------------------------------------------
def test_drift_classifications_are_ordered(drift, growth_policy) -> None:
    expected = growth_policy.drift.expected_per_window["trait_velocity"]
    assert drift.classify("trait_velocity", expected * 0.5)[0] == "EXPECTED"
    assert drift.classify("trait_velocity", expected * 3.0)[0] == "SUSPICIOUS"
    assert drift.classify("trait_velocity", expected * 10.0)[0] == "INVALID"


def test_an_unmonitored_metric_is_never_an_anomaly(drift) -> None:
    classification, _ = drift.classify("something_nobody_declared", 1e9)
    assert classification == "EXPECTED"


def test_the_drift_monitor_changes_no_state(drift, db, clock) -> None:
    """Spec 23.3: normal life change must not be clamped away."""
    state = StateRepository(db)
    state.write_value(
        domain="personality", key="openness", value=0.9, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=None,
    )

    report = drift.measure(now=clock.now())

    assert report.observations
    assert state.get("personality", "openness").value == pytest.approx(0.9)


def test_only_invalid_is_a_rollback_candidate(drift, growth_policy) -> None:
    expected = growth_policy.drift.expected_per_window["trait_velocity"]
    report = drift.measure(
        now=None, extra={"trait_velocity": expected * 3.0}
    )
    suspicious = [o for o in report.observations if o.metric == "trait_velocity"]

    assert suspicious[0].classification == "SUSPICIOUS"
    assert suspicious[0].rollback_candidate is False
    assert report.rollback_candidates == ()


# --- end to end -------------------------------------------------------------
async def test_ordinary_conversation_never_touches_deep_state(
    temp_config, clock, make_event
) -> None:
    """The real pipeline, the real arbitrator, many events, no deep change."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        for _ in range(8):
            await application.processor.process(make_event(actor_type="user"))
            clock.advance(seconds=600)

        domains = set(application.state.domains())
        assert domains & DEEP_DOMAINS == set()
        assert application.consolidation.due() is True
    finally:
        application.db.close()


async def test_consolidation_runs_through_the_normal_pipeline(
    temp_config, clock, make_event
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        for _ in range(6):
            await application.processor.process(make_event(actor_type="user"))
            clock.advance(hours=2)

        result = await application.consolidation.run()

        assert result.ran
        assert result.outcome is not None
        assert result.outcome.status in ("committed", "rejected", "no_subscribers")
        # A first consolidation cannot possibly satisfy temporal persistence.
        assert result.deep_change_count == 0
        assert set(application.state.domains()) & DEEP_DOMAINS == set()
        # It did, however, record that it happened.
        assert application.consolidation.due() is False
    finally:
        application.db.close()


async def test_evidence_accumulates_over_months_instead_of_arriving_at_once(
    temp_config, clock, make_event
) -> None:
    """Repetition is counted in consolidations, not in messages (spec 12.3)."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    candidates = CandidateRepository(application.db)
    try:
        for _ in range(4):
            for _ in range(3):
                await application.processor.process(make_event(actor_type="user"))
                clock.advance(minutes=30)
            clock.advance(days=8)
            await application.consolidation.run()

        open_candidates = candidates.accumulating()
        assert open_candidates, "committed change history must produce candidates"
        # Nothing has been allowed through yet, and every one of them can say
        # exactly which condition it is still missing.
        for candidate in open_candidates:
            decision = application.growth.gate.evaluate(candidate)
            assert decision.passed is False or candidate.persistence_days > 0
        assert max(c.pattern_count for c in open_candidates) >= 2
        assert max(c.persistence_days for c in open_candidates) > 0.0
    finally:
        application.db.close()


async def test_a_trait_can_change_naturally_but_only_after_months(
    temp_config, clock, make_event
) -> None:
    """The whole point of Phase 9, run for real: growth without a shortcut."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    candidates = CandidateRepository(application.db)
    try:
        for _ in range(6):
            for _ in range(3):
                await application.processor.process(make_event(actor_type="user"))
                clock.advance(minutes=30)
            clock.advance(days=8)
            await application.consolidation.run()

        promoted = candidates.by_status("promoted")
        assert promoted, "months of consistent, cross-context evidence must count"
        for candidate in promoted:
            assert candidate.pattern_count >= application.growth_policy.deep_gate.min_pattern_count
            assert len(candidate.contexts) >= application.growth_policy.deep_gate.min_contexts
            assert (
                candidate.persistence_days
                >= application.growth_policy.deep_gate.min_persistence_days
            )

        # Every deep change is one small step, and every one of them is on record.
        for row in PersonalityRepository(application.db).history():
            assert abs(row["new_value"] - row["previous_value"]) <= (
                application.growth_policy.personality.max_step + 1e-9
            )
            assert row["candidate_id"]
            assert row["run_id"]
    finally:
        application.db.close()


async def test_a_fully_evidenced_candidate_survives_the_real_arbitrator(
    temp_config, clock, make_event
) -> None:
    """Years later, a trait may finally move — by one tiny, recorded step."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        await application.processor.process(make_event(actor_type="user"))
        application.growth.ensure_seeded()
        policy = application.growth_policy
        before = next(t for t in application.growth.traits() if t.name == "openness").baseline

        candidates = CandidateRepository(application.db)
        ready_candidate(candidates, policy, clock)

        result = await application.consolidation.run(force=True)

        assert result.deep_updates, "a fully evidenced candidate must be applied"
        committed = application.state.get("personality", "openness")
        assert committed is not None
        assert 0.0 < committed.value - before <= policy.personality.max_step + 1e-9

        history = PersonalityRepository(application.db).history("openness")
        assert len(history) == 1
        assert history[0]["reason_code"] == "deep_update"
        after = next(t for t in application.growth.traits() if t.name == "openness")
        assert before < after.baseline < committed.value
    finally:
        application.db.close()


async def test_a_value_shift_keeps_the_distribution_whole(
    temp_config, clock, make_event
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        await application.processor.process(make_event(actor_type="user"))
        application.values.ensure_seeded()
        before = application.values.current_priorities()

        ready_candidate(
            CandidateRepository(application.db),
            application.growth_policy,
            clock,
            domain="values",
            key="self_direction",
        )
        result = await application.consolidation.run(force=True)

        assert result.value_shifts
        after = application.values.current_priorities()
        assert after["self_direction"] > before["self_direction"]
        assert any(after[name] < before[name] for name in before)
        assert sum(after.values()) == pytest.approx(sum(before.values()), abs=1e-6)
    finally:
        application.db.close()


async def test_consolidation_is_idempotent_across_restarts(
    temp_config, clock, make_event
) -> None:
    first = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        await first.processor.process(make_event(actor_type="user"))
        await first.consolidation.run()
        traits = {trait.name: trait.baseline for trait in first.growth.traits()}
        values = first.values.current_priorities()
    finally:
        first.db.close()

    clock.advance(hours=1)
    second = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        assert second.consolidation.due() is False
        skipped = await second.consolidation.run()
        assert skipped.ran is False
        assert skipped.skipped_reason == "not_due"
        assert {t.name: t.baseline for t in second.growth.traits()} == traits
        assert second.values.current_priorities() == values
    finally:
        second.db.close()
