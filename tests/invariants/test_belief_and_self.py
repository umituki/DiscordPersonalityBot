"""INVARIANT: beliefs come from evidence; the self-concept lags behind behaviour.

* Spec 25 — evidence is kept for *and* against, and one primary source counts
  once however often it is repeated.
* Spec 12.4 — the self model is separate from personality and may be wrong.
* Spec 23.2 — a self schema changes after the behaviour it describes, not with
  it, and mixed evidence costs clarity rather than flipping the schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.social.belief_policy import BeliefSelfPolicy
from app.social.beliefs import BeliefEngine
from app.social.self_model import CONTRADICTS, SUPPORTS, SelfEngine
from app.storage.repositories.social import BeliefRepository, SelfRepository

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
STATEMENT = "ユーザーは朝が苦手だ"
SUBJECT = "user"


@pytest.fixture
def belief_self_policy() -> BeliefSelfPolicy:
    return BeliefSelfPolicy.load(REPO_ROOT / "config" / "policies" / "belief_self.yaml")


@pytest.fixture
def beliefs(db, belief_self_policy, clock) -> BeliefEngine:
    return BeliefEngine(BeliefRepository(db), belief_self_policy.belief, clock=clock)


@pytest.fixture
def self_engine(db, belief_self_policy, clock) -> SelfEngine:
    return SelfEngine(SelfRepository(db), belief_self_policy.self_schema, clock=clock)


def support(engine, source_id: str, **kwargs):
    return engine.record_evidence(
        statement=STATEMENT,
        subject=SUBJECT,
        stance="supports",
        source_type="real_user_message",
        primary_source_id=source_id,
        **kwargs,
    )


def contradict(engine, source_id: str, **kwargs):
    return engine.record_evidence(
        statement=STATEMENT,
        subject=SUBJECT,
        stance="contradicts",
        source_type="real_user_message",
        primary_source_id=source_id,
        **kwargs,
    )


# --- beliefs ----------------------------------------------------------------


def test_a_belief_is_derived_from_evidence_not_set(beliefs, belief_self_policy) -> None:
    first = support(beliefs, "msg_1")

    assert first.belief.support_weight > 0
    assert first.belief.confidence > 0
    # One piece of evidence never produces certainty.
    assert first.belief.confidence < belief_self_policy.belief.max_confidence


def test_the_same_primary_source_counts_once(beliefs) -> None:
    """Spec 25: a repost is not independent evidence."""
    first = support(beliefs, "msg_1")
    repeats = [support(beliefs, "msg_1") for _ in range(4)]

    assert first.evidence_counted is True
    assert all(update.evidence_counted is False for update in repeats)
    assert repeats[-1].reason == "duplicate_primary_source"
    assert len(beliefs.evidence_for(first.belief.belief_id)) == 1
    assert repeats[-1].belief.confidence == first.belief.confidence


def test_independent_sources_do_accumulate(beliefs) -> None:
    weak = support(beliefs, "msg_1").belief.confidence
    for index in range(2, 6):
        support(beliefs, f"msg_{index}")
    strong = beliefs.belief_about(STATEMENT, SUBJECT, "real_discord")

    assert strong.confidence > weak
    assert len(beliefs.evidence_for(strong.belief_id)) == 5


def test_contradicting_evidence_is_kept_not_discarded(beliefs) -> None:
    """Spec 25: evidence for and against are both retained."""
    for index in range(3):
        support(beliefs, f"pro_{index}")
    before = beliefs.belief_about(STATEMENT, SUBJECT, "real_discord").confidence

    contradict(beliefs, "con_1")
    after = beliefs.belief_about(STATEMENT, SUBJECT, "real_discord")

    assert after.confidence < before
    assert after.contradiction_weight > 0
    assert after.support_weight > 0
    stances = {item.stance for item in beliefs.evidence_for(after.belief_id)}
    assert stances == {"supports", "contradicts"}


def test_balanced_evidence_makes_a_belief_contested(beliefs) -> None:
    for index in range(3):
        support(beliefs, f"pro_{index}")
        contradict(beliefs, f"con_{index}")

    belief = beliefs.belief_about(STATEMENT, SUBJECT, "real_discord")
    assert belief.is_contested
    assert belief.status != "abandoned"


def test_an_abandoned_belief_is_not_deleted(beliefs) -> None:
    support(beliefs, "pro_1")
    for index in range(12):
        contradict(beliefs, f"con_{index}")

    belief = beliefs.belief_about(STATEMENT, SUBJECT, "real_discord")
    assert belief.status == "abandoned"
    assert belief.confidence < 0.2
    # Still on disk, with its evidence, in case something revives it.
    assert len(beliefs.evidence_for(belief.belief_id)) == 13
    assert beliefs.held_beliefs() == []


def test_beliefs_of_different_origins_stay_separate(beliefs) -> None:
    """Spec 2.10: a simulated-past belief is not a real-history belief."""
    real = support(beliefs, "msg_1").belief
    simulated = beliefs.record_evidence(
        statement=STATEMENT,
        subject=SUBJECT,
        stance="supports",
        source_type="simulated_past",
        primary_source_id="sim_1",
        origin="simulated_past",
    ).belief

    assert real.belief_id != simulated.belief_id
    assert simulated.origin == "simulated_past"


# --- self model -------------------------------------------------------------


def test_the_self_concept_lags_behind_behaviour(
    self_engine, belief_self_policy
) -> None:
    """Spec 23.2: acting differently does not immediately change self-image."""
    required = belief_self_policy.self_schema.evidence_before_change
    start = belief_self_policy.self_schema.initial_strength

    lagging = [
        self_engine.observe_behaviour(
            name="social_confidence",
            statement="人と話すのは得意ではない",
            stance=SUPPORTS,
            event_id=f"evt_{index}",
        )
        for index in range(int(required) - 1)
    ]

    assert all(update.lagged for update in lagging)
    assert self_engine.schema("social_confidence").strength == pytest.approx(start)

    caught_up = self_engine.observe_behaviour(
        name="social_confidence",
        statement="人と話すのは得意ではない",
        stance=SUPPORTS,
        event_id="evt_final",
    )
    assert caught_up.strength_changed
    assert caught_up.schema.strength > start


def test_the_self_concept_may_disagree_with_behaviour(self_engine) -> None:
    """Spec 12.4: YUI's self schema is allowed to be wrong."""
    for index in range(20):
        self_engine.observe_behaviour(
            name="social_confidence",
            statement="人と話すのは得意ではない",
            stance=CONTRADICTS,
            event_id=f"evt_{index}",
        )

    schema = self_engine.schema("social_confidence")
    # Twenty contradicting observations lower it, but nothing forces the
    # schema to match the behaviour outright.
    assert schema.strength > 0.0
    assert schema.contradicting_count == 20


