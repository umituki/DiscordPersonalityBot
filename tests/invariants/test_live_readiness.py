"""INVARIANT: the character plane opens once, deliberately, and never by drift
(rebuild spec 50 Phase 15, 54).

    Phase 15 — Live
    Discord Character mode 開始。

One sentence, and the whole rebuild behind it. Everything before this phase
built something and proved it works; this one decides whether a real person may
talk to her, which is a different claim about a different thing — a system can
be entirely correct and still not be ready, because readiness is a fact about
this database, this machine and this configuration rather than about the code.

The tests are almost all negative, for the same reason Phase 13's were: the
useful property is not that the gate can open. It is that it stays shut while
anything on §54's list is unmet, and that no combination of things becoming
true on their own adds up to going live.

The one check the phase exists for is `shadow_evaluated`. Phase 14 built the
shadow machinery, and machinery nobody consults is the failure this project
keeps rediscovering: a capability switched straight from OFF to LIVE has had
exactly as much review as one that was never built.
"""

from __future__ import annotations

import pytest

from app.admin.router import ADMIN_PREFIX
from app.bootstrap import Application
from app.live.readiness import Check, LiveReadiness, LiveReport
from app.runtime.shadow import SHADOW_CAPABILITIES, ShadowController
from app.storage.migrations import LATEST_VERSION
from tests.support import live_shadow, mark_born, use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


def _owned(config, *, live: bool = False, **runtime):
    return config.model_copy(
        update={
            "secrets": config.secrets.model_copy(
                update={
                    "discord_owner_user_id": OWNER,
                    "discord_channel_id": CHANNEL,
                    "discord_bot_token": "fake-token",
                }
            ),
            "runtime": config.runtime.model_copy(update={"live": live, **runtime}),
        }
    )


@pytest.fixture
def application(temp_config, clock):
    built = Application.build(
        _owned(temp_config, live=True), clock=clock, configure_logs=False
    )
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


class _Contracts(dict):
    """Contracts that are all verified, so other checks can be tested alone."""


class _Verified:
    complete = True


class _Unverified:
    complete = False


def _gate(**overrides) -> LiveReadiness:
    """A gate with everything satisfied, so each test breaks exactly one thing."""

    class Born:
        def status(self):
            return "COMPLETE"

    class NoDecisions:
        def tally(self):
            return []

    defaults = dict(
        first_boot=Born(),
        shadow=ShadowController(modes={}),
        shadow_decisions=NoDecisions(),
        contracts={"normal_reply": _Verified()},
        backups=_Backups([{"backup_id": "b"}]),
        ticks=None,
        owner_id=OWNER,
        channel_id=CHANNEL,
        token="fake",
        llm_healthy=True,
        schema_version=LATEST_SCHEMA,
        latest_schema=LATEST_SCHEMA,
        enabled=True,
    )
    defaults.update(overrides)
    return LiveReadiness(**defaults)


LATEST_SCHEMA = LATEST_VERSION


class _Backups:
    def __init__(self, rows):
        self._rows = rows

    def recent(self, *, limit=1):
        return self._rows[:limit]


def _named(report: LiveReport, name: str) -> Check:
    return next(check for check in report.checks if check.name == name)


# =============================================================================
# The gate opens, once, when everything is true.
# =============================================================================


def test_everything_satisfied_is_ready() -> None:
    report = _gate().check()

    assert report.ready, report.describe()
    assert report.blockers == ()


# =============================================================================
# And stays shut otherwise.
# =============================================================================


def test_an_unborn_yui_cannot_go_live() -> None:
    """Phase 13's gate, restated here so the go-live report explains itself
    rather than failing obscurely."""

    class Pending:
        def status(self):
            return "PENDING"

    report = _gate(first_boot=Pending()).check()

    assert not report.ready
    assert _named(report, "first_boot_complete").blocks


@pytest.mark.parametrize(
    "status", ["PENDING", "READY", "RUNNING", "PAUSED", "BLOCKED_RETRYABLE", "AUDITING"]
)
def test_no_first_boot_status_except_complete_opens_it(status) -> None:
    class Stuck:
        def status(self):
            return status

    assert not _gate(first_boot=Stuck()).check().ready


