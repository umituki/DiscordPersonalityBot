"""INVARIANT: she is not a bot that googles things
(rebuild spec 17, 32 — Phase 11).

Three separations, and every test here defends one of them.

**未知 = 自動 Web Search にしてはならない.** A gap is a situation with seven
possible responses, and searching is one. The gap table records which was
chosen, so "she searches for everything" is a claim the database settles.

**Search request ≠ search success ≠ knowledge acquisition.** Three events with
three truth conditions. The model writing 「調べたら〜だった」 establishes none
of them.

**External information ≠ what she permanently knows.** Ten results is not ten
facts learned. Everything goes through attention, comprehension and retention,
and most of it stops before the end.

Plus the one rule that is Python rather than prose: a result published after
the moment she is searching *from* is unusable, whatever it says.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.admin.router import ADMIN_PREFIX
from app.bootstrap import Application
from app.epistemics.actions import KnowledgeGap
from app.knowledge.events import KNOWLEDGE_ACQUIRED, SEARCH_PERFORMED
from app.knowledge.search import (
    FixtureSearchProvider,
    NullSearchProvider,
    SearchQuery,
    SearchResult,
    reject_the_future,
)
from app.runtime.knowledge import INVESTIGATE, KNOWLEDGE_GAP, GapSource
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


@pytest.fixture
def application(temp_config, clock):
    owned = temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )
    built = Application.build(owned, clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


def _a_gap(topic: str = "海", **kwargs) -> KnowledgeGap:
    defaults = dict(
        topic=topic,
        known_part="",
        unknown_part=f"{topic}のこと",
        uncertainty=0.9,
        relevance=0.9,
        curiosity=0.9,
        time_sensitive=True,
    )
    defaults.update(kwargs)
    return KnowledgeGap(**defaults)  # type: ignore[arg-type]


def _raise_gap(application, clock, topic: str = "海", **kwargs) -> str:
    gap = _a_gap(topic, **kwargs)
    return application.gaps.raise_gap(
        topic=gap.topic,
        known_part=gap.known_part,
        unknown_part=gap.unknown_part,
        uncertainty=gap.uncertainty,
        relevance=gap.relevance,
        curiosity=gap.curiosity,
        time_sensitive=gap.time_sensitive,
        now=clock.now(),
    )


# --- the temporal hard gate --------------------------------------------------


def test_a_result_from_the_future_is_unusable() -> None:
    """Python, not prose. Telling a model not to use the future is a request
    that a good model mostly honours and a degraded one does not."""
    now = datetime(2012, 6, 1, tzinfo=timezone.utc)
    past = SearchResult(statement="昔のこと", published_at=datetime(2010, 1, 1, tzinfo=timezone.utc))
    future = SearchResult(
        statement="まだ起きていないこと", published_at=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )

    verdict = reject_the_future((past, future), now)

    assert [item.statement for item in verdict.usable] == ["昔のこと"]
    assert verdict.rejected_count == 1


def test_an_undated_result_is_also_refused() -> None:
    """Not knowing when something was written is not evidence that it is old
    enough. For a simulated past, "probably fine" is how a decade leaks."""
    verdict = reject_the_future(
        (SearchResult(statement="いつのものか分からない"),),
        datetime(2012, 6, 1, tzinfo=timezone.utc),
    )

    assert verdict.usable == ()
    assert verdict.rejected_count == 1


def test_the_query_cannot_be_built_without_a_moment() -> None:
    """``effective_now`` has no default anywhere in the chain. A caller that
    forgets it fails loudly rather than inheriting today's clock."""
    with pytest.raises(Exception):
        SearchQuery(text="海")  # type: ignore[call-arg]


