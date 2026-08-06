"""INVARIANT: a checkpoint records that work finished, not that it is still true.

Five findings with one shape between them: something that *recorded* an answer
was read as if it were the answer.

    the most recently started Genesis run was read as "the life she is living".
    FIRST BOOT is what attaches a life, and a later run can exist that it never
    pointed at — so a stale authoritative life passed because a newer row was
    sitting beside it in the table.

    a non-empty `final_summary` was read as "this year is synthesised". The
    fast path returned before a single month had been looked at, so a year
    whose months no longer verify reported itself done.

    `validate_interaction` was read as "this could have happened in Genesis".
    It answers that question about the present, where YUI and the USER are in
    fact talking, and `shared_communication` between them is correct — and
    those nineteen years are before the conversation existed.

    a permissive metadata reader was read as "nobody was involved". A
    participants blob that will not parse came back as `()`, which is the
    answer a hand-edited row gets for free, and the final identity audit —
    the last thing between a life and FIRST_BOOT_COMPLETE — passed it.

    persisted prose was read as "context". A stored month or summary that no
    longer verifies was handed to the next generation call, whose output
    carries the current stamp.

So every read of persisted Genesis data is revalidated at the point of use,
including on the paths that already ran, and the authoritative life is the one
FIRST BOOT names. `CURRENT_WORLD_MODEL_VERSION` goes to 3 because several of
these turn what used to pass into a refusal: a life judged under v2 was judged
by rules that no longer hold, and re-judging it would mean reading its prose.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.genesis.anchors import LifeAnchors
from app.genesis.models import AnnualSynthesis
from app.genesis.world_gate import (
    stored_summary_refusal,
    validate_genesis_month,
    validate_genesis_summary,
    validate_genesis_world,
)
from app.storage.repositories.firstboot import FirstBootRepository
from app.storage.repositories.genesis import (
    GenesisRunRepository,
    LifeRecordRepository,
)
from app.storage.repositories.rebuild import RebuildEpochRepository
from app.storage.repositories.world_provenance import read_world_provenance
from app.world.scope import CURRENT_WORLD_MODEL_VERSION

pytestmark = pytest.mark.invariant

BIRTH = datetime(2007, 3, 14, tzinfo=timezone.utc)


# =============================================================================
# Fixtures and doubles
# =============================================================================


@pytest.fixture
def application(temp_config, clock):
    """The production composition, so a wiring regression surfaces here."""
    from app.bootstrap import Application

    built = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        yield built
    finally:
        built.db.close()


class Counting:
    """A structured client that records every call and answers nothing.

    Most assertions here are "the model was never reached", and a double that
    counts is what makes the difference between a gate that refused and a gate
    that generated and then discarded.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, schema, messages, *, purpose="", **kwargs):
        self.calls.append(purpose)
        raise AssertionError(f"unverified data reached the model: {purpose}")


class Answering(Counting):
    """Counts, and returns whatever it was told to for each purpose."""

    def __init__(self, answers: dict) -> None:
        super().__init__()
        self._answers = answers

    async def generate(self, schema, messages, *, purpose="", **kwargs):
        self.calls.append(purpose)

        class Outcome:
            ok = True
            value = self._answers.get(purpose)

        return Outcome()


def _runner(application, structured):
    """A runner over the real repositories, with a countable model."""
    from app.genesis.runner import GenesisRunner

    return GenesisRunner(
        runs=application.genesis_runs,
        records=application.life_records,
        entities=application.life_entities,
        audits=application.generation_audits,
        experiences=application.genesis_experiences,
        structured=structured,
        prompts=application.prompts,
        clock=application.clock,
    )


def _seed_year(
    application,
    *,
    months: int = 12,
    participants: tuple[str, ...] = ("npc",),
    summary: str = "",
) -> tuple[str, str]:
    """A run with one fully written, currently-valid year. `(run_id, year_id)`."""
    now = application.clock.now()
    run_id = application.genesis_runs.start(
        birth=BIRTH, present=now, years=19, now=now
    )
    year_id = application.life_records.add_year(
        run_id=run_id,
        year_number=1,
        calendar_start=BIRTH,
        calendar_end=BIRTH.replace(year=2008),
        age_start=0,
        age_end=1,
        scaffold_text="生まれた年。",
        prompt_version="v1",
        model_version="test",
        now=now,
    )
    for number in range(1, months + 1):
        start = BIRTH + timedelta(days=30 * (number - 1))
        application.life_records.add_month(
            year_id=year_id,
            month_number=number,
            month_start=start,
            month_end=start + timedelta(days=30),
            age_start=0,
            age_end=0,
            narrative=f"{number}か月目。",
            importance_class="routine",
            prompt_version="v1",
            model_version="test",
            participants=participants,
            interaction_scope="local_to_subject_world",
        )
    if summary:
        application.life_records.synthesise(
            year_id,
            summary=summary,
            participants=participants,
            interaction_scope="local_to_subject_world",
        )
    return run_id, year_id


