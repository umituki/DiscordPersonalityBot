"""SCENARIO: the required behaviour examples of spec 34.3.

Each test is one of the ten named scenarios, checked as the spec asks — for
invariants and allowed ranges, not for one exact psychological number
(``.claude/rules/testing.md``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import Application
from app.epistemics.actions import KnowledgeGap, unresolved_gap
from app.social.policy import RelationshipPolicy
from app.social.relationship import CONFLICT_RESIDUE, TRUST, RelationshipEngine
from tests.unit.test_psychology import view_with

pytestmark = pytest.mark.scenario

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def relationship_policy() -> RelationshipPolicy:
    return RelationshipPolicy.load(REPO_ROOT / "config" / "policies" / "relationship.yaml")


# --- 1. conflict → apology → next day ---------------------------------------
async def test_conflict_then_apology_then_the_next_day(
    relationship_policy, clock, make_event
) -> None:
    """An apology eases the residue. It does not give trust back (spec 13.3)."""
    engine = RelationshipEngine(relationship_policy, clock=clock)
    view = view_with(
        relationship__trust=0.7,
        relationship__conflict_residue=0.5,
        relationship__familiarity=0.6,
        relationship__emotional_closeness=0.5,
    )

    apology = make_event(actor_type="user", text="さっきはごめん。言い過ぎた")
    result = await engine.handle(apology, view)
    changes = {p.target_key: p.value for p in result.proposals}

    assert changes.get(CONFLICT_RESIDUE, 0.5) < 0.5
    assert changes.get(TRUST, 0.7) <= 0.7 + 1e-9


# --- 2. secure relationship → several days of absence ------------------------
async def test_a_secure_bond_tolerates_absence(temp_config, clock, make_event) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        for _ in range(4):
            await application.processor.process(make_event(actor_type="user"))
            clock.advance(minutes=20)
        before = application.state.get("attachment", "felt_security")

        clock.advance(days=4)
        await application.processor.process(make_event(actor_type="user"))
        after = application.state.get("attachment", "felt_security")

        assert after is not None
        # Absence is felt, not catastrophic: security does not collapse.
        assert after.numeric > 0.0
        if before is not None:
            assert after.numeric >= before.numeric - 0.3
    finally:
        application.db.close()


# --- 3. one compliment → self and personality stability ----------------------
async def test_one_compliment_moves_nothing_deep(temp_config, clock, make_event) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        await application.processor.process(
            make_event(actor_type="user", text="ゆいはほんとうにやさしいね")
        )

        assert application.state.list_domain("personality") == []
        assert application.state.list_domain("values") == []
        assert len(application.self_model.active_schemas()) <= 1
    finally:
        application.db.close()


# --- 4. repeated cross-context success → gradual adaptation ------------------
async def test_repeated_cross_context_evidence_moves_an_adaptation_not_a_trait(
    temp_config, clock, make_event
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        for _ in range(4):
            for _ in range(3):
                await application.processor.process(make_event(actor_type="user"))
                clock.advance(minutes=30)
            clock.advance(days=8)
            await application.consolidation.run()

        adaptations = application.adaptations.all()
        assert adaptations, "consistent experience must reach the adaptive layer"
        moved = [item for item in adaptations if abs(item.offset) > 0]
        assert moved, "and something must actually have moved"
        # Traits, if they moved at all, moved by a tiny recorded step.
        for row in application.db.query_all("SELECT * FROM personality_history"):
            assert abs(row["new_value"] - row["previous_value"]) <= 0.01 + 1e-9
    finally:
        application.db.close()


# --- 5. unknown current fact → search decision -------------------------------
def test_not_knowing_does_not_automatically_mean_searching(temp_config, clock) -> None:
    """Spec 17.1: unknown is not a trigger, it is an input to a decision."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        gap = KnowledgeGap(
            topic="今日の天気",
            unknown_part="今日は雨が降るのか",
            relevance=0.8,
            curiosity=0.6,
            time_sensitive=True,
        )
        with_user = application.epistemics.select(gap, user_present=True)
        alone = application.epistemics.select(gap, user_present=False)
        trivial = application.epistemics.select(
            KnowledgeGap(topic="どうでもいいこと", relevance=0.02, curiosity=0.0),
            user_present=False,
        )

        # Somebody is right there: asking beats searching.
        assert with_user.action == "ask_user"
        assert alone.action in ("web_search", "infer", "defer", "ignore")
        # And not caring is a legitimate answer to not knowing.
        assert trivial.searches is False
    finally:
        application.db.close()