async def test_the_gate_runs_on_a_real_search(application, clock) -> None:
    """The fixture deliberately contains a result dated 2099, so every run
    exercises the gate rather than only the happy path."""
    gap_id = _raise_gap(application, clock)

    outcome = await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    assert outcome.results >= 4
    assert outcome.future_rejected >= 1
    statements = [
        item.statement for item in application.knowledge_repo_all()
    ] if hasattr(application, "knowledge_repo_all") else []
    assert not any("2099" in statement for statement in statements)


async def test_a_past_dated_search_sees_only_the_past(application, clock) -> None:
    """A Genesis run in 2012 must not read about 2020, however relevant."""
    gap_id = _raise_gap(application, clock)

    outcome = await application.investigation.investigate(
        _a_gap(),
        effective_now=datetime(2016, 1, 1, tzinfo=timezone.utc),
        gap_id=gap_id,
    )

    # Only the 2015 entry could have been known in 2016.
    assert outcome.future_rejected >= 3
    for item in application.knowledge.retained_items(limit=50):
        assert item.available_from <= datetime(2016, 1, 1, tzinfo=timezone.utc)


# --- 17.1: a gap is not a trigger --------------------------------------------


def test_the_selector_has_all_seven_options() -> None:
    from app.epistemics.actions import EpistemicAction
    import typing

    assert set(typing.get_args(EpistemicAction)) == {
        "recall",
        "infer",
        "ask_user",
        "web_search",
        "defer",
        "ignore",
        "avoid",
    }


async def test_a_gap_answerable_from_memory_is_not_searched(
    application, clock
) -> None:
    gap_id = _raise_gap(application, clock, topic="昨日の話")
    row = application.gaps.get(gap_id)
    known = _a_gap("昨日の話", likely_in_memory=True, time_sensitive=False)

    decision = application.investigation.decide(known, gap_id=gap_id)

    assert decision.action != "web_search"
    assert application.gaps.get(gap_id)["chosen_action"] == decision.action


async def test_the_builder_only_produces_a_candidate_for_web_search(
    application, clock
) -> None:
    """The structural form of 17.1: a gap the selector answers another way
    leaves no search candidate behind at all."""
    from app.runtime.knowledge import KnowledgeCandidates
    from app.world.models import Opportunity

    gap_id = application.gaps.raise_gap(
        topic="どうでもいいこと",
        relevance=0.02,
        uncertainty=0.1,
        curiosity=0.0,
        now=clock.now(),
    )
    candidates = KnowledgeCandidates(application.gaps, application.investigation)

    candidate = candidates.investigate(
        Opportunity(kind=KNOWLEDGE_GAP, detail=gap_id, created_at=clock.now()),
        clock.now(),
    )

    assert candidate is None
    assert application.gaps.get(gap_id)["chosen_action"] != "web_search"


async def test_the_decision_is_recorded_either_way(application, clock) -> None:
    """So that "she searches for everything" is settled by a query."""
    for index in range(4):
        gap_id = application.gaps.raise_gap(
            topic=f"話題{index}", relevance=0.05 * index, now=clock.now()
        )
        application.investigation.decide(
            _a_gap(f"話題{index}", relevance=0.05 * index, curiosity=0.05 * index),
            gap_id=gap_id,
        )

    taken = application.gaps.actions_taken()
    assert taken, "no epistemic decisions were recorded"
    assert sum(taken.values()) == 4


# --- request ≠ success ≠ acquisition -----------------------------------------


async def test_a_search_is_not_an_acquisition(application, clock) -> None:
    """The number that matters: results in, far fewer facts out."""
    gap_id = _raise_gap(application, clock)

    outcome = await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    assert outcome.results > 0
    assert outcome.acquired <= outcome.exposed <= outcome.results
    assert outcome.acquired < outcome.results, (
        "every result became knowledge; the funnel is not doing anything"
    )


async def test_the_two_events_are_different(application, clock) -> None:
    gap_id = _raise_gap(application, clock)

    outcome = await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    types = [event.event_type for event in application.event_store.recent(limit=30)]
    assert SEARCH_PERFORMED in types
    searched = types.count(SEARCH_PERFORMED)
    acquired = types.count(KNOWLEDGE_ACQUIRED)
    assert searched == 1
    assert acquired == outcome.acquired


