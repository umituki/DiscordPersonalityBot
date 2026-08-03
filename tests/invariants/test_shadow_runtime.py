"""INVARIANT: SHADOW deliberates fully and acts on nothing
(rebuild spec 47, 28.4 — Phase 14).

Four capabilities act without being asked, and all four are hard to take back:
a message the USER did not prompt, a decision not to answer one they did, a
social life lived while nobody is watching, and a request that leaves this
machine. 危険な自律機能は first live から直接送信しない.

Two failure shapes this file is written against.

**A shadow that is not consulted.** The lesson of Phase 11 was that a
SearchProvider nobody calls is not a capability, and a mode nobody reads is the
same bug wearing a different hat. So the central test here does not check that
`ShadowController` returns the right string — it drives the wired application
through each of the four and asks whether the effect happened. A capability
with no call site fails, loudly, in the test that enumerates all four.

**A shadow that is a second implementation.** SHADOW has to be the real path
with the last step removed, not a parallel dry-run. That is why the assertions
are about what is *missing* downstream — no interaction row, no search call, no
sent message — while the deliberation upstream really ran.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.admin.router import ADMIN_PREFIX
from app.bootstrap import Application
from app.conversation.response_intent import (
    ResponseIntent,
    ResponseIntentGate,
    VetoReason,
)
from app.conversation.social_interpretation import SocialInterpretation
from app.runtime.knowledge import INVESTIGATE
from app.runtime.shadow import (
    MODES,
    SHADOW_CAPABILITIES,
    ShadowController,
    UnknownCapability,
)
from app.runtime.social import CONTACT_NPC
from app.society.events import NPC_INTERACTION
from tests.support import live_shadow, use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


def _owned(config):
    return config.model_copy(
        update={
            "secrets": config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )


@pytest.fixture
def shadowed(temp_config, clock):
    """The default: everything deliberates, nothing acts."""
    built = Application.build(_owned(temp_config), clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


@pytest.fixture
def live(temp_config, clock):
    """The same application with the four switched on."""
    built = Application.build(
        live_shadow(_owned(temp_config)), clock=clock, configure_logs=False
    )
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


def _raise_gap(application, clock, topic: str = "海") -> str:
    return application.gaps.raise_gap(
        topic=topic,
        known_part="海があること",
        unknown_part="どうして塩辛いのか",
        uncertainty=0.8,
        relevance=0.8,
        curiosity=0.8,
        time_sensitive=False,
        now=clock.now(),
    )


async def _tick_until(application, clock, action: str, *, tries: int = 6) -> bool:
    for _ in range(tries):
        tick = await application.runtime.tick()
        if tick.chosen_action == action:
            return True
        clock.advance(hours=2)
    return False


# =============================================================================
# The mode is real, and everything reads the same one.
# =============================================================================


def test_the_capabilities_are_the_specs_four() -> None:
    assert set(SHADOW_CAPABILITIES) == {
        "proactive_contact",
        "intentional_silence",
        "npc_contact",
        "web_search",
    }
    assert MODES == ("OFF", "SHADOW", "LIVE")


def test_a_capability_nobody_specified_is_shadow() -> None:
    """The default direction is the safe one. A capability added to the list
    and forgotten in the config must not thereby go live."""
    controller = ShadowController(modes={})

    assert all(controller.mode(name) == "SHADOW" for name in SHADOW_CAPABILITIES)


def test_an_unknown_capability_is_refused() -> None:
    with pytest.raises(UnknownCapability):
        ShadowController(modes={"send_emails": "LIVE"})
    with pytest.raises(UnknownCapability):
        ShadowController(modes={}).mode("send_emails")


def test_a_nonsense_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        ShadowController(modes={"web_search": "SOMETIMES"})


def test_off_does_not_deliberate(shadowed) -> None:
    """28.4: OFF sends nothing *and deliberates nothing*."""
    controller = ShadowController(modes={"proactive_contact": "OFF"})

    assert controller.runs("proactive_contact") is False
    assert controller.live("proactive_contact") is False
    assert controller.runs("web_search") is True  # the default is SHADOW


def test_the_shipped_default_is_shadow_for_all_four(shadowed) -> None:
    """初期運用は SHADOW. A fresh install must not reach out, search, go quiet
    or live a social life on its first tick."""
    assert set(shadowed.shadow.as_dict().values()) == {"SHADOW"}


# =============================================================================
# Every capability has a call site. This is the test the phase turns on.
# =============================================================================


async def test_every_capability_actually_asks(shadowed, clock) -> None:
    """A mode nobody reads is not a mode.

    Each of the four is driven through the wired application, and each must
    leave a row saying it asked. A capability that acquires a mode and no call
    site fails here rather than in production.
    """
    # 1. npc_contact
    shadowed.society.introduce(name="ミカ", tier=1, availability=0.9, warmth=0.8)
    await _tick_until(shadowed, clock, CONTACT_NPC)

    # 2. web_search
    _raise_gap(shadowed, clock)
    await _tick_until(shadowed, clock, INVESTIGATE)

    # 3. proactive_contact
    await shadowed.proactive_deliberation.deliberate(
        _opportunity(clock), _view(), now=clock.now()
    )

    # 4. intentional_silence
    shadowed.shadow.decide(
        "intentional_silence", would_act=True, subject="うん", now=clock.now()
    )

    asked = {row["capability"] for row in shadowed.shadow_decisions.recent(limit=200)}
    missing = set(SHADOW_CAPABILITIES) - asked
    assert not missing, f"no call site reached for: {sorted(missing)}"


def _opportunity(clock):
    from app.world.models import Opportunity

    return Opportunity(
        kind="proactive_contact",
        detail="unfinished_conversation:昨日の話",
        urgency=0.7,
        created_at=clock.now(),
    )


def _view():
    class View:
        def number(self, domain, key, default=0.0):
            return {"connection_desire": 0.9}.get(key, default)

    return View()


# =============================================================================
# npc_contact
# =============================================================================


async def test_a_shadowed_npc_contact_commits_nothing(shadowed, clock) -> None:
    """She chooses who to talk to, and the relationship does not move."""
    npc = shadowed.society.introduce(name="ミカ", tier=1, availability=0.9, warmth=0.8)

    chose = await _tick_until(shadowed, clock, CONTACT_NPC)

    assert chose, "the runtime never chose to contact anyone"
    assert shadowed.society.interactions_with(npc.npc_id) == []
    types = {event.event_type for event in shadowed.event_store.recent(limit=20)}
    assert NPC_INTERACTION not in types
    row = shadowed.shadow_decisions.recent(capability="npc_contact", limit=1)[0]
    assert row["would_act"] == 1
    assert row["acted"] == 0
    assert row["subject"] == "ミカ"


async def test_a_live_npc_contact_commits(live, clock) -> None:
    """The same path with the last step allowed."""
    npc = live.society.introduce(name="ミカ", tier=1, availability=0.9, warmth=0.8)

    await _tick_until(live, clock, CONTACT_NPC)

    assert len(live.society.interactions_with(npc.npc_id)) == 1
    row = live.shadow_decisions.recent(capability="npc_contact", limit=1)[0]
    assert row["acted"] == 1


# =============================================================================
# web_search — the one that leaves the machine
# =============================================================================


async def test_a_shadowed_search_never_reaches_the_provider(shadowed, clock) -> None:
    """SHADOW has to stop *before* the request, not before the result is kept.

    A search that ran and was then discarded has already done the thing spec 47
    is about: it left this machine.
    """
    gap_id = _raise_gap(shadowed, clock)

    chose = await _tick_until(shadowed, clock, INVESTIGATE)

    assert chose, "the runtime never chose to investigate"
    assert shadowed.searches.count() == 0, "a search was performed in SHADOW"
    # And the gap is still open, because nothing answered it.
    assert shadowed.gaps.get(gap_id)["status"] == "open"
    row = shadowed.shadow_decisions.recent(capability="web_search", limit=1)[0]
    assert row["would_act"] == 1 and row["acted"] == 0


async def test_a_live_search_reaches_the_provider(live, clock) -> None:
    _raise_gap(live, clock)

    await _tick_until(live, clock, INVESTIGATE)

    assert live.searches.count() > 0
    row = live.shadow_decisions.recent(capability="web_search", limit=1)[0]
    assert row["acted"] == 1


# =============================================================================
# proactive_contact — Phase 9's chain, now asking Phase 14 for permission
# =============================================================================


async def test_a_shadowed_proactive_message_is_not_sent(shadowed, clock) -> None:
    outcome = await shadowed.proactive_deliberation.deliberate(
        _opportunity(clock), _view(), now=clock.now()
    )

    assert outcome.sent is False
    rows = shadowed.shadow_decisions.recent(capability="proactive_contact", limit=1)
    assert rows and rows[0]["acted"] == 0


async def test_the_proactive_mode_comes_from_the_controller(live) -> None:
    """One answer per process. A deliberation holding its own copy of the mode
    is a deliberation that can disagree with the configuration."""
    assert live.proactive_deliberation.mode == "LIVE"
    assert live.shadow.mode("proactive_contact") == "LIVE"


# =============================================================================
# intentional_silence — the shadow of *not* doing something
# =============================================================================


def _quiet_turn() -> SocialInterpretation:
    return SocialInterpretation(
        primary_move="acknowledge",
        wants_to_speak="silence",
        user_state_hint="unknown",
        reason="会話が閉じている",
    )


def test_a_shadowed_silence_speaks_instead() -> None:
    """The record of what she would have done is `proposed`, which the gate
    already keeps for vetoes — the mode is another Python-side override, not a
    new mechanism."""
    gate = ResponseIntentGate()

    decision = gate.decide(_quiet_turn(), user_text="うん", silence_allowed=False)

    assert decision.speaks
    assert decision.intent is ResponseIntent.BRIEF_REPLY
    assert decision.proposed is ResponseIntent.INTENTIONAL_SILENCE
    assert decision.source == "shadow"


def test_a_live_silence_stays_silent() -> None:
    gate = ResponseIntentGate()

    decision = gate.decide(_quiet_turn(), user_text="うん", silence_allowed=True)

    assert decision.is_silence
    assert decision.source == "llm"


def test_a_veto_still_speaks_in_every_mode() -> None:
    """The veto is a hard gate and a mode is not. A direct question is answered
    whatever spec 47 says, and the reason recorded is the veto."""
    gate = ResponseIntentGate()

    for allowed in (True, False):
        decision = gate.decide(
            _quiet_turn(), user_text="これどう思う？", silence_allowed=allowed
        )

        assert decision.speaks
        assert decision.veto is VetoReason.DIRECT_QUESTION


async def test_the_wired_engine_reads_the_silence_mode(shadowed, live) -> None:
    """Not the gate in isolation: the engine the conversation service uses."""
    assert shadowed.conversation_engine._silence_allowed() is False
    assert live.conversation_engine._silence_allowed() is True


async def test_a_broken_controller_does_not_break_a_turn(live) -> None:
    """A mode that cannot be read must not cost her the ability to answer."""

    class Broken:
        def live(self, capability):
            raise RuntimeError("gone")

    engine = live.conversation_engine
    engine._shadow = Broken()

    assert engine._silence_allowed() is True


def test_only_mode_decided_silences_are_recorded(shadowed, clock) -> None:
    """A vetoed turn is not a shadow decision.

    Recording it would credit the mode with restraint that was Python's, and
    bury the rows the OWNER has to read under every question she answered.
    """
    engine = shadowed.conversation_engine
    gate = ResponseIntentGate()
    vetoed = gate.decide(_quiet_turn(), user_text="これどう思う？")

    engine._record_silence_shadow(vetoed, "これどう思う？")

    assert shadowed.shadow_decisions.recent(capability="intentional_silence") == []


# =============================================================================
# The record, and reviewing it
# =============================================================================


def test_wanted_and_acted_are_separate_facts(shadowed, clock) -> None:
    """"She decided not to" and "she was not allowed to" are different things
    about her, and a review that cannot tell them apart is not a review."""
    shadowed.shadow.decide(
        "npc_contact", would_act=False, subject="だれも", now=clock.now()
    )
    shadowed.shadow.decide(
        "npc_contact", would_act=True, subject="ミカ", now=clock.now()
    )

    suppressed = shadowed.shadow_decisions.suppressed()

    assert [row["subject"] for row in suppressed] == ["ミカ"]
    assert shadowed.shadow_decisions.count() == 2


def test_the_tally_shows_an_unevaluated_capability(shadowed, clock) -> None:
    """SHADOW with `wanted = 0` means the capability was enabled, not
    evaluated — and reporting the first as the second is how a review gets
    skipped."""
    shadowed.shadow.decide(
        "web_search", would_act=False, subject="なし", now=clock.now()
    )

    tally = {row["capability"]: row for row in shadowed.shadow_decisions.tally()}

    assert tally["web_search"]["considered"] == 1
    assert (tally["web_search"]["wanted"] or 0) == 0
    assert "npc_contact" not in tally


def test_a_review_note_does_not_rewrite_the_decision(shadowed, clock) -> None:
    verdict = shadowed.shadow.decide(
        "npc_contact", would_act=True, subject="ミカ", reason="退屈", now=clock.now()
    )

    shadowed.shadow_decisions.review(
        verdict.shadow_id, note="送ってよい", now=clock.now()
    )

    row = shadowed.shadow_decisions.get(verdict.shadow_id)
    assert row["review_note"] == "送ってよい"
    assert row["reviewed_at"]
    assert row["would_act"] == 1 and row["acted"] == 0
    assert row["reason"] == "退屈"
    assert shadowed.shadow_decisions.unreviewed_count() == 0


def test_the_record_survives_a_restart(temp_config, clock) -> None:
    first = Application.build(_owned(temp_config), clock=clock, configure_logs=False)
    first.shadow.decide(
        "npc_contact", would_act=True, subject="ミカ", now=clock.now()
    )
    before = first.shadow_decisions.count()
    first.db.close()

    second = Application.build(_owned(temp_config), clock=clock, configure_logs=False)
    try:
        assert second.shadow_decisions.count() == before
        assert second.shadow_decisions.suppressed()[0]["subject"] == "ミカ"
    finally:
        second.db.close()


# =============================================================================
# The review surface
# =============================================================================


async def test_the_shadow_views_are_wired(shadowed, clock) -> None:
    shadowed.shadow.decide(
        "npc_contact", would_act=True, subject="ミカ", now=clock.now()
    )

    for command in ("shadow", "shadow recent", "shadow suppressed"):
        outcome = await shadowed.admin_router.route(
            text=f"{ADMIN_PREFIX} {command}", author_id=OWNER, channel_id=CHANNEL
        )
        assert not outcome.result.failed, outcome.result.error


async def test_the_shadow_views_change_nothing(shadowed, clock) -> None:
    shadowed.shadow.decide(
        "npc_contact", would_act=True, subject="ミカ", now=clock.now()
    )
    before = (shadowed.shadow_decisions.count(), shadowed.event_store.count())

    for _ in range(3):
        for command in ("shadow", "shadow recent", "shadow suppressed"):
            await shadowed.admin_router.route(
                text=f"{ADMIN_PREFIX} {command}", author_id=OWNER, channel_id=CHANNEL
            )

    assert (shadowed.shadow_decisions.count(), shadowed.event_store.count()) == before


def test_the_admin_plane_cannot_switch_a_capability_on() -> None:
    """Going live is a configuration decision the OWNER makes deliberately, not
    something one chat message away."""
    from app.admin.commands import REGISTRY

    assert all(REGISTRY[key].is_read_only for key in REGISTRY if key.startswith("shadow"))
    for key in REGISTRY:
        assert not key.startswith("shadow live")
        assert not key.startswith("shadow mode")


@pytest.mark.parametrize("action", ["status", "list", "suppressed"])
def test_every_shadow_command_runs(temp_config, capsys, action) -> None:
    from app import main

    settings = temp_config.root_dir / "config" / "settings.yaml"
    argv = ["--config", str(settings), "--root", str(temp_config.root_dir)]
    main.main([*argv, "migrate"])
    capsys.readouterr()

    code = main.main([*argv, "shadow", action])

    assert capsys.readouterr().out.strip()
    assert code == 0


def test_review_needs_something_to_review(temp_config, capsys) -> None:
    from app import main

    settings = temp_config.root_dir / "config" / "settings.yaml"
    argv = ["--config", str(settings), "--root", str(temp_config.root_dir)]
    main.main([*argv, "migrate"])
    capsys.readouterr()

    assert main.main([*argv, "shadow", "review"]) == 2
    assert main.main([*argv, "shadow", "review", "--id", "shd_x", "--note", "ok"]) == 1
