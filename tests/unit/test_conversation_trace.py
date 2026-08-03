"""Where the USER's wait went (patch spec 19.2).

The 2026-08-02 reply took four to five minutes and nothing said which stage it
went in. The per-call telemetry of 19.1 says how long each model call took; this
says how long each *stage* took, and keeps queue wait apart from inference so a
busy queue is never mistaken for a slow model.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.observability.trace import STAGES, ConversationTrace, ConversationTracer
from app.storage.repositories.traces import ConversationTraceRepository
from tests.unit.test_conversation import (  # noqa: F401 - shared fixtures
    CHANNEL,
    OWNER,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    inbound,
    make_engine,
)


@pytest.fixture
def traces(db) -> ConversationTraceRepository:
    return ConversationTraceRepository(db)


@pytest.fixture
def tracer(traces, clock) -> ConversationTracer:
    return ConversationTracer(traces, clock=clock)


# --- the trace itself --------------------------------------------------------
def test_it_starts_with_the_moment_the_message_arrived(tracer, clock) -> None:
    trace = tracer.start(channel_id=CHANNEL)

    assert trace.received_at == clock.now()
    assert trace.at("received_at") == clock.now()
    assert trace.channel_id == CHANNEL


def test_every_stage_of_the_spec_is_markable(tracer) -> None:
    """Patch spec 19.2 lists these by name; all of them have to exist."""
    trace = tracer.start()
    for stage in STAGES:
        tracer.mark(trace, stage)

    assert set(trace.marks) == set(STAGES)


def test_an_unknown_stage_is_ignored_rather_than_raised(tracer) -> None:
    trace = tracer.start()
    tracer.mark(trace, "nonsense")

    assert "nonsense" not in trace.marks


def test_elapsed_is_none_until_both_ends_exist(tracer, clock) -> None:
    trace = tracer.start()
    tracer.mark(trace, "reply_started_at")

    assert trace.elapsed_ms("reply_started_at", "reply_ended_at") is None

    clock.advance(seconds=3)
    tracer.mark(trace, "reply_ended_at")
    assert trace.elapsed_ms("reply_started_at", "reply_ended_at") == 3000


def test_the_total_is_measured_to_the_reply_being_visible(tracer, clock) -> None:
    trace = tracer.start()
    clock.advance(seconds=12)
    tracer.mark(trace, "outbound_projected_at")

    assert trace.total_ms == 12_000


def test_a_suppressed_turn_still_has_a_total(tracer, clock) -> None:
    """Nothing was sent, and the USER still waited."""
    trace = tracer.start()
    clock.advance(seconds=5)
    tracer.mark(trace, "reply_ended_at")

    assert trace.total_ms == 5000


def test_queue_wait_and_inference_are_kept_apart(tracer, clock) -> None:
    """Patch spec 19.2: ``Queue waitとinferenceを区別する``."""
    trace = tracer.start()
    clock.advance(seconds=20)
    tracer.mark(trace, "outbound_projected_at")
    trace.add_model_time(queue_wait_ms=8000, inference_ms=9000)

    assert trace.queue_wait_ms == 8000
    assert trace.inference_ms == 9000
    assert trace.model_calls == 1
    # The rest is where a fix would have to go, and it is visible on its own.
    assert trace.unaccounted_ms == 3000


def test_marking_never_raises_on_a_missing_trace(tracer) -> None:
    tracer.mark(None, "reply_started_at")
    tracer.finish(None, outcome="sent")


# --- persistence -------------------------------------------------------------
def test_a_finished_trace_is_stored(tracer, traces, clock) -> None:
    trace = tracer.start(channel_id=CHANNEL)
    trace.event_id = "evt_1"
    clock.advance(seconds=7)
    tracer.mark(trace, "outbound_projected_at")

    tracer.finish(trace, outcome="sent")

    rows = traces.recent()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "sent"
    assert rows[0]["event_id"] == "evt_1"
    assert rows[0]["total_ms"] == 7000


def test_a_finished_trace_can_receive_later_gateway_marks(tracer, traces, clock) -> None:
    trace = tracer.start(channel_id=CHANNEL)
    tracer.mark(trace, "reply_ended_at")
    tracer.finish(trace, outcome="suppressed")
    clock.advance(seconds=1)
    tracer.mark(trace, "typing_stopped_at")

    tracer.finish(trace, outcome="suppressed")

    rows = traces.recent()
    assert len(rows) == 1
    assert rows[0]["typing_stopped_at"] is not None


def test_the_model_time_comes_from_the_calls_the_run_made(
    tracer, traces, llm_calls_repo, clock
) -> None:
    """Read from ``llm_calls``, never inferred from gaps in the timeline."""
    llm_calls_repo.start(
        call_id="call_1",
        run_id="run_1",
        event_id=None,
        manifest_id=None,
        purpose="conversation_reply",
        priority="P0",
        model="test",
        prompt_id=None,
        prompt_version=None,
        structured=True,
        request_fingerprint="fp",
        request_transcript=None,
        now=clock.now(),
    )
    llm_calls_repo.finish(
        call_id="call_1",
        status="accepted",
        now=clock.now(),
        latency_ms=4000,
        prompt_tokens=None,
        completion_tokens=None,
        response_text=None,
        error_type=None,
        error_detail=None,
        queue_wait_ms=1500,
        model_total_duration_ms=3500,
    )
    trace = tracer.start()
    trace.run_id = "run_1"
    clock.advance(seconds=6)
    tracer.mark(trace, "outbound_projected_at")

    tracer.finish(trace, outcome="sent")

    row = traces.recent()[0]
    assert row["queue_wait_ms"] == 1500
    assert row["inference_ms"] == 3500
    assert row["model_calls"] == 1


def test_a_broken_repository_does_not_break_the_reply(clock) -> None:
    class Broken:
        def record(self, trace):
            raise RuntimeError("the disk is gone")

        def calls_for_run(self, run_id):
            return []

    tracer = ConversationTracer(Broken(), clock=clock)
    trace = tracer.start()

    tracer.finish(trace, outcome="sent")  # must not raise


def test_the_stage_breakdown_is_recorded(tracer, traces, clock) -> None:
    trace = tracer.start()
    tracer.mark(trace, "dialogue_started_at")
    clock.advance(seconds=2)
    tracer.mark(trace, "dialogue_ended_at")
    tracer.mark(trace, "outbound_projected_at")

    tracer.finish(trace, outcome="sent")

    import json

    detail = json.loads(traces.recent()[0]["detail_json"])
    assert detail["stages_ms"]["dialogue"] == 2000


def test_latencies_are_readable_for_the_percentiles(tracer, traces, clock) -> None:
    """Spec 20 / 26 need a median and a p95; this is what they read."""
    for seconds in (5, 9, 30):
        trace = tracer.start()
        clock.advance(seconds=seconds)
        tracer.mark(trace, "outbound_projected_at")
        tracer.finish(trace, outcome="sent")
        clock.advance(seconds=1)

    assert sorted(traces.latencies()) == [5000, 9000, 30_000]


# --- through the real conversation path --------------------------------------
@pytest.fixture
def traced_service(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy,
    tracer, clock,
):
    from app.conversation.service import ConversationService
    from app.orchestrator.processor import EventProcessor

    def build(script: list) -> ConversationService:
        processor = EventProcessor(
            db=db,
            event_store=event_store,
            dispatcher=dispatcher,
            snapshots=snapshots,
            arbitrator=arbitrator,
            committer=committer,
            runs=runs,
            failures=failures,
            mode="test",
            clock=clock,
        )
        return ConversationService(
            processor=processor,
            engine=make_engine(
                identity, prompt_registry, guard, conversation_policy, clock, script
            ),
            event_store=event_store,
            conversations=conversations,
            adapter=adapter,
            failures=failures,
            policy=conversation_policy,
            tracer=tracer,
            clock=clock,
        )

    return build


async def test_a_whole_turn_is_traced(traced_service, traces, clock) -> None:
    service = traced_service(['{"text": "やっほー。"}'])

    result = await service.handle_inbound(inbound(clock, text="やっほー"))
    await service.confirm_sent(result, message_id="10")

    row = traces.recent()[0]
    assert row["outcome"] == "sent"
    assert row["admitted_at"] is not None
    assert row["state_commit_started_at"] is not None
    # Phase 3 §47: the stages Phase 3 added are separately visible, so the
    # latency it costs can be measured rather than guessed at.
    assert row["social_interpretation_started_at"] is not None
    assert row["social_interpretation_ended_at"] is not None
    assert row["reference_retrieval_started_at"] is not None
    assert row["reference_retrieval_ended_at"] is not None
    assert row["realization_started_at"] is not None
    assert row["realization_ended_at"] is not None
    assert row["reply_ended_at"] is not None
    assert row["outbound_projected_at"] is not None
    assert row["run_id"] == result.outcome.run.run_id


async def test_an_ignored_message_is_traced_as_ignored(traced_service, traces, clock) -> None:
    service = traced_service([])

    await service.handle_inbound(inbound(clock, text="やっほー", author="999999999999999999"))

    row = traces.recent()[0]
    assert row["outcome"] == "ignored:not_the_user"
    assert row["event_id"] is None


async def test_a_suppressed_reply_is_traced_as_suppressed(
    traced_service, traces, clock
) -> None:
    service = traced_service(['{"text": "コンビニに買い物に行ってきた。"}'] * 2)

    result = await service.handle_inbound(inbound(clock, text="なにしてた?"))

    assert result.suppressed
    assert traces.recent()[0]["outcome"] == "suppressed"


async def test_a_service_without_a_tracer_still_answers(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy, clock,
) -> None:
    """Observability is never a precondition for replying."""
    from app.conversation.service import ConversationService
    from app.orchestrator.processor import EventProcessor

    service = ConversationService(
        processor=EventProcessor(
            db=db, event_store=event_store, dispatcher=dispatcher, snapshots=snapshots,
            arbitrator=arbitrator, committer=committer, runs=runs, failures=failures,
            mode="test", clock=clock,
        ),
        engine=make_engine(
            identity, prompt_registry, guard, conversation_policy, clock,
            ['{"text": "やっほー。"}'],
        ),
        event_store=event_store,
        conversations=conversations,
        adapter=adapter,
        failures=failures,
        policy=conversation_policy,
        clock=clock,
    )

    result = await service.handle_inbound(inbound(clock, text="やっほー"))

    assert result.should_send
    assert result.trace is None