def _corrupt_month(application, year_id: str, month_number: int, **columns) -> None:
    """Write directly, the way a hand edit or an older version would have."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    application.db.execute(
        f"UPDATE life_months SET {assignments} WHERE year_id = ? AND month_number = ?",
        (*columns.values(), year_id, month_number),
    )


def _corrupt_summary(application, year_id: str, **columns) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    application.db.execute(
        f"UPDATE life_years SET {assignments} WHERE year_id = ?",
        (*columns.values(), year_id),
    )


async def _resume(application, structured, run_id: str):
    """One year of a resume, over data already on disk."""
    runner = _runner(application, structured)
    anchors = LifeAnchors(birth_datetime=BIRTH, present_datetime=application.clock.now())
    return await runner.run(anchors, resume=run_id, max_years=1)


def _codes(progress) -> set[str]:
    return {issue.code for issue in progress.blocked_by}


# =============================================================================
# 56. Which life is authoritative
# =============================================================================


class TestAuthoritativeRunSelection:
    """FIRST BOOT names the life. Nothing else gets to nominate one."""

    @staticmethod
    def _boot(db, run_id: str | None, *, clock) -> str:
        epoch_id = RebuildEpochRepository(db).record(
            started_at=clock.now(), reason="test"
        )
        boot = FirstBootRepository(db)
        boot.ensure(epoch_id)
        if run_id is not None:
            boot.attach_run(epoch_id, run_id)
        return epoch_id

    @staticmethod
    def _stale_run(db, run_id: str, *, clock, started_at: datetime) -> None:
        db.execute(
            "INSERT INTO genesis_runs (genesis_run_id, started_at, birth_datetime, "
            "present_datetime, years, world_model_version) VALUES (?, ?, ?, ?, 19, 0)",
            (
                run_id,
                started_at.isoformat(),
                BIRTH.isoformat(),
                clock.now().isoformat(),
            ),
        )

    def test_a_newer_current_run_does_not_rescue_a_stale_one(self, db, clock) -> None:
        """A. The reproduction.

        FIRST BOOT points at A, which is stale. B is newer and current and was
        never attached to anything — under the latest-run heuristic B answered
        the question, and the life she is actually living went unnoticed.
        """
        self._stale_run(db, "gen_a", clock=clock, started_at=clock.now())
        GenesisRunRepository(db).start(
            birth=BIRTH, present=clock.now(), years=19, now=clock.now() + timedelta(hours=1)
        )
        self._boot(db, "gen_a", clock=clock)

        run_id = FirstBootRepository(db).authoritative_genesis_run_id()
        report = read_world_provenance(db, authoritative_genesis_run_id=run_id)

        assert run_id == "gen_a"
        assert report.rebuild_required
        assert "genesis_run=1" in report.blocking

    def test_a_stale_run_beside_the_authoritative_one_is_history(
        self, db, clock
    ) -> None:
        """B. The other direction, and the reason scoping is not "count
        everything": a replaced life left rows behind, and blocking a fresh
        life over them means a reset can never succeed."""
        current = GenesisRunRepository(db).start(
            birth=BIRTH, present=clock.now(), years=19, now=clock.now()
        )
        self._stale_run(
            db, "gen_old", clock=clock, started_at=clock.now() - timedelta(days=30)
        )
        db.execute(
            "INSERT INTO life_years (year_id, genesis_run_id, year_number, "
            "calendar_start, calendar_end, age_start, age_end, created_at) "
            "VALUES ('lyr_old', 'gen_old', 1, ?, ?, 0, 1, ?)",
            (BIRTH.isoformat(), BIRTH.isoformat(), clock.now().isoformat()),
        )
        db.execute(
            "INSERT INTO life_months (month_id, year_id, month_number, month_start, "
            "month_end, age_start, age_end, narrative, world_model_version) "
            "VALUES ('lmo_old', 'lyr_old', 1, ?, ?, 0, 0, '前の人生', 0)",
            (BIRTH.isoformat(), BIRTH.isoformat()),
        )
        self._boot(db, current, clock=clock)

        run_id = FirstBootRepository(db).authoritative_genesis_run_id()
        report = read_world_provenance(db, authoritative_genesis_run_id=run_id)

        assert run_id == current
        assert not report.rebuild_required, report.blocking

    def test_a_run_first_boot_points_at_that_is_gone_blocks(self, db, clock) -> None:
        """C. Missing is not "nothing to check". FIRST BOOT says she is living
        a life that is not in the table."""
        self._boot(db, "gen_vanished", clock=clock)

        run_id = FirstBootRepository(db).authoritative_genesis_run_id()
        report = read_world_provenance(db, authoritative_genesis_run_id=run_id)

        assert run_id == "gen_vanished"
        assert report.rebuild_required
        assert "genesis_run=1" in report.blocking

    def test_no_attached_run_does_not_fall_back_to_the_latest(
        self, db, clock
    ) -> None:
        """D. A life that has not begun. `first_boot_complete` holds the door
        in that state, and going looking for a run to judge would re-introduce
        the heuristic under another name."""
        GenesisRunRepository(db).start(
            birth=BIRTH, present=clock.now(), years=19, now=clock.now()
        )
        self._boot(db, None, clock=clock)

        run_id = FirstBootRepository(db).authoritative_genesis_run_id()
        report = read_world_provenance(db, authoritative_genesis_run_id=run_id)

        assert run_id is None
        assert "genesis_run" not in report.counts
        assert "life_months" not in report.counts

    def test_no_first_boot_row_at_all_is_no_run(self, db, clock) -> None:
        """D, the earlier state: nothing has ever been recorded."""
        GenesisRunRepository(db).start(
            birth=BIRTH, present=clock.now(), years=19, now=clock.now()
        )

        assert FirstBootRepository(db).authoritative_genesis_run_id() is None

    def test_the_reader_has_no_way_to_choose_a_run(self) -> None:
        """Structural. The heuristic is gone rather than merely unused — an
        `ORDER BY started_at DESC` in this file is the finding coming back."""
        source = inspect.getsource(read_world_provenance)

        assert "FROM genesis_runs" in source
        # The run is looked up *by id*. Ranking runs in any way is the
        # heuristic, whatever it is renamed to.
        assert "WHERE genesis_run_id = ?" in source
        assert "FROM genesis_runs ORDER BY" not in source
        assert "latest()" not in source
        assert "authoritative_genesis_run_id" in source

    def test_the_application_wires_first_boot_to_the_reader(
        self, application
    ) -> None:
        """The gate is only as good as what feeds it."""
        import app.bootstrap as bootstrap

        source = inspect.getsource(bootstrap)

        assert "authoritative_genesis_run_id()" in source
        report = application.live._world_provenance()  # noqa: SLF001
        assert report.current == CURRENT_WORLD_MODEL_VERSION


# =============================================================================
# 57. A checkpoint is not a proof
# =============================================================================


class TestCheckpointDoesNotBypassValidation:
    """Every one of these has the checkpoint written. None of them proceed."""

    @staticmethod
    def _checkpoint(application, run_id: str, *names: str) -> None:
        for name in names:
            application.genesis_runs.checkpoint(
                run_id, name=name, year_number=1, now=application.clock.now()
            )

    async def test_year_critics_done_does_not_excuse_an_invalid_month(
        self, application
    ) -> None:
        """A. 「USERと会った」 stored under the current stamp. The critics ran
        once, under rules that permitted it."""
        run_id, year_id = _seed_year(application)
        _corrupt_month(application, year_id, 3, participants_json='["user"]')
        self._checkpoint(application, run_id, "year_months_done", "year_critics_done")
        structured = Counting()

        progress = await _resume(application, structured, run_id)

        assert "MONTH_WORLD_MODEL_UNVERIFIED" in _codes(progress)
        assert progress.stopped_at == 1
        assert structured.calls == []

    async def test_year_extraction_done_does_not_excuse_an_invalid_experience(
        self, application
    ) -> None:
        """B. The experience was staged before its metadata was required."""
        run_id, year_id = _seed_year(application)
        month = application.life_records.month(year_id, 1)
        application.db.execute(
            "INSERT INTO genesis_experiences (experience_id, genesis_run_id, "
            "month_id, year_number, sequence, occurred_at, actors, world_model_version) "
            "VALUES ('exp_old', ?, ?, 1, 0, ?, 'USER', 0)",
            (run_id, month["month_id"], BIRTH.isoformat()),
        )
        self._checkpoint(
            application,
            run_id,
            "year_months_done",
            "year_critics_done",
            "year_extraction_done",
        )
        structured = Counting()

        progress = await _resume(application, structured, run_id)

        assert "EXPERIENCE_WORLD_MODEL_UNVERIFIED" in _codes(progress)
        assert structured.calls == []

    async def test_year_replay_done_does_not_excuse_an_invalid_experience(
        self, application
    ) -> None:
        """C. Replayed once is not "may be replayed forward from"."""
        run_id, year_id = _seed_year(application)
        month = application.life_records.month(year_id, 1)
        application.db.execute(
            "INSERT INTO genesis_experiences (experience_id, genesis_run_id, "
            "month_id, year_number, sequence, occurred_at, actors, "
            "replay_status, world_model_version) "
            "VALUES ('exp_old', ?, ?, 1, 0, ?, 'USER', 'replayed', 0)",
            (run_id, month["month_id"], BIRTH.isoformat()),
        )
        self._checkpoint(
            application,
            run_id,
            "year_months_done",
            "year_critics_done",
            "year_extraction_done",
            "year_replay_done",
        )
        structured = Counting()

        progress = await _resume(application, structured, run_id)

        assert "EXPERIENCE_WORLD_MODEL_UNVERIFIED" in _codes(progress)
        assert structured.calls == []

    async def test_every_checkpoint_written_does_not_excuse_a_stale_month(
        self, application
    ) -> None:
        """D. The whole year marked done, top to bottom, and one month written
        before the world model existed."""
        run_id, year_id = _seed_year(application, summary="読んでばかりの一年。")
        _corrupt_month(application, year_id, 7, world_model_version=0)
        self._checkpoint(
            application,
            run_id,
            "year_months_done",
            "year_critics_done",
            "year_extraction_done",
            "year_replay_done",
            "year_memory_done",
        )
        structured = Counting()

        progress = await _resume(application, structured, run_id)

        assert "MONTH_WORLD_MODEL_UNVERIFIED" in _codes(progress)
        assert progress.stopped_at == 1
        assert structured.calls == []

    async def test_a_sound_year_still_resumes(self, application) -> None:
        """The gate has to let the ordinary case through, or it is just an
        outage. Nothing is regenerated and no model call is made."""
        run_id, year_id = _seed_year(application, summary="読んでばかりの一年。")
        self._checkpoint(
            application,
            run_id,
            "year_months_done",
            "year_critics_done",
            "year_extraction_done",
            "year_replay_done",
            "year_memory_done",
        )
        structured = Counting()

        progress = await _resume(application, structured, run_id)

        assert not progress.blocked_by, [i.reason for i in progress.blocked_by]
        assert progress.stopped_at is None
        assert structured.calls == []

    def test_the_gate_runs_before_the_first_checkpoint_branch(self) -> None:
        """Structural, because ordering is the entire finding. A revalidation
        placed after `reached(...)` is a revalidation the resume skips."""
        from app.genesis.runner import GenesisRunner

        source = inspect.getsource(GenesisRunner._year)  # noqa: SLF001

        assert source.index("_resume_state_is_sound(") < source.index("reached(")


# =============================================================================
# 58. Synthesis is a derivation, not a free write
# =============================================================================


class TestSynthesis:
    async def test_a_stale_month_stops_reuse_of_the_summary(
        self, application
    ) -> None:
        """A. The fast path used to return True here without a read."""
        run_id, year_id = _seed_year(application, summary="読んでばかりの一年。")
        _corrupt_month(application, year_id, 4, world_model_version=0)
        structured = Counting()
        runner = _runner(application, structured)

        done = await runner._synthesise(  # noqa: SLF001
            run_id, _anchors(application), _span(application), year_id
        )

        assert not done
        assert structured.calls == []

    async def test_a_user_month_stops_reuse_of_the_summary(
        self, application
    ) -> None:
        """B. Stamped current, and describing a conversation that had not
        happened yet — as `shared_communication`, which the present-day rule
        accepts and Genesis does not."""
        run_id, year_id = _seed_year(application, summary="読んでばかりの一年。")
        _corrupt_month(
            application,
            year_id,
            4,
            participants_json='["user"]',
            interaction_scope="shared_communication",
        )
        structured = Counting()
        runner = _runner(application, structured)

        done = await runner._synthesise(  # noqa: SLF001
            run_id, _anchors(application), _span(application), year_id
        )

        assert not done
        assert structured.calls == []

    async def test_valid_months_and_a_valid_summary_are_reused(
        self, application
    ) -> None:
        """C. A verified synthesis is never rewritten by a resume."""
        run_id, year_id = _seed_year(application, summary="読んでばかりの一年。")
        structured = Counting()
        runner = _runner(application, structured)

        done = await runner._synthesise(  # noqa: SLF001
            run_id, _anchors(application), _span(application), year_id
        )

        assert done
        assert structured.calls == []
        row = application.life_records.year(run_id, 1)
        assert row["final_summary"] == "読んでばかりの一年。"

    async def test_a_summary_that_introduces_the_user_is_not_saved(
        self, application
    ) -> None:
        """D. The second model call is where a person from another world gets
        added to a year whose months never had one."""
        run_id, year_id = _seed_year(application)
        structured = Answering(
            {
                "genesis_annual_synthesis": AnnualSynthesis(
                    summary="USERと話した一年。",
                    participants=("user",),
                    interaction_scope="shared_communication",
                )
            }
        )
        runner = _runner(application, structured)

        done = await runner._synthesise(  # noqa: SLF001
            run_id, _anchors(application), _span(application), year_id
        )

        assert not done
        row = application.life_records.year(run_id, 1)
        assert not row["final_summary"], "an invalid summary was written and kept"

    async def test_a_summary_may_not_introduce_anyone_the_months_lacked(
        self, application
    ) -> None:
        """E. Not only the USER. A compression that adds a participant is not
        a compression, and `world` is otherwise a perfectly ordinary value."""
        run_id, year_id = _seed_year(application, participants=())
        structured = Answering(
            {
                "genesis_annual_synthesis": AnnualSynthesis(
                    summary="誰かと過ごした一年。",
                    participants=("npc",),
                    interaction_scope="local_to_subject_world",
                )
            }
        )
        runner = _runner(application, structured)

        done = await runner._synthesise(  # noqa: SLF001
            run_id, _anchors(application), _span(application), year_id
        )

        assert not done
        assert not application.life_records.year(run_id, 1)["final_summary"]

    async def test_a_valid_summary_is_stamped_by_the_write_that_stores_it(
        self, application
    ) -> None:
        """F. One statement. A version added by a later UPDATE leaves a window
        in which the summary exists without provenance, and a crash inside that
        window leaves exactly the row this guards against."""
        run_id, year_id = _seed_year(application)
        structured = Answering(
            {
                "genesis_annual_synthesis": AnnualSynthesis(
                    summary="ミカと過ごした一年。",
                    participants=("npc",),
                    interaction_scope="local_to_subject_world",
                )
            }
        )
        runner = _runner(application, structured)

        done = await runner._synthesise(  # noqa: SLF001
            run_id, _anchors(application), _span(application), year_id
        )

        assert done
        row = application.life_records.year(run_id, 1)
        assert row["final_summary"] == "ミカと過ごした一年。"
        assert row["final_summary_world_model_version"] == CURRENT_WORLD_MODEL_VERSION
        assert json.loads(row["final_summary_participants_json"]) == ["npc"]
        assert row["final_summary_interaction_scope"] == "local_to_subject_world"

        source = inspect.getsource(LifeRecordRepository.synthesise)
        assert source.count("self._db.execute(") == 1

    async def test_a_summary_whose_metadata_is_malformed_is_not_reused(
        self, application
    ) -> None:
        """G. After a restart the only thing left is the row, and a blob that
        will not parse is not an empty blob."""
        run_id, year_id = _seed_year(application, summary="読んでばかりの一年。")
        _corrupt_summary(application, year_id, final_summary_participants_json="{oops")
        structured = Counting()
        runner = _runner(application, structured)

        done = await runner._synthesise(  # noqa: SLF001
            run_id, _anchors(application), _span(application), year_id
        )

        assert not done
        assert structured.calls == []

    def test_the_validator_is_one_function(self) -> None:
        """The USER ban copied into seven call sites is seven places to drift."""
        assert validate_genesis_summary(
            participants=("user",), interaction_scope="shared_communication"
        ).startswith("first_boot_boundary")
        assert (
            validate_genesis_summary(
                participants=("npc",),
                interaction_scope="local_to_subject_world",
                month_participants=("npc", "world"),
            )
            == ""
        )
        assert validate_genesis_summary(
            participants=("world",),
            interaction_scope="local_to_subject_world",
            month_participants=("npc",),
        ) == "summary_participant_not_in_months:world"

    def test_the_schema_requires_the_metadata(self) -> None:
        """A default would mean a model that omitted them looked like a model
        that said "nobody", which is the permissive answer."""
        for name in ("participants", "interaction_scope"):
            assert AnnualSynthesis.model_fields[name].is_required(), name

    def test_the_prompt_asks_for_them(self, application) -> None:
        """In the call that already exists. A second call per year is 19 more
        model calls for metadata the summariser already knows."""
        template = application.prompts.get("genesis_annual_synthesis")

        assert template.prompt_version.endswith("v2")
        assert "participants" in template.body
        assert "interaction_scope" in template.body
        assert "USER" in template.body


def _anchors(application) -> LifeAnchors:
    return LifeAnchors(
        birth_datetime=BIRTH, present_datetime=application.clock.now()
    )


def _span(application):
    return _anchors(application).years[0]


# =============================================================================
# 59. The final identity audit
# =============================================================================


class TestIdentityAudit:
    """The last thing between a generated life and FIRST_BOOT_COMPLETE."""

    def test_malformed_participants_fail_the_audit(self, application) -> None:
        """A. It used to read them with a helper that answered a corrupt list
        with "nobody", so a row with harmless prose and unreadable metadata
        passed the one check that exists to catch it."""
        run_id, year_id = _seed_year(application, months=1)
        _corrupt_month(application, year_id, 1, participants_json="{oops")
        runner = _runner(application, Counting())

        result = runner._audit_identity(run_id)  # noqa: SLF001

        assert not result.passed
        assert "month_world_metadata_unreadable" in result.detail

    def test_a_user_month_fails_the_audit(self, application) -> None:
        """B. Both scopes. `shared_communication` is the one the general rule
        allows, and it is a conversation that had not happened yet."""
        run_id, year_id = _seed_year(application, months=1)
        _corrupt_month(
            application,
            year_id,
            1,
            participants_json='["user"]',
            interaction_scope="shared_communication",
        )
        runner = _runner(application, Counting())

        result = runner._audit_identity(run_id)  # noqa: SLF001

        assert not result.passed
        assert "first_boot_boundary:user" in result.detail

    def test_a_valid_month_reaches_the_ordinary_critic(self, application) -> None:
        """C. The strict gate is a precondition, not a replacement — the
        content check still runs on what gets past it."""
        run_id, _ = _seed_year(application, months=2)
        runner = _runner(application, Counting())

        result = runner._audit_identity(run_id)  # noqa: SLF001

        assert result.passed
        assert "2 months" in result.detail

    def test_the_audit_reads_strictly(self) -> None:
        """Structural: the permissive reader must not come back here."""
        from app.genesis.runner import GenesisRunner

        source = inspect.getsource(GenesisRunner._audit_identity)  # noqa: SLF001

        assert "_month_refusal(" in source
        assert "_stored_participants(" not in source


# =============================================================================
# 60. Context is data, and data is revalidated
# =============================================================================


class TestPreviousContext:
    async def test_a_stale_previous_month_stops_the_next_one(
        self, application
    ) -> None:
        """A. Zero model calls. The next month's prose would carry the current
        stamp, and its premise would be a month nothing checked."""
        run_id, year_id = _seed_year(application, months=1)
        _corrupt_month(application, year_id, 1, world_model_version=0)
        structured = Counting()
        runner = _runner(application, structured)
        year = application.life_records.year(run_id, 1)
        progress = _progress(run_id)

        await runner._month(  # noqa: SLF001
            run_id,
            _anchors(application),
            year,
            2,
            BIRTH,
            BIRTH + timedelta(days=30),
            0,
            _ledger(runner, run_id),
            progress,
        )

        assert "MONTH_CONTEXT_WORLD_MODEL_UNVERIFIED" in _codes(progress)
        assert structured.calls == []

    async def test_a_malformed_previous_month_stops_the_next_one(
        self, application
    ) -> None:
        """B. Stamped current, unreadable metadata — the hand-edited shape."""
        run_id, year_id = _seed_year(application, months=1)
        _corrupt_month(application, year_id, 1, participants_json="{oops")
        structured = Counting()
        runner = _runner(application, structured)
        year = application.life_records.year(run_id, 1)
        progress = _progress(run_id)

        await runner._month(  # noqa: SLF001
            run_id,
            _anchors(application),
            year,
            2,
            BIRTH,
            BIRTH + timedelta(days=30),
            0,
            _ledger(runner, run_id),
            progress,
        )

        assert "MONTH_CONTEXT_WORLD_MODEL_UNVERIFIED" in _codes(progress)
        assert structured.calls == []

    async def test_an_unverified_previous_summary_does_not_become_context(
        self, application
    ) -> None:
        """C. And is not quietly replaced by the scaffold either: that would
        swap a verified summary for a guess and generate the next year from
        it, which is the same year built on a different lie."""
        run_id, year_id = _seed_year(application, summary="読んでばかりの一年。")
        _corrupt_summary(application, year_id, final_summary_world_model_version=0)
        structured = Counting()
        runner = _runner(application, structured)

        assert runner._previous_year_text(run_id, 2) is None  # noqa: SLF001

        progress = _progress(run_id)
        await runner._scaffold(  # noqa: SLF001
            run_id,
            _anchors(application),
            _anchors(application).years[1],
            _ledger(runner, run_id),
            progress,
        )

        assert "YEAR_CONTEXT_WORLD_MODEL_UNVERIFIED" in _codes(progress)
        assert structured.calls == []

    async def test_valid_context_reads_normally(self, application) -> None:
        """D. The ordinary resume. Both readers return the stored text."""
        run_id, year_id = _seed_year(application, summary="読んでばかりの一年。")
        runner = _runner(application, Counting())
        year = application.life_records.year(run_id, 1)

        assert runner._previous_year_text(run_id, 2) == "読んでばかりの一年。"  # noqa: SLF001
        assert runner._previous_month_text(run_id, year, 5) == "4か月目。"  # noqa: SLF001

    def test_a_year_without_a_summary_still_uses_its_scaffold(
        self, application
    ) -> None:
        """The existing behaviour, unchanged. `None` means "this did not
        verify"; a year that was simply never synthesised is a different
        state and still reads as its provisional sketch."""
        run_id, _ = _seed_year(application)
        runner = _runner(application, Counting())

        assert runner._previous_year_text(run_id, 2) == "生まれた年。"  # noqa: SLF001


def _progress(run_id: str):
    from app.genesis.runner import GenesisProgress

    return GenesisProgress(run_id=run_id)


def _ledger(runner, run_id: str):
    from app.genesis.ledger import ContinuityLedger

    return ContinuityLedger(runner._entities, genesis_run_id=run_id)  # noqa: SLF001


# =============================================================================
# 61. The version bump
# =============================================================================


class TestVersionBump:
    def test_v2_data_is_stale_under_the_new_code(self, db, clock) -> None:
        """A. Several of this round's changes turn what used to pass into a
        refusal, so a life judged under v2 was judged by rules that no longer
        hold — and re-judging it would mean reading its prose."""
        assert CURRENT_WORLD_MODEL_VERSION == 3

        RebuildEpochRepository(db).record(started_at=clock.now(), reason="v2 era")
        db.execute("UPDATE rebuild_epochs SET world_model_version = 2")
        db.execute(
            "INSERT INTO genesis_runs (genesis_run_id, started_at, birth_datetime, "
            "present_datetime, years, world_model_version) "
            "VALUES ('gen_v2', ?, ?, ?, 19, 2)",
            (clock.now().isoformat(), BIRTH.isoformat(), clock.now().isoformat()),
        )

        report = read_world_provenance(db, authoritative_genesis_run_id="gen_v2")

        assert report.rebuild_required
        assert "rebuild_epoch=1" in report.blocking
        assert "genesis_run=1" in report.blocking

    def test_a_fresh_life_is_written_at_version_three(self, application) -> None:
        """B. Epoch, run and month, all stamped by the code that ran the
        rules — the only thing that can honestly stamp them."""
        epoch_id = RebuildEpochRepository(application.db).record(
            started_at=application.clock.now(), reason="fresh"
        )
        run_id, year_id = _seed_year(application)
        FirstBootRepository(application.db).ensure(epoch_id)
        FirstBootRepository(application.db).attach_run(epoch_id, run_id)

        epoch = application.db.query_one(
            "SELECT world_model_version FROM rebuild_epochs WHERE epoch_id = ?",
            (epoch_id,),
        )
        run = application.genesis_runs.get(run_id)
        month = application.life_records.month(year_id, 1)

        assert epoch["world_model_version"] == 3
        assert run["world_model_version"] == 3
        assert month["world_model_version"] == 3
        assert not read_world_provenance(
            application.db,
            authoritative_genesis_run_id=(
                FirstBootRepository(application.db).authoritative_genesis_run_id()
            ),
        ).rebuild_required

    def test_migration_39_does_not_upgrade_anything_semantic(
        self, tmp_path, clock
    ) -> None:
        """C. It adds three columns and stamps nothing. A migration cannot know
        who was in a year — inferring that from Japanese prose is the exact
        structure the world model replaced."""
        from app.storage.database import Database
        from app.storage.migrations import MIGRATIONS, migrate

        db = Database(tmp_path / "at38.db", synchronous="OFF")
        db.connect()
        try:
            migrate(db, clock=clock, target_version=38)
            db.execute(
                "INSERT INTO genesis_runs (genesis_run_id, started_at, "
                "birth_datetime, present_datetime, years, world_model_version) "
                "VALUES ('gen_x', ?, ?, ?, 19, 2)",
                (clock.now().isoformat(), BIRTH.isoformat(), clock.now().isoformat()),
            )
            db.execute(
                "INSERT INTO life_years (year_id, genesis_run_id, year_number, "
                "calendar_start, calendar_end, age_start, age_end, created_at, "
                "final_summary) VALUES ('lyr_x', 'gen_x', 1, ?, ?, 0, 1, ?, "
                "'ミカとよく遊んだ一年。')",
                (BIRTH.isoformat(), BIRTH.isoformat(), clock.now().isoformat()),
            )

            result = migrate(db, clock=clock)

            assert result.applied == (39,)
            row = db.query_one("SELECT * FROM life_years WHERE year_id = 'lyr_x'")
            # The prose survives; nothing pretends to have read it.
            assert row["final_summary"] == "ミカとよく遊んだ一年。"
            assert row["final_summary_world_model_version"] == 0
            assert row["final_summary_participants_json"] == "[]"
            assert row["final_summary_interaction_scope"] == ""
            assert stored_summary_refusal(row) == "summary_world_model_unverified"
            # And the run it belongs to is not quietly promoted either.
            assert (
                db.query_one(
                    "SELECT world_model_version FROM genesis_runs "
                    "WHERE genesis_run_id = 'gen_x'"
                )["world_model_version"]
                == 2
            )
        finally:
            db.close()

        body = inspect.getsource(MIGRATIONS[-1].__class__)  # sanity: it is a dataclass
        assert body


# =============================================================================
# The Genesis USER ban, stated once
# =============================================================================


class TestGenesisUserBan:
    """Scope-independent, and separate from the general world rule on purpose.

    `validate_interaction` is right about the present: they are talking, so
    `shared_communication` between them is exactly what is possible. What makes
    it impossible in Genesis is *when*, not who.
    """

    @pytest.mark.parametrize(
        "scope",
        ["local_to_subject_world", "shared_communication", "cross_world_physical"],
    )
    def test_the_user_is_refused_under_every_scope(self, scope) -> None:
        assert validate_genesis_world(
            participants=("user",), interaction_scope=scope
        ).startswith("first_boot_boundary")

    def test_the_general_rule_still_allows_the_present(self) -> None:
        """The two answers differ, which is why there are two functions."""
        from app.world.scope import validate_interaction

        assert (
            validate_interaction(
                subject="yui",
                participants=("user",),
                scope="shared_communication",
            )
            == ""
        )

    def test_her_neighbours_are_not_affected(self) -> None:
        assert (
            validate_genesis_world(
                participants=("npc", "world"),
                interaction_scope="local_to_subject_world",
            )
            == ""
        )

    def test_a_month_is_judged_by_the_same_function(self) -> None:
        from app.genesis.runner import _month_refusal

        assert _month_refusal is validate_genesis_month