def test_mixed_evidence_lowers_clarity_not_the_schema(self_engine) -> None:
    for index in range(6):
        self_engine.observe_behaviour(
            name="independence",
            statement="ひとりでも平気だと思う",
            stance=SUPPORTS if index % 2 == 0 else CONTRADICTS,
            event_id=f"evt_{index}",
        )

    schema = self_engine.schema("independence")
    assert schema.clarity < 0.5
    assert schema.strength == pytest.approx(0.5)  # pushed both ways, went nowhere


def test_consistent_evidence_raises_clarity(self_engine) -> None:
    for index in range(6):
        self_engine.observe_behaviour(
            name="curiosity",
            statement="知らないことを知りたいと思う",
            stance=SUPPORTS,
            event_id=f"evt_{index}",
        )

    schema = self_engine.schema("curiosity")
    assert schema.clarity > 0.5
    assert self_engine.self_concept_clarity() == schema.clarity


def test_behaviour_is_linked_to_the_schema_it_bears_on(self_engine) -> None:
    """Spec 12.4 keeps self_event_connections for exactly this."""
    self_engine.observe_behaviour(
        name="curiosity",
        statement="知らないことを知りたいと思う",
        stance=SUPPORTS,
        event_id="evt_1",
    )
    self_engine.observe_behaviour(
        name="curiosity",
        statement="知らないことを知りたいと思う",
        stance=SUPPORTS,
        event_id="evt_1",
    )

    connections = self_engine.connections("curiosity")
    assert len(connections) == 1  # the same event is linked once
    assert connections[0]["relation"] == SUPPORTS


def test_possible_selves_are_kept_apart_from_the_current_self(self_engine) -> None:
    self_engine.observe_behaviour(
        name="curiosity", statement="知りたいと思う", stance=SUPPORTS, event_id="evt_1"
    )
    self_engine.imagine(
        name="someone_who_travels",
        statement="いつか遠くへ行ってみたい",
        valence="hoped",
        salience=0.4,
    )

    current = {schema.name for schema in self_engine.active_schemas()}
    possible = {item.name for item in self_engine.possible_selves()}

    assert current == {"curiosity"}
    assert possible == {"someone_who_travels"}
    assert current & possible == set()


def test_self_model_state_survives_a_restart(temp_config, clock) -> None:
    from app.bootstrap import Application

    first = Application.build(temp_config, clock=clock, configure_logs=False)
    first.self_model.observe_behaviour(
        name="curiosity", statement="知りたいと思う", stance=SUPPORTS, event_id="evt_1"
    )
    first.beliefs.record_evidence(
        statement=STATEMENT,
        subject=SUBJECT,
        stance="supports",
        source_type="real_user_message",
        primary_source_id="msg_1",
    )
    first.db.close()

    second = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        assert second.self_model.schema("curiosity") is not None
        assert second.beliefs.belief_about(STATEMENT, SUBJECT, "real_discord") is not None
    finally:
        second.db.close()