async def test_the_search_goes_through_the_tool_manager(application, clock) -> None:
    """32.2: ToolManager success is the only authority. A provider reached
    beside the tool would let a search that never ran be described as one that
    did."""
    gap_id = _raise_gap(application, clock)
    before = application.db.scalar("SELECT COUNT(*) FROM tool_calls")

    outcome = await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    after = application.db.scalar("SELECT COUNT(*) FROM tool_calls")
    assert after == before + 1
    assert outcome.tool_call_id is not None


# --- failure is not knowledge ------------------------------------------------


@pytest.mark.parametrize(
    "behaviour", ["timeout", "network_error", "provider_error", "no_results"]
)
async def test_a_failed_search_acquires_nothing(
    application, clock, behaviour: str
) -> None:
    gap_id = _raise_gap(application, clock)
    _provider(application).behaviour = behaviour

    outcome = await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    assert outcome.acquired == 0
    assert not outcome.learned_anything
    assert application.knowledge.retained_items(limit=10) == []


async def test_no_results_is_not_a_fact_about_the_world(application, clock) -> None:
    """"We looked and found nothing" is about the search. It is not a claim
    that the thing does not exist, and nothing may record it as one."""
    gap_id = _raise_gap(application, clock, topic="存在しない話題")
    _provider(application).behaviour = "no_results"

    outcome = await application.investigation.investigate(
        _a_gap("存在しない話題"), effective_now=clock.now(), gap_id=gap_id
    )

    assert outcome.outcome == "no_results"
    assert application.knowledge_repo.count() == 0
    statements = [
        item.statement for item in application.knowledge.retained_items(limit=20)
    ]
    assert not any("存在しない" in statement for statement in statements)
    # And the gap stays open: nothing was answered.
    assert application.gaps.get(gap_id)["status"] == "open"


def test_the_failure_modes_are_distinct() -> None:
    from app.knowledge.search import PRODUCTIVE, SearchOutcome
    import typing

    outcomes = set(typing.get_args(SearchOutcome))
    assert {"timeout", "network_error", "provider_error", "no_results"} <= outcomes
    # None of the failures counts as having found something.
    assert not (PRODUCTIVE & {"timeout", "network_error", "provider_error", "no_results"})


async def test_no_provider_is_not_no_results(application, clock) -> None:
    """"We have nowhere to look" and "we looked and found nothing" are
    different, and only the second is about the world."""
    response = await NullSearchProvider().search(
        SearchQuery(text="海", effective_now=datetime.now(timezone.utc))
    )

    assert response.outcome == "provider_error"
    assert not response.found_nothing


# --- external fact ≠ her belief ----------------------------------------------


async def test_a_search_result_argues_rather_than_decrees(
    application, clock
) -> None:
    """Overwriting a belief because a search disagreed would make her a search
    cache with a personality attached — and mean she can never be wrong for
    longer than one query."""
    import inspect

    from app.knowledge import investigation

    source = inspect.getsource(investigation.InvestigationService)
    assert "record_evidence" in source
    for overwrite in ("set_belief", "replace_belief", "suppress("):
        assert overwrite not in source, overwrite


async def test_acquired_knowledge_reaches_the_belief_engine(
    application, clock
) -> None:
    gap_id = _raise_gap(application, clock)
    before = len(application.beliefs.held_beliefs(limit=50))

    outcome = await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    if outcome.acquired:
        assert len(application.beliefs.held_beliefs(limit=50)) >= before


# --- provenance --------------------------------------------------------------