# --- 6. search failure → honest unresolved state ------------------------------
def test_a_failed_search_leaves_an_honest_gap(temp_config, clock) -> None:
    """Spec 17.3: a failed search is not retried at once, and nothing is invented."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        gap = KnowledgeGap(
            topic="調べたいこと",
            unknown_part="どうなっているのか",
            relevance=0.8,
            curiosity=0.8,
            time_sensitive=True,
        )
        first = application.epistemics.select(gap, user_present=False)
        assert first.action == "web_search"

        retry = application.epistemics.select(
            gap, user_present=False, last_search_failure_at=clock.now()
        )
        assert retry.action != "web_search"

        # The gap stays open and says so, rather than being quietly filled in.
        still_open = unresolved_gap(gap, attempted="web_search")
        assert still_open.unknown_part
        assert still_open.uncertainty >= gap.uncertainty
        assert still_open.likely_in_memory is False
    finally:
        application.db.close()


# --- 7. suppressed memory → archive stays out of normal recall ---------------
async def test_a_suppressed_memory_is_unreachable_but_the_archive_remains(
    temp_config, clock, make_event
) -> None:
    """Spec 10.1 / 30: recall loses it; the objective archive does not."""
    from app import ids
    from app.memory.models import EpisodicMemory

    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        outcome = await application.processor.process(
            make_event(actor_type="user", text="海に行った話")
        )
        episode = application.memories.open_episode(
            conversation_id=None, origin="real_discord", started_at=clock.now()
        )
        memory_id = ids.new_id("mem")
        application.memories.insert_memory(
            EpisodicMemory(
                memory_id=memory_id,
                episode_id=episode.episode_id,
                origin="real_discord",
                summary="海に行った話をした",
                topics=("海",),
                importance=0.6,
                emotional_intensity=0.4,
                accessibility=0.9,
                content_confidence=0.7,
                source_confidence=0.7,
                temporal_confidence=0.7,
                novelty=0.5,
                prediction_error=0.1,
                occurred_at=clock.now(),
                created_at=clock.now(),
                updated_at=clock.now(),
                last_decayed_at=clock.now(),
                source_event_ids=(outcome.event.event_id,),
            )
        )
        assert (await application.memory.recall("海")).selected

        application.admin.from_conversation(memory_id, actor="owner")

        assert (await application.memory.recall("海")).selected == ()
        # The events that made it are still in the archive, untouched.
        assert application.event_store.count() > 0
    finally:
        application.db.close()


# --- 8. old knowledge → later correction --------------------------------------
def test_knowledge_can_be_corrected_without_erasing_what_was_believed(
    temp_config, clock
) -> None:
    from datetime import datetime, timezone

    from app.knowledge.builder import Candidate

    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        item = application.knowledge_builder.register(
            Candidate(
                statement="当時はそう言われていたこと",
                coverage_class="historical_cultural",
                available_from=datetime(2005, 1, 1, tzinfo=timezone.utc),
                topic="科学",
            )
        )
        application.knowledge_builder.revise(
            item.knowledge_id,
            statement="のちに訂正された内容",
            truth_confidence=0.9,
            reason="later correction",
        )

        from app.storage.repositories.knowledge import KnowledgeRepository

        versions = KnowledgeRepository(application.db).versions(item.knowledge_id)
        assert len(versions) == 2
        assert versions[0]["statement"] == "当時はそう言われていたこと"
        assert versions[1]["statement"] == "のちに訂正された内容"
    finally:
        application.db.close()


# --- 9. one short USER message → no trait overgeneralisation -------------------
async def test_a_single_short_message_does_not_define_the_user(
    temp_config, clock, make_event
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        await application.processor.process(make_event(actor_type="user", text="うん"))

        keys = {value.key for value in application.state.list_domain("user_model")}
        # Spec 14: how the USER seems right now may be recorded. What kind of
        # person they are may not, on the strength of one short message.
        assert not {key for key in keys if key.startswith("trait_")}
        assert keys <= {"provisional"} | {key for key in keys if key.startswith("state_")}
        assert application.state.list_domain("personality") == []
    finally:
        application.db.close()


# --- 10. same event delivered twice → idempotent result -----------------------
async def test_the_same_event_delivered_twice_is_idempotent(
    temp_config, clock, make_event
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        event = make_event(actor_type="user")
        await application.processor.process(event)
        changes = application.state.change_count()
        snapshot = {
            f"{value.domain}.{value.key}": value.value
            for value in application.state.list_all()
        }

        second = await application.processor.process(event)

        assert second.newly_stored is False
        assert application.state.change_count() == changes
        assert {
            f"{value.domain}.{value.key}": value.value
            for value in application.state.list_all()
        } == snapshot
    finally:
        application.db.close()


# --- 34.4: a long run neither explodes nor freezes ----------------------------
async def test_a_long_run_neither_runs_away_nor_freezes(
    temp_config, clock, make_event
) -> None:
    """Spec 34.2-9/10 and 41: no state converges to an extreme, and not nothing changes."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        start = {
            f"{value.domain}.{value.key}": value.numeric
            for value in application.state.list_all()
        }
        for cycle in range(10):
            for _ in range(3):
                await application.processor.process(make_event(actor_type="user"))
                clock.advance(minutes=45)
            clock.advance(days=7)
            await application.consolidation.run()

        values = application.state.list_all()
        assert values

        extreme = [
            f"{value.domain}.{value.key}"
            for value in values
            if value.numeric is not None
            and value.domain not in ("user_model_counters",)
            and (value.numeric <= 0.001 or value.numeric >= 0.999)
        ]
        # A few keys legitimately sit at a bound (loneliness relieved to zero);
        # what must not happen is everything piling up at the edges.
        assert len(extreme) < len(values) / 2, extreme

        moved = [
            key
            for key, before in start.items()
            for value in values
            if f"{value.domain}.{value.key}" == key
            and before is not None
            and value.numeric is not None
            and abs(value.numeric - before) > 1e-6
        ]
        assert moved or len(values) > len(start), "a decade of life must change something"

        # And the drift monitor saw nothing impossible.
        assert application.drift.open_anomalies() == []
    finally:
        application.db.close()