def test_a_first_boot_that_cannot_be_read_keeps_it_shut() -> None:
    class Broken:
        def status(self):
            raise RuntimeError("the database is gone")

    report = _gate(first_boot=Broken()).check()

    assert not report.ready
    assert _named(report, "first_boot_complete").blocks


def test_going_live_needs_somebody_to_decide_it() -> None:
    """Everything else here can become true on its own — a Genesis finishes, a
    backup is taken, a contract is marked verified. This one cannot."""
    report = _gate(enabled=False).check()

    assert not report.ready
    assert _named(report, "live_enabled").blocks


def test_an_unfinished_capability_blocks_going_live() -> None:
    """§54: ``Implementation Ledger all required IDs E2E_VERIFIED``."""
    report = _gate(
        contracts={"normal_reply": _Verified(), "spontaneous_memory": _Unverified()}
    ).check()

    assert not report.ready
    check = _named(report, "capabilities_verified")
    assert check.blocks
    assert "spontaneous_memory" in check.detail


def test_missing_contracts_block_rather_than_pass() -> None:
    """Not being able to check is not the same as passing — the Phase 12
    lesson, applied to the last gate."""
    report = _gate(contracts=None).check()

    assert not report.ready
    assert _named(report, "capabilities_verified").blocks


def test_a_dead_subsystem_blocks_going_live() -> None:
    """§54: ``no required dead subsystem``. An opportunity kind that no builder
    claims is a source firing into nothing."""

    class Ticks:
        def recent(self, *, limit=50):
            return [{"unclaimed_kinds": "spontaneous_memory,diary_due"}]

    report = _gate(ticks=Ticks()).check()

    assert not report.ready
    check = _named(report, "no_dead_subsystem")
    assert check.blocks
    assert "spontaneous_memory" in check.detail


def test_no_owner_no_conversation() -> None:
    assert not _gate(owner_id="").check().ready
    assert not _gate(channel_id="").check().ready


def test_no_token_no_gateway() -> None:
    report = _gate(token=None).check()

    assert not report.ready
    assert _named(report, "discord_token").blocks


def test_an_old_schema_blocks_going_live() -> None:
    report = _gate(schema_version=LATEST_SCHEMA - 1).check()

    assert not report.ready
    assert _named(report, "schema_is_latest").blocks


# =============================================================================
# The check the phase exists for.
# =============================================================================


def test_a_live_capability_that_was_never_watched_blocks() -> None:
    """§47 → §50. Shadow mode costs nothing to enable and everything to ignore.

    A capability switched straight from OFF to LIVE has had exactly as much
    review as one that was never built, and the mode setting alone cannot tell
    the difference — so the evidence is the record.
    """

    class Nothing:
        def tally(self):
            return []

    report = _gate(
        shadow=ShadowController(modes={"proactive_contact": "LIVE"}),
        shadow_decisions=Nothing(),
    ).check()

    assert not report.ready
    assert _named(report, "shadow_evaluated:proactive_contact").blocks


def test_a_live_capability_that_was_watched_passes() -> None:
    class Watched:
        def tally(self):
            return [
                {
                    "capability": "proactive_contact",
                    "mode": "SHADOW",
                    "considered": 12,
                    "wanted": 3,
                    "acted": 0,
                    "last_at": "2026-01-01T09:00:00+00:00",
                }
            ]

    report = _gate(
        shadow=ShadowController(modes={"proactive_contact": "LIVE"}),
        shadow_decisions=Watched(),
    ).check()

    assert report.ready, report.describe()


def test_considered_is_not_evaluated() -> None:
    """A capability that was enabled and never once wanted to act has not been
    evaluated — it has merely been switched on."""

    class OnlyConsidered:
        def tally(self):
            return [
                {
                    "capability": "web_search",
                    "mode": "SHADOW",
                    "considered": 40,
                    "wanted": 0,
                    "acted": 0,
                    "last_at": "2026-01-01T09:00:00+00:00",
                }
            ]

    report = _gate(
        shadow=ShadowController(modes={"web_search": "LIVE"}),
        shadow_decisions=OnlyConsidered(),
    ).check()

    assert not report.ready
    assert _named(report, "shadow_evaluated:web_search").blocks


def test_shadowed_capabilities_need_no_evidence() -> None:
    """Nothing is live, so there is nothing to have watched first."""
    report = _gate(shadow=ShadowController(modes={})).check()

    assert report.ready
    assert _named(report, "shadow_evaluated").passed


