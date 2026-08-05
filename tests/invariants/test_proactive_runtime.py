"""INVARIANT: she does not talk more when nobody answers
(rebuild spec 28 — Phase 9).

28.1 states the rule as a structural prohibition, not a tuning target:

    USER が返さないほど送信頻度が増える構造は禁止。

A system that reaches out more when nobody replies is not lonely, it is broken —
silence becomes an input that produces messages, which produces more silence.
Most of this file is about that one direction of causation.

The rest is 28.4: 初期運用は SHADOW. Shadow has to deliberate *fully* and record
what she would have said, because a shadow mode that skips the work proves
nothing about the mode that does not skip it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.admin.router import ADMIN_PREFIX
from app.bootstrap import Application
from app.conversation.events import YUI_MESSAGE_SENT
from app.runtime.proactive import (
    PROACTIVE_CONTACT,
    REACH_OUT,
    TRIGGER_EVENTS,
    NullSender,
    ProactiveActions,
    ProactiveCandidates,
    ProactiveDeliberation,
    ProactiveJudgment,
    ProactiveSource,
)
from app.world.models import Opportunity
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


class Judge:
    """A model that answers the 28.2 question however the test needs."""

    def __init__(self, judgment: ProactiveJudgment, draft: str = "海を見てきたよ。") -> None:
        self.judgment = judgment
        self.draft = draft
        self.calls: list[str] = []

    async def generate(self, schema, messages, *, purpose: str, **kwargs):
        self.calls.append(purpose)
        value = self.judgment if purpose == "proactive_judgment" else schema(text=self.draft)
        return type("Outcome", (), {"ok": True, "value": value})()


class Recorder:
    """A sender that records instead of sending, so LIVE can be tested."""

    def __init__(self, message_id: str | None = "999") -> None:
        self.sent: list[str] = []
        self._message_id = message_id

    async def send(self, text: str) -> str | None:
        self.sent.append(text)
        return self._message_id


def _shadow(application, mode):
    """A controller for one capability's mode (Phase 14).

    The deliberation no longer knows its own mode: it asks the
    ShadowController, so that "is proactive contact live" has one answer per
    process rather than one per object holding a copy.
    """
    from app.runtime.shadow import ShadowController

    return ShadowController(
        modes={"proactive_contact": mode},
        repository=application.shadow_decisions,
        clock=application.clock,
    )


def _deliberation(application, *, mode="SHADOW", judge=None, sender=None):
    return ProactiveDeliberation(
        engine=application.proactive,
        source=ProactiveSource(
            application.event_store, application.state, clock=application.clock
        ),
        deliberations=application.proactive_deliberations,
        shadow=_shadow(application, mode),
        structured=judge,
        prompts=application.prompts if judge is not None else None,
        guard=application.guard,
        sender=sender,
        processor=application.processor,
        channel_id=CHANNEL,
        clock=application.clock,
    )


class _View:
    def __init__(self, **values: float) -> None:
        self._values = values

    def number(self, domain: str, key: str, default: float = 0.0) -> float:
        return self._values.get(key, default)


def _wants_to_talk() -> _View:
    return _View(connection_desire=0.9, loneliness=0.8, emotional_closeness=0.8)


def _opportunity(clock, kind: str = "activity_completion") -> Opportunity:
    return Opportunity(
        kind=PROACTIVE_CONTACT, detail=f"{kind}:ACTIVITY_FINISHED", created_at=clock.now()
    )


# --- 28.1: the anti-feedback rule --------------------------------------------


async def test_silence_never_increases_the_rate(application, clock) -> None:
    """The whole of 28.1 in one assertion: each unanswered message makes the
    next one wait longer, never sooner."""
    waits = []
    while True:
        assessment = application.proactive.assess(
            _opportunity(clock), _wants_to_talk(), now=clock.now()
        )
        if not assessment.allowed:
            # The ceiling. Beyond it there is no interval at all, which is the
            # strongest possible form of "not sooner".
            assert "unanswered" in assessment.reason
            break
        waits.append(assessment.required_wait_hours)
        application.proactive.record_sent(_opportunity(clock))
        clock.advance(days=30)  # long enough that only the backoff matters

    assert len(waits) >= 2, "the gate blocked before the backoff could be seen"
    assert waits == sorted(waits), waits
    assert waits[-1] > waits[0]


async def test_too_many_unanswered_stops_her_entirely(application, clock) -> None:
    for _ in range(5):
        application.proactive.record_sent(_opportunity(clock))
        clock.advance(days=30)

    assessment = application.proactive.assess(
        _opportunity(clock), _wants_to_talk(), now=clock.now()
    )

    assert not assessment.allowed
    assert "unanswered" in assessment.reason


async def test_a_reply_clears_the_backlog(application, clock) -> None:
    """The feedback runs the other way too: answering restores the interval."""
    for _ in range(3):
        application.proactive.record_sent(_opportunity(clock))
        clock.advance(days=30)
    blocked = application.proactive.assess(
        _opportunity(clock), _wants_to_talk(), now=clock.now()
    )
    assert not blocked.allowed

    application.proactive.note_user_replied()
    clock.advance(days=30)
    after = application.proactive.assess(
        _opportunity(clock), _wants_to_talk(), now=clock.now()
    )

    assert after.allowed, "answering did not restore the interval"
    assert application.proactive.unanswered() == 0


async def test_the_gate_runs_before_the_model_is_asked(application, clock) -> None:
    """28.2 says 「Hard Gate 通過後に LLM が判断」. A model consulted first would
    be a model that can talk her past the gate."""
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="話したい"))
    application.proactive.record_sent(_opportunity(clock))  # sets a cooldown

    outcome = await _deliberation(application, judge=judge).deliberate(
        _opportunity(clock), _wants_to_talk()
    )

    assert not outcome.gate_passed
    assert judge.calls == [], "the model was asked despite the gate blocking"
    assert not outcome.would_send


async def test_a_blocked_deliberation_is_still_recorded(application, clock) -> None:
    application.proactive.record_sent(_opportunity(clock))

    await _deliberation(application).deliberate(_opportunity(clock), _wants_to_talk())

    rows = application.proactive_deliberations.recent()
    assert len(rows) == 1
    assert rows[0]["gate_passed"] == 0
    assert rows[0]["gate_reason"]


# --- 28.2: the judgment, and its conservative failure ------------------------


async def test_no_model_means_no_message(application, clock) -> None:
    """Failing towards "send something" is how a degraded system becomes a
    talkative one."""
    outcome = await _deliberation(application, judge=None).deliberate(
        _opportunity(clock), _wants_to_talk()
    )

    assert outcome.gate_passed
    assert not outcome.judged
    assert not outcome.would_send


async def test_a_broken_model_means_no_message(application, clock) -> None:
    class Broken:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("the model is gone")

    outcome = await _deliberation(application, judge=Broken()).deliberate(
        _opportunity(clock), _wants_to_talk()
    )

    assert outcome.gate_passed
    assert not outcome.would_send


@pytest.mark.parametrize("answer", ["no", "not_really"])
async def test_anything_short_of_yes_is_no(application, clock, answer: str) -> None:
    judge = Judge(ProactiveJudgment(wants_to_say=answer))

    outcome = await _deliberation(application, judge=judge).deliberate(
        _opportunity(clock), _wants_to_talk()
    )

    assert outcome.judged
    assert not outcome.would_send
    assert "proactive_message" not in judge.calls  # it never got as far as drafting


async def test_the_judgment_is_categorical() -> None:
    """A float here invites the tuning 28.1 forbids."""
    import typing

    hints = typing.get_type_hints(ProactiveJudgment)
    assert typing.get_origin(hints["wants_to_say"]) is typing.Literal
    assert typing.get_origin(hints["because"]) is typing.Literal


# --- 28.4: SHADOW deliberates fully and sends nothing ------------------------


async def test_shadow_is_the_default(application) -> None:
    assert application.config.runtime.proactive_mode == "SHADOW"
    assert application.proactive_deliberation.mode == "SHADOW"


async def test_shadow_records_what_she_would_have_said(application, clock) -> None:
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="海を見た"))
    sender = Recorder()

    outcome = await _deliberation(
        application, mode="SHADOW", judge=judge, sender=sender
    ).deliberate(_opportunity(clock), _wants_to_talk())

    assert outcome.would_send is True
    assert outcome.sent is False
    assert outcome.draft
    assert sender.sent == [], "shadow mode sent something"
    rows = application.proactive_deliberations.recent()
    assert rows[0]["would_send"] == 1
    assert rows[0]["sent"] == 0


async def test_shadow_leaves_no_contact_row(application, clock) -> None:
    """A contact she did not make must not count against the backoff."""
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="海を見た"))
    before = application.proactive.unanswered()

    await _deliberation(application, mode="SHADOW", judge=judge).deliberate(
        _opportunity(clock), _wants_to_talk()
    )

    assert application.proactive.unanswered() == before
    types = {event.event_type for event in application.event_store.recent(limit=10)}
    assert YUI_MESSAGE_SENT not in types


async def test_off_deliberates_nothing_at_all(application, clock) -> None:
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="海を見た"))

    outcome = await _deliberation(application, mode="OFF", judge=judge).deliberate(
        _opportunity(clock), _wants_to_talk()
    )

    assert not outcome.gate_passed
    assert judge.calls == []
    # Still recorded: "she was switched off" is a fact worth being able to read.
    assert application.proactive_deliberations.count() == 1


# --- 28.5: the live chain ----------------------------------------------------


async def test_live_sends_and_records_in_that_order(application, clock) -> None:
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="海を見た"))
    sender = Recorder()

    outcome = await _deliberation(
        application, mode="LIVE", judge=judge, sender=sender
    ).deliberate(_opportunity(clock), _wants_to_talk())

    assert outcome.sent is True
    assert sender.sent == [outcome.draft]
    assert outcome.event_id and outcome.contact_id
    types = {event.event_type for event in application.event_store.recent(limit=10)}
    assert YUI_MESSAGE_SENT in types
    assert application.proactive.unanswered() == 1


async def test_a_failed_send_records_no_contact(application, clock) -> None:
    """28.5: success only. A contact row for a message that never arrived would
    make the backoff count a silence that was never hers."""
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="海を見た"))

    class Broken:
        async def send(self, text: str) -> str | None:
            raise RuntimeError("discord is down")

    outcome = await _deliberation(
        application, mode="LIVE", judge=judge, sender=Broken()
    ).deliberate(_opportunity(clock), _wants_to_talk())

    assert outcome.would_send is True
    assert outcome.sent is False
    assert application.proactive.unanswered() == 0
    types = {event.event_type for event in application.event_store.recent(limit=10)}
    assert YUI_MESSAGE_SENT not in types


async def test_a_guarded_draft_never_leaves(application, clock) -> None:
    """The same §16 guard as the conversation. A gentler one for unprompted
    messages would be a hole with a justification attached."""
    judge = Judge(
        ProactiveJudgment(wants_to_say="yes", about="散歩"),
        # identity v2: reaching into the USER's world, which no framing and
        # no evidence can make sayable (§16).
        draft="いま君の隣に座っているよ。",
    )
    sender = Recorder()

    outcome = await _deliberation(
        application, mode="LIVE", judge=judge, sender=sender
    ).deliberate(_opportunity(clock), _wants_to_talk())

    assert outcome.guard_verdict != "clean"
    assert outcome.would_send is False
    assert sender.sent == []


async def test_the_null_sender_is_the_default(application, clock) -> None:
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="海を見た"))
    deliberation = ProactiveDeliberation(
        engine=application.proactive,
        source=ProactiveSource(application.event_store, application.state),
        deliberations=application.proactive_deliberations,
        shadow=_shadow(application, "LIVE"),
        structured=judge,
        prompts=application.prompts,
        guard=application.guard,
        processor=application.processor,
        clock=application.clock,
    )

    outcome = await deliberation.deliberate(_opportunity(clock), _wants_to_talk())

    assert outcome.would_send is True
    assert outcome.sent is False  # there was nowhere to send it


def test_the_null_sender_sends_nothing() -> None:
    import asyncio

    assert asyncio.run(NullSender().send("test")) is None


# --- 28.3: a message needs something to be about -----------------------------


async def test_nothing_to_say_is_no_opportunity(application, clock) -> None:
    source = ProactiveSource(application.event_store, application.state, clock=clock)

    assert list(source.collect(clock.now())) == []


async def test_something_that_happened_is_a_trigger(application, clock) -> None:
    source = ProactiveSource(application.event_store, application.state, clock=clock)
    application.state.write_value(
        domain="world", key="sleep_pressure", value=0.1, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=None,
    )
    await application.runtime.tick()  # starts an activity
    clock.advance(minutes=90)
    await application.runtime.tick()  # finishes it

    trigger = source.find_trigger(clock.now())

    # Living produces several kinds of subject at once — finishing something,
    # and feeling about it. Which one wins is "the most recent"; what this
    # asserts is that a real event became a real trigger.
    assert trigger is not None
    assert trigger.kind in set(TRIGGER_EVENTS.values())
    assert trigger.event_id is not None


async def test_a_stale_trigger_is_not_a_reason(application, clock) -> None:
    """Something that happened yesterday is not a reason to message today."""
    source = ProactiveSource(application.event_store, application.state, clock=clock)
    await application.runtime.tick()
    clock.advance(minutes=90)
    await application.runtime.tick()

    clock.advance(days=1)

    assert source.find_trigger(clock.now()) is None


async def test_wanting_company_counts_but_only_when_it_is_real(
    application, clock
) -> None:
    source = ProactiveSource(application.event_store, application.state, clock=clock)
    application.state.write_value(
        domain="needs", key="connection_desire", value=0.2, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=None,
    )
    assert source.find_trigger(clock.now()) is None

    entry = application.state.get("needs", "connection_desire")
    application.state.write_value(
        domain="needs", key="connection_desire", value=0.85, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=entry.version,
    )

    trigger = source.find_trigger(clock.now())
    assert trigger is not None
    assert trigger.kind == "connection_desire"


def test_finishing_something_is_a_trigger_kind() -> None:
    """The 28.3 mapping, directly, so the test above can stay about the scan."""
    assert TRIGGER_EVENTS["ACTIVITY_FINISHED"] == "activity_completion"
    assert TRIGGER_EVENTS["GOAL_PURSUED"] == "goal_event"
    assert TRIGGER_EVENTS["NPC_INTERACTION"] == "npc_event"


def test_the_users_own_messages_are_not_triggers(application, clock) -> None:
    """Otherwise a reply becomes a reason to write again, which is the loop
    28.1 exists to break, running in the other direction."""
    assert "USER_MESSAGE_RECEIVED" not in TRIGGER_EVENTS


# --- the dry run: read-only in the strict sense ------------------------------


async def test_the_dryrun_changes_nothing_at_all(application, clock) -> None:
    """The OWNER's Phase 5 requirement, now that there is something to dry-run:
    no contact row, no decision, no deliberation, no opportunity consumed, no
    event."""
    counts = (
        application.proactive.unanswered(),
        application.proactive_deliberations.count(),
        application.event_store.count(),
        application.runtime_ticks.count(),
    )

    for _ in range(10):
        outcome = await application.admin_router.route(
            text=f"{ADMIN_PREFIX} proactive dryrun",
            author_id=OWNER,
            channel_id=CHANNEL,
        )
        assert not outcome.result.failed, outcome.result.error

    assert (
        application.proactive.unanswered(),
        application.proactive_deliberations.count(),
        application.event_store.count(),
        application.runtime_ticks.count(),
    ) == counts


async def test_the_dryrun_makes_no_model_call(application, clock) -> None:
    before = application.llm_calls.count()

    await application.admin_router.route(
        text=f"{ADMIN_PREFIX} proactive dryrun", author_id=OWNER, channel_id=CHANNEL
    )

    assert application.llm_calls.count() == before


async def test_the_dryrun_reports_the_gate(application, clock) -> None:
    application.state.write_value(
        domain="needs", key="connection_desire", value=0.9, confidence=None,
        now=clock.now(), run_id=None, event_id=None, expected_version=None,
    )
    application.proactive.record_sent(_opportunity(clock))

    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} proactive dryrun", author_id=OWNER, channel_id=CHANNEL
    )

    rows = [row for section in outcome.result.sections for row in section.rows]
    assert any(row.get("gate") == "block" for row in rows)


async def test_the_shadow_view_reads_the_deliberations(application, clock) -> None:
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="海を見た"))
    await _deliberation(application, judge=judge).deliberate(
        _opportunity(clock), _wants_to_talk()
    )

    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} proactive shadow", author_id=OWNER, channel_id=CHANNEL
    )

    assert not outcome.result.failed, outcome.result.error
    rows = [row for section in outcome.result.sections for row in section.rows]
    assert rows and rows[0]["mode"] == "SHADOW"


# --- the loop's rules still hold ---------------------------------------------


def test_it_is_registered_in_the_loop(application) -> None:
    registry = application.runtime._registry
    assert "proactive" in {source.name for source in registry.sources}
    assert registry.builder_for(PROACTIVE_CONTACT) is not None
    assert registry.handler_for(REACH_OUT) is not None


def test_reaching_out_competes_modestly(clock) -> None:
    """The bar for interrupting someone is higher than the bar for entertaining
    herself, and the numbers should say so."""
    candidate = ProactiveCandidates(None).contact(
        Opportunity(kind=PROACTIVE_CONTACT, urgency=1.0, created_at=clock.now()),
        clock.now(),
    )

    assert candidate.expected_value <= 0.6


async def test_a_shadow_deliberation_is_not_reported_as_an_action(
    application, clock
) -> None:
    """Otherwise every shadow run looks like she messaged someone."""
    judge = Judge(ProactiveJudgment(wants_to_say="yes", about="海を見た"))
    actions = ProactiveActions(
        _deliberation(application, mode="SHADOW", judge=judge),
        state=application.state,
        engine=application.proactive,
        clock=clock,
    )

    performed = await actions.reach_out(
        ProactiveCandidates(None).contact(_opportunity(clock), clock.now()), clock.now()
    )

    assert performed is False
    assert application.proactive_deliberations.count() == 1