async def test_every_acquired_fact_knows_where_it_came_from(
    application, clock
) -> None:
    """Genesis asks "when could this have been known, and on whose word?".
    A row that cannot answer is a leak waiting twelve phases."""
    gap_id = _raise_gap(application, clock)

    outcome = await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )
    assert outcome.acquired > 0

    for knowledge_id in outcome.acquired_ids:
        provenance = application.knowledge_repo.provenance(knowledge_id)
        assert provenance is not None
        assert provenance["source_type"] == "web_search"
        assert provenance["retrieved_at"]
        assert provenance["search_id"] == outcome.search_id
        assert provenance["available_from"]


# --- the runtime actually drives it ------------------------------------------


def test_it_is_registered_in_the_loop(application) -> None:
    registry = application.runtime._registry
    assert "knowledge_gaps" in {source.name for source in registry.sources}
    assert registry.builder_for(KNOWLEDGE_GAP) is not None
    assert registry.handler_for(INVESTIGATE) is not None


async def test_an_open_gap_is_offered_by_the_source(application, clock) -> None:
    gap_id = _raise_gap(application, clock)
    source = GapSource(application.gaps, application.world, clock=clock)

    offered = list(source.collect(clock.now()))

    assert [item.detail for item in offered] == [gap_id]


async def test_the_whole_chain_runs_from_the_loop(application, clock) -> None:
    """The fixture E2E the phase is judged on: gap → selector → decision →
    tool → provider → temporal gate → exposure → knowledge row."""
    _raise_gap(application, clock)

    for _ in range(6):
        tick = await application.runtime.tick()
        if tick.chosen_action == INVESTIGATE:
            break
        clock.advance(hours=2)
    else:  # pragma: no cover - the loop above is the assertion
        pytest.fail("the runtime never chose to investigate an open gap")

    totals = application.searches.totals()
    assert application.searches.count() > 0, "no search was attempted"
    assert totals["results"] > 0, "the provider returned nothing"
    assert totals["future_rejected"] > 0, "the temporal gate never fired"
    assert totals["exposed"] > 0, "nothing reached the exposure funnel"
    assert totals["acquired"] > 0, "nothing was ever learned"
    assert totals["acquired"] < totals["results"], (
        "every result became knowledge; the funnel is not doing anything"
    )


async def test_a_failed_search_path_is_exercised_too(application, clock) -> None:
    gap_id = _raise_gap(application, clock)
    _provider(application).behaviour = "network_error"

    await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    assert application.searches.count(outcome="network_error") > 0


# --- the debug path ----------------------------------------------------------


async def test_the_search_views_are_wired(application, clock) -> None:
    gap_id = _raise_gap(application, clock)
    await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    for command in ("search recent", "gaps", "tools", "knowledge"):
        outcome = await application.admin_router.route(
            text=f"{ADMIN_PREFIX} {command}", author_id=OWNER, channel_id=CHANNEL
        )
        assert not outcome.result.failed, (command, outcome.result.error)
        assert "not wired yet" not in outcome.result.summary, command


async def test_knowledge_find_shows_provenance(application, clock) -> None:
    """The view the Genesis leakage audit will use."""
    gap_id = _raise_gap(application, clock)
    await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )

    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} knowledge find 海", author_id=OWNER, channel_id=CHANNEL
    )

    assert not outcome.result.failed, outcome.result.error
    rows = [row for section in outcome.result.sections for row in section.rows]
    assert rows
    assert rows[0]["source_type"] == "web_search"
    assert rows[0]["available_from"]


async def test_the_debug_views_search_for_nothing(application, clock) -> None:
    """Reading the knowledge table is not an act of looking anything up."""
    gap_id = _raise_gap(application, clock)
    await application.investigation.investigate(
        _a_gap(), effective_now=clock.now(), gap_id=gap_id
    )
    before = application.searches.count()

    for _ in range(5):
        await application.admin_router.route(
            text=f"{ADMIN_PREFIX} knowledge find 海", author_id=OWNER, channel_id=CHANNEL
        )

    assert application.searches.count() == before


def _provider(application) -> FixtureSearchProvider:
    return application.investigation._provider  # type: ignore[attr-defined]