def test_every_shadow_capability_is_covered_when_live() -> None:
    class Nothing:
        def tally(self):
            return []

    report = _gate(
        shadow=ShadowController(modes={name: "LIVE" for name in SHADOW_CAPABILITIES}),
        shadow_decisions=Nothing(),
    ).check()

    blocked = {check.name for check in report.blockers}
    assert blocked == {f"shadow_evaluated:{name}" for name in SHADOW_CAPABILITIES}


# =============================================================================
# Advisory: worth seeing, not worth refusing over.
# =============================================================================


def test_a_degraded_model_is_a_warning_not_a_refusal() -> None:
    """A bad first conversation, not an unsafe one — and refusing to start
    would make the failure harder to see rather than easier."""
    report = _gate(llm_healthy=False).check()

    assert report.ready
    assert _named(report, "model_reachable") in report.warnings


def test_no_backup_is_a_warning() -> None:
    report = _gate(backups=_Backups([])).check()

    assert report.ready
    assert _named(report, "backup_available") in report.warnings


# =============================================================================
# The wired application
# =============================================================================


def test_the_gate_answers_the_gateways_question(application) -> None:
    """The Discord gateway asks one object one question. Phase 15 makes the
    answer stricter, not plural."""
    assert hasattr(application.live, "character_plane_open")
    assert application.live.character_plane_open() is False  # not born yet


def test_a_born_yui_is_still_not_automatically_live(application) -> None:
    """Finishing a Genesis is not permission to talk to anyone.

    The most important negative test in the file: the one condition that takes
    nineteen years to satisfy is not, by itself, the answer.
    """
    mark_born(application)

    report = application.live.check()

    assert _named(report, "first_boot_complete").passed
    assert not report.ready
    assert application.live.character_plane_open() is False


def test_the_wired_gate_names_what_is_missing(application) -> None:
    mark_born(application)

    blockers = {check.name for check in application.live.check().blockers}

    # Two capabilities really are unfinished, and the gate says so rather than
    # rounding up to ready.
    assert "capabilities_verified" in blockers


def test_a_broken_gate_stays_shut() -> None:
    class Exploding:
        def status(self):
            raise RuntimeError("gone")

    gate = _gate(first_boot=Exploding())
    gate._shadow_decisions = None  # type: ignore[attr-defined]

    assert gate.character_plane_open() is False


def test_live_is_off_by_default(temp_config) -> None:
    """A fresh install does not talk to anybody."""
    assert temp_config.runtime.live is False


# =============================================================================
# Observability
# =============================================================================


async def test_the_live_view_is_wired(application) -> None:
    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} live", author_id=OWNER, channel_id=CHANNEL
    )

    assert not outcome.result.failed, outcome.result.error
    rows = [row for section in outcome.result.sections for row in section.rows]
    assert any(row["check"] == "first_boot_complete" for row in rows)


async def test_the_live_view_changes_nothing(application) -> None:
    before = (application.event_store.count(), application.shadow_decisions.count())

    for _ in range(3):
        await application.admin_router.route(
            text=f"{ADMIN_PREFIX} live", author_id=OWNER, channel_id=CHANNEL
        )

    assert (
        application.event_store.count(),
        application.shadow_decisions.count(),
    ) == before


def test_the_admin_plane_cannot_go_live() -> None:
    """Throwing the switch is a configuration change on the machine. A chat
    message that could start the character plane is a chat message that could
    start it by accident."""
    from app.admin.commands import REGISTRY

    assert REGISTRY["live"].is_read_only
    for key in REGISTRY:
        assert not key.startswith("live on")
        assert not key.startswith("live enable")


def test_the_live_check_command_runs(temp_config, capsys) -> None:
    from app import main

    settings = temp_config.root_dir / "config" / "settings.yaml"
    argv = ["--config", str(settings), "--root", str(temp_config.root_dir)]
    main.main([*argv, "migrate"])
    capsys.readouterr()

    code = main.main([*argv, "live-check"])

    output = capsys.readouterr().out
    assert "NOT READY" in output
    assert "first_boot_complete" in output
    # Non-zero so it is usable as a gate in whatever starts the process.
    assert code == 1
