"""INVARIANT: nothing counts as verified because a migration gave it columns.

Three findings with one cause. `interaction_scope` and `participants` became
required, and a claim omitting them fails closed — but three older paths reach
the same resolver without ever having been asked:

    a Common Ground row from before the world model. `StoredSemanticClaim`
    fills the missing fields with `local_to_subject_world` and no participants,
    so 「USERと同じ部屋で本を読んだ」 came back as an ordinary local claim and
    resolved against her own activity row.

    a life month from before it. Migration 36 gave every existing row
    `participants=[]` and `interaction_scope=local` — structurally identical to
    a month the current validator had passed, and produced by a substring
    critic that a paraphrase walks straight past.

    an experience extracted *after* a month was validated. A second model call
    produced it, and it carried no world metadata at all; `actors=["USER"]` was
    accepted and staged.

In each case the row looked verified because the columns existed. So provenance
is recorded by the code that ran the rules — never inferred, never backfilled,
because inferring what an old row meant would mean reading its prose, which is
the structure the world model replaced. The way out of a stale life is an
explicit rebuild, and that is a fact the system states rather than something an
operator remembers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.clock import FixedClock
from app.storage.database import Database
from app.storage.migrations import LATEST_VERSION, migrate, schema_version
from app.storage.repositories.world_provenance import read_world_provenance
from app.world.scope import (
    CURRENT_WORLD_MODEL_VERSION,
    UNVERIFIED_WORLD_MODEL_VERSION,
    is_current_world_model,
)

pytestmark = pytest.mark.invariant


@pytest.fixture
def application(temp_config, clock):
    """The production composition, so wiring regressions surface here."""
    from app.bootstrap import Application

    built = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        yield built
    finally:
        built.db.close()


# =============================================================================
# The version itself
# =============================================================================


class TestWorldModelVersion:
    def test_there_is_one_definition(self) -> None:
        """Every subsystem reads the same constant. A number copied into four
        modules is four numbers as soon as one of them is edited."""
        import app.conversation.common_ground as conversation
        import app.storage.repositories.common_ground as claims
        import app.storage.repositories.genesis as genesis
        import app.storage.repositories.rebuild as rebuild

        for module in (claims, genesis, rebuild):
            assert (
                module.CURRENT_WORLD_MODEL_VERSION is CURRENT_WORLD_MODEL_VERSION
            ), module.__name__
        assert conversation.is_current_world_model is is_current_world_model

    def test_it_is_not_the_schema_version(self) -> None:
        """A migration adds columns. It cannot add meaning, and conflating the
        two is exactly how a migrated row started looking verified."""
        assert CURRENT_WORLD_MODEL_VERSION != LATEST_VERSION

    def test_absence_is_unverified(self) -> None:
        assert UNVERIFIED_WORLD_MODEL_VERSION == 0
        assert not is_current_world_model(0)
        assert not is_current_world_model(None)
        assert not is_current_world_model(1)
        assert is_current_world_model(CURRENT_WORLD_MODEL_VERSION)


# =============================================================================
# Migration 37
# =============================================================================


def _at(tmp_path, clock, version: int, name: str) -> Database:
    db = Database(tmp_path / name, synchronous="OFF")
    db.connect()
    migrate(db, clock=clock, target_version=version)
    return db


class TestMigration37:
    def test_a_fresh_database_is_at_the_latest(self, db: Database) -> None:
        assert schema_version(db) == LATEST_VERSION == 38

    @pytest.mark.parametrize(
        ("table", "column"),
        [
            ("rebuild_epochs", "world_model_version"),
            ("common_ground_claims", "world_model_version"),
            ("life_months", "world_model_version"),
            ("genesis_experiences", "world_model_version"),
            ("genesis_experiences", "participants_json"),
            ("genesis_experiences", "interaction_scope"),
            ("genesis_experiences", "actor_subjects_json"),
        ],
    )
    def test_the_provenance_columns_exist(self, db, table, column) -> None:
        columns = {row["name"] for row in db.query_all(f"PRAGMA table_info({table})")}

        assert column in columns

    def test_a_schema_36_database_upgrades(self, tmp_path, clock) -> None:
        db = _at(tmp_path, clock, 36, "at36.db")
        try:
            result = migrate(db, clock=clock)

            assert result.applied == (37, 38)
            assert schema_version(db) == LATEST_VERSION
        finally:
            db.close()

    def test_nothing_is_backfilled(self, tmp_path, clock) -> None:
        """The audit's reproduction, verbatim.

        A month written under schema 35 whose prose the old critic missed.
        Migration 36 gives it the metadata of a verified month; migration 37
        must not also give it the *provenance* of one, because nothing checked
        it and nothing safely can.
        """
        db = _at(tmp_path, clock, 35, "at35.db")
        try:
            _seed_month(db, "君と映画を見に行った")

            migrate(db, clock=clock)

            row = db.query_one("SELECT * FROM life_months LIMIT 1")
            # Migration 36's defaults: indistinguishable from a verified month.
            assert row["participants_json"] == "[]"
            assert row["interaction_scope"] == "local_to_subject_world"
            # And the thing that tells them apart.
            assert row["world_model_version"] == UNVERIFIED_WORLD_MODEL_VERSION
            assert not is_current_world_model(row["world_model_version"])
        finally:
            db.close()

    def test_an_old_life_is_reported_as_needing_a_rebuild(
        self, tmp_path, clock
    ) -> None:
        db = _at(tmp_path, clock, 35, "stale.db")
        try:
            _seed_month(db, "君と映画を見に行った")
            migrate(db, clock=clock)

            report = read_world_provenance(db)

            assert report.rebuild_required
            assert any(item.startswith("life_months=") for item in report.stale)
        finally:
            db.close()

    def test_a_fresh_database_needs_nothing(self, db: Database) -> None:
        report = read_world_provenance(db)

        assert not report.rebuild_required
        assert report.current == CURRENT_WORLD_MODEL_VERSION


def _seed_month(db: Database, narrative: str) -> None:
    """A life month as schema 35 wrote them — prose, and nothing else."""
    db.execute(
        "INSERT INTO genesis_runs (genesis_run_id, started_at, birth_datetime, "
        "present_datetime, years) VALUES ('gen_x', ?, ?, ?, 19)",
        ("2026-01-01T00:00:00+00:00", "2007-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    db.execute(
        "INSERT INTO life_years (year_id, genesis_run_id, year_number, "
        "calendar_start, calendar_end, age_start, age_end, created_at) "
        "VALUES ('lyr_x', 'gen_x', 1, ?, ?, 0, 1, ?)",
        (
            "2007-01-01T00:00:00+00:00",
            "2008-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    db.execute(
        "INSERT INTO life_months (month_id, year_id, month_number, month_start, "
        "month_end, age_start, age_end, narrative) "
        "VALUES ('lmo_x', 'lyr_x', 1, ?, ?, 0, 0, ?)",
        ("2007-01-01T00:00:00+00:00", "2007-02-01T00:00:00+00:00", narrative),
    )


# =============================================================================
# The rebuild epoch
# =============================================================================


class TestRebuildEpochProvenance:
    def test_an_epoch_this_code_wrote_carries_the_version(self, db, clock) -> None:
        from app.storage.repositories.rebuild import RebuildEpochRepository

        repository = RebuildEpochRepository(db)
        repository.record(started_at=clock.now(), reason="test")

        row = db.query_one("SELECT * FROM rebuild_epochs LIMIT 1")
        assert row["world_model_version"] == CURRENT_WORLD_MODEL_VERSION

    def test_a_migrated_epoch_does_not(self, tmp_path, clock) -> None:
        db = _at(tmp_path, clock, 36, "epoch36.db")
        try:
            db.execute(
                "INSERT INTO rebuild_epochs (epoch_id, started_at, reason) "
                "VALUES ('epo_old', ?, 'before the world model')",
                ("2026-01-01T00:00:00+00:00",),
            )
            migrate(db, clock=clock)

            report = read_world_provenance(db)

            assert report.rebuild_required
            assert "rebuild_epoch=1" in report.stale
        finally:
            db.close()

    def test_the_readiness_gate_blocks_on_it(self, db, clock) -> None:
        """Named, so the go-live report explains itself rather than failing
        obscurely — and the name says what to do about it."""
        from app.live.readiness import LiveReadiness

        class Born:
            def status(self):
                return "COMPLETE"

        db.execute(
            "INSERT INTO rebuild_epochs (epoch_id, started_at, reason) "
            "VALUES ('epo_old', ?, 'before the world model')",
            ("2026-01-01T00:00:00+00:00",),
        )
        gate = LiveReadiness(
            first_boot=Born(),
            shadow=None,
            shadow_decisions=None,
            world_provenance=lambda: read_world_provenance(db),
        )

        check = next(
            item for item in gate.check().checks if item.name == "world_model_current"
        )

        assert not check.passed
        assert "rebuild_required_world_model_version" in check.detail

    def test_a_current_epoch_passes(self, db, clock) -> None:
        from app.live.readiness import LiveReadiness
        from app.storage.repositories.rebuild import RebuildEpochRepository

        RebuildEpochRepository(db).record(started_at=clock.now(), reason="fresh")

        class Born:
            def status(self):
                return "COMPLETE"

        gate = LiveReadiness(
            first_boot=Born(),
            shadow=None,
            shadow_decisions=None,
            world_provenance=lambda: read_world_provenance(db),
        )

        check = next(
            item for item in gate.check().checks if item.name == "world_model_current"
        )
        assert check.passed

    def test_nothing_is_deleted_automatically(self, tmp_path, clock) -> None:
        """A stale life is refused as authority, not erased.

        The old rows are still worth reading — forensics, debugging, an owner
        looking at what happened. Deleting them on startup would destroy the
        only record of the thing that went wrong.
        """
        db = _at(tmp_path, clock, 35, "kept.db")
        try:
            _seed_month(db, "君と映画を見に行った")
            migrate(db, clock=clock)

            read_world_provenance(db)

            assert db.scalar("SELECT COUNT(*) FROM life_months") == 1
            row = db.query_one("SELECT narrative FROM life_months LIMIT 1")
            assert row["narrative"] == "君と映画を見に行った"
        finally:
            db.close()


# =============================================================================
# Common Ground: what a stored row may and may not do
# =============================================================================


class TestLegacyCommonGround:
    """A row written before the world model is a record of what was said.

    Not a fact that may be re-asserted. `StoredSemanticClaim` stays lenient —
    reading is not classifying, and refusing to parse an old blob would push
    the correction path back onto the regex extractor. What changed is that
    parsing it is no longer the same as trusting it.
    """

    @staticmethod
    def _claim(*, semantic: str = "", version: int = 0):
        from app.conversation.common_ground import CommonGroundClaim

        return CommonGroundClaim(
            claim_id="cgc_x",
            conversation_id="conv_x",
            kind="yui_completed_action",
            statement="USERと同じ部屋で本を読んだ",
            semantic_json=semantic,
            world_model_version=version,
        )

    def test_a_legacy_row_is_not_world_verified(self) -> None:
        assert not self._claim().world_verified

    def test_a_current_row_is(self) -> None:
        assert self._claim(version=CURRENT_WORLD_MODEL_VERSION).world_verified

    def test_the_stored_model_still_reads_an_old_blob(self) -> None:
        """The leniency that has to stay. An unreadable blob would mean the
        correction path cannot see what she said at all."""
        from app.dialogue.semantic_claims import StoredSemanticClaim

        stored = StoredSemanticClaim.model_validate(
            {
                "proposition": "YUIはUSERと同じ部屋で本を読んだ",
                "trigger": "本を読んだ",
                "subject": "yui",
                "category": "yui_completed_action",
            }
        )

        assert stored.interaction_scope == "local_to_subject_world"
        assert stored.participants == ()

    def test_the_permissive_defaults_are_not_an_answer(self) -> None:
        """And the reason it has to stay lenient is the reason it cannot be
        trusted: those defaults are what a cross-world claim needs to look
        local. The version is what settles it, not the blob."""
        blob = json.dumps(
            {
                "proposition": "YUIはUSERと同じ部屋で本を読んだ",
                "trigger": "本を読んだ",
                "subject": "yui",
                "category": "yui_completed_action",
            },
            ensure_ascii=False,
        )

        assert not self._claim(semantic=blob).world_verified
        assert self._claim(semantic=blob, version=CURRENT_WORLD_MODEL_VERSION).world_verified

    def test_a_legacy_statement_is_still_addressable(self) -> None:
        """The separation that matters: the row records that she said it.

        Losing that would take away the thing a correction attaches to — the
        USER saying 「前にそう言ったけど違うよ」 needs the statement to exist.
        """
        claim = self._claim()

        assert claim.statement == "USERと同じ部屋で本を読んだ"
        assert claim.claim_id == "cgc_x"

    def test_a_reviewed_row_is_stamped_on_write(self, db, clock) -> None:
        from app.storage.repositories.common_ground import CommonGroundRepository
        from app.storage.repositories.conversations import ConversationRepository

        conversation = ConversationRepository(db).ensure_conversation(
            channel_id="chan_x", channel_type="direct_message", now=clock.now()
        )
        repository = CommonGroundRepository(db)
        reviewed = repository.record(
            conversation_id=conversation.conversation_id,
            event_id=None,
            kind="yui_completed_action",
            statement="本を読んだ",
            status="provisional",
            source="yui_reviewed",
            confidence="high",
            evidence=(),
            semantic={"proposition": "YUIは本を読んだ"},
            subject="yui",
            modality="assertion",
            now=clock.now(),
        )
        legacy = repository.record(
            conversation_id=conversation.conversation_id,
            event_id=None,
            kind="yui_completed_action",
            statement="詩を詠んだ",
            status="provisional",
            source="claim_extractor",
            confidence="low",
            evidence=(),
            semantic=None,
            subject="",
            modality="",
            now=clock.now(),
        )

        assert reviewed.world_model_version == CURRENT_WORLD_MODEL_VERSION
        assert reviewed.world_verified
        assert legacy.world_model_version == UNVERIFIED_WORLD_MODEL_VERSION
        assert not legacy.world_verified

    def test_the_regex_extractor_is_not_the_fallback(self) -> None:
        """Structural. An unverified row must not get its truth from a reader
        nothing else in the system trusts — that is the second factual
        authority this subsystem exists to remove."""
        import inspect

        from app.conversation.common_ground import CommonGroundTracker

        source = inspect.getsource(CommonGroundTracker._still_supported)  # noqa: SLF001

        assert "self._guard.review" not in source


# =============================================================================
# Genesis experiences: a derivative gets checked like everything else
# =============================================================================


class TestExperienceBoundary:
    @staticmethod
    def _candidate(**overrides):
        from app.genesis.models import ExperienceCandidate

        base = dict(
            occurred_at=datetime(2008, 6, 1, tzinfo=timezone.utc),
            action="本を読んだ",
            actor_refs=(),
            participants=(),
            interaction_scope="local_to_subject_world",
        )
        base.update(overrides)
        return ExperienceCandidate(**base)

    def test_omitting_the_world_metadata_is_a_schema_failure(self) -> None:
        """A. The audit's finding was that this schema accepted anything."""
        import pydantic

        from app.genesis.models import ExperienceCandidate

        with pytest.raises(pydantic.ValidationError) as caught:
            ExperienceCandidate(
                occurred_at=datetime(2008, 6, 1, tzinfo=timezone.utc),
                action="本を読んだ",
            )

        missing = {error["loc"][0] for error in caught.value.errors()}
        assert {"actor_refs", "participants", "interaction_scope"} <= missing

    def test_the_free_text_actor_list_is_no_longer_the_only_word(self) -> None:
        """`actors=["USER"]` was a string nothing could see was a person from
        another world. The name stays free text; the role is checkable."""
        from app.genesis.models import ExperienceActor

        actor = ExperienceActor(name="ミカ", subject="npc")

        assert actor.name == "ミカ"
        assert actor.subject == "npc"

    @pytest.mark.parametrize(
        ("participants", "scope", "actors", "month", "refused"),
        [
            # B. the USER, in any capacity
            (("user",), "local_to_subject_world", (), ("npc",), "first_boot_boundary"),
            (("user",), "cross_world_physical", (), ("npc",), "first_boot_boundary"),
            # I. even through the one scope that spans the worlds elsewhere
            (("user",), "shared_communication", (), ("user",), "first_boot_boundary"),
            # the actor list alone is enough to catch it
            ((), "local_to_subject_world", ("user",), (), "first_boot_boundary"),
            # E. an unidentified counterparty
            (("unknown",), "local_to_subject_world", (), ("npc",), "unknown_participant"),
            # G'. a crossing that crosses nothing
            (
                ("npc",),
                "cross_world_physical",
                ("npc",),
                ("npc",),
                "cross_world_scope_without_crossing",
            ),
            # F. somebody the month never had
            (
                ("npc",),
                "local_to_subject_world",
                ("npc",),
                (),
                "experience_participant_not_in_month",
            ),
            # the two answers from one call, disagreeing
            (
                (),
                "local_to_subject_world",
                ("npc",),
                ("npc",),
                "actor_not_in_participants",
            ),
        ],
    )
    def test_the_refusal_matrix(
        self, participants, scope, actors, month, refused
    ) -> None:
        from app.genesis.experience_world import validate_experience

        reason = validate_experience(
            participants=participants,
            interaction_scope=scope,
            actor_subjects=actors,
            month_participants=month,
        )

        assert reason.startswith(refused), reason

    @pytest.mark.parametrize(
        ("participants", "actors", "month"),
        [
            # G. an ordinary afternoon with a neighbour
            (("npc",), ("npc",), ("npc",)),
            # H. an ordinary afternoon alone
            ((), (), ()),
            ((), ("yui",), ()),
            # compressing away a participant the month had is fine; adding one
            # is not
            ((), (), ("npc",)),
        ],
    )
    def test_what_passes(self, participants, actors, month) -> None:
        from app.genesis.experience_world import validate_experience

        assert (
            validate_experience(
                participants=participants,
                interaction_scope="local_to_subject_world",
                actor_subjects=actors,
                month_participants=month,
            )
            == ""
        )

    def test_validation_happens_before_the_row_exists(self) -> None:
        """An experience written and then rejected is a past that already
        happened: it has an id, a month, a sequence, and the next resume finds
        it there."""
        import inspect

        from app.genesis.runner import GenesisRunner

        source = inspect.getsource(GenesisRunner._extract)  # noqa: SLF001
        validate_at = source.index("validate_experience(")
        stage_at = source.index("self._experiences.stage(")

        assert validate_at < stage_at

    def test_a_staged_experience_carries_its_metadata(self, db, clock) -> None:
        """Persisted, so a restart re-checks the same thing rather than
        trusting that it was checked once."""
        from app.storage.repositories.genesis import (
            GenesisExperienceRepository,
            GenesisRunRepository,
        )
        from app.genesis.models import ExperienceActor

        run_id = GenesisRunRepository(db).start(
            birth=datetime(2007, 1, 1, tzinfo=timezone.utc),
            present=clock.now(),
            years=19,
            now=clock.now(),
        )
        repository = GenesisExperienceRepository(db)
        repository.stage(
            genesis_run_id=run_id,
            month_id="lmo_x",
            year_number=1,
            sequence=0,
            candidate=self._candidate(
                actor_refs=(ExperienceActor(name="ミカ", subject="npc"),),
                participants=("npc",),
            ),
        )

        row = db.query_one("SELECT * FROM genesis_experiences LIMIT 1")
        assert row["world_model_version"] == CURRENT_WORLD_MODEL_VERSION
        assert json.loads(row["participants_json"]) == ["npc"]
        assert row["interaction_scope"] == "local_to_subject_world"
        assert json.loads(row["actor_subjects_json"]) == ["npc"]
        # The free-text column still holds the display name, and is not what
        # the world check reads.
        assert row["actors"] == "ミカ"

    def test_replay_rechecks_what_is_on_disk(self, db, clock) -> None:
        """Defence in depth at the boundary where an experience becomes an
        Event, and from there a memory, a mood, a personality drift.

        The pre-stage check ran against an object in memory. This runs against
        what a restart, a legacy row or a hand-edited database presents.
        """
        from app.genesis.runner import _replay_refusal

        class Row(dict):
            def __getitem__(self, key):
                return self.get(key)

        current = Row(
            world_model_version=CURRENT_WORLD_MODEL_VERSION,
            participants_json='["npc"]',
            actor_subjects_json='["npc"]',
            interaction_scope="local_to_subject_world",
        )
        assert _replay_refusal(current) == ""

        legacy = Row(
            world_model_version=0,
            participants_json="[]",
            actor_subjects_json="[]",
            interaction_scope="local_to_subject_world",
        )
        assert _replay_refusal(legacy) == "world_model_unverified"

        tampered = Row(
            world_model_version=CURRENT_WORLD_MODEL_VERSION,
            participants_json='["user"]',
            actor_subjects_json="[]",
            interaction_scope="local_to_subject_world",
        )
        assert _replay_refusal(tampered).startswith("first_boot_boundary")

        blank = Row(
            world_model_version=CURRENT_WORLD_MODEL_VERSION,
            participants_json="[]",
            actor_subjects_json="[]",
            interaction_scope="",
        )
        assert _replay_refusal(blank) == "world_metadata_missing"

    def test_an_unreplayable_experience_makes_no_event(self) -> None:
        """The hard boundary. Nothing reaches the psychology, the memory or
        the personality from a row that did not pass."""
        import inspect

        from app.genesis.runner import GenesisRunner

        source = inspect.getsource(GenesisRunner._replay)  # noqa: SLF001
        refusal_at = source.index("_replay_refusal(")
        event_at = source.index("Event.create(")

        assert refusal_at < event_at


# =============================================================================
# The Genesis entrance: a resume cannot launder an old life
# =============================================================================


class TestGenesisRunProvenance:
    """`stage()` stamps the current version on whatever reaches it.

    That is correct — it is the code that ran the validation. It also means the
    chain from a resumed run through months, synthesis and extraction ends in a
    row that looks verified, so every one of those steps has to refuse a life
    the current rules never judged. The entrance refuses first, because a
    resume takes a run id and an old run id is as easy to type as a new one.
    """

    def test_a_new_run_is_stamped_in_the_same_insert(self, db, clock) -> None:
        """Not a later UPDATE: that leaves a window in which the run exists
        without provenance, and a crash inside it leaves exactly the row this
        is for."""
        from app.storage.repositories.genesis import GenesisRunRepository

        runs = GenesisRunRepository(db)
        run_id = runs.start(
            birth=datetime(2007, 1, 1, tzinfo=timezone.utc),
            present=clock.now(),
            years=19,
            now=clock.now(),
        )

        assert runs.world_model_version(run_id) == CURRENT_WORLD_MODEL_VERSION
        row = db.query_one(
            "SELECT world_model_version FROM genesis_runs WHERE genesis_run_id = ?",
            (run_id,),
        )
        assert row["world_model_version"] == CURRENT_WORLD_MODEL_VERSION

    def test_a_migrated_run_is_not(self, tmp_path, clock) -> None:
        db = _at(tmp_path, clock, 37, "run37.db")
        try:
            db.execute(
                "INSERT INTO genesis_runs (genesis_run_id, started_at, "
                "birth_datetime, present_datetime, years) "
                "VALUES ('gen_old', ?, ?, ?, 19)",
                (
                    "2026-01-01T00:00:00+00:00",
                    "2007-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            migrate(db, clock=clock)

            from app.storage.repositories.genesis import GenesisRunRepository

            assert GenesisRunRepository(db).world_model_version("gen_old") == (
                UNVERIFIED_WORLD_MODEL_VERSION
            )
            assert read_world_provenance(db).rebuild_required
        finally:
            db.close()

    def test_an_unknown_run_is_unverified(self, db) -> None:
        from app.storage.repositories.genesis import GenesisRunRepository

        assert GenesisRunRepository(db).world_model_version("gen_nope") == 0


class TestGenesisRunnerGates:
    """Each stage refuses on its own, so no single missed gate opens the chain."""

    @staticmethod
    def _runner(**overrides):
        """A runner whose model call raises if anything reaches it.

        The assertion most of these need is "no model call happened", and a
        double that counts is weaker than one that cannot be called by accident.
        """
        from app.genesis.runner import GenesisRunner

        class Exploding:
            calls = 0

            async def generate(self, *args, **kwargs):
                Exploding.calls += 1
                raise AssertionError("a stale month reached the model")

        Exploding.calls = 0
        return GenesisRunner, Exploding

    async def test_an_old_run_is_refused_at_the_entrance(
        self, application, clock
    ) -> None:
        """The reproduction. Nothing downstream runs, because nothing gets in.

        No anchors saved, no model call, no month reused, no experience staged,
        no event, no memory.
        """
        from app.genesis.anchors import LifeAnchors

        application.db.execute(
            "INSERT INTO genesis_runs (genesis_run_id, started_at, "
            "birth_datetime, present_datetime, years, world_model_version) "
            "VALUES ('gen_old', ?, ?, ?, 19, 0)",
            (
                "2026-01-01T00:00:00+00:00",
                "2007-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        before = application.db.scalar("SELECT COUNT(*) FROM life_anchors")

        anchors = LifeAnchors(
            birth_datetime=datetime(2007, 1, 1, tzinfo=timezone.utc),
            present_datetime=clock.now(),
        )
        progress = await application.genesis_runner.run(
            anchors, resume="gen_old", max_years=1
        )

        assert progress.blocked_by
        assert progress.blocked_by[0].code == "GENESIS_RUN_WORLD_MODEL_UNVERIFIED"
        assert "rebuild_required_world_model_version" in progress.blocked_by[0].reason
        # Nothing was written, not even the anchors the run would normally save.
        assert application.db.scalar("SELECT COUNT(*) FROM life_anchors") == before
        assert application.db.scalar("SELECT COUNT(*) FROM life_months") == 0
        assert application.db.scalar("SELECT COUNT(*) FROM genesis_experiences") == 0
        assert application.db.scalar("SELECT COUNT(*) FROM events") == 0

    def test_a_stale_month_is_not_reused_as_written(self) -> None:
        from app.genesis.runner import _month_refusal

        assert _month_refusal(_row(world_model_version=0)) == (
            "month_world_model_unverified"
        )
        assert _month_refusal(_row()) == ""

    def test_corrupt_metadata_on_a_current_row_is_not_read_as_nobody(self) -> None:
        """The permissive direction, and the one a hand-edited database takes.

        A month stamped current whose participant list will not parse is not a
        month with no participants.
        """
        from app.genesis.runner import _month_refusal

        assert _month_refusal(_row(participants_json="{oops")) == (
            "month_world_metadata_unreadable"
        )
        assert _month_refusal(_row(interaction_scope="")) == (
            "month_world_metadata_missing"
        )

    def test_a_current_row_with_a_contradiction_is_refused(self) -> None:
        """Stamped current is not the same as consistent. The same world rule a
        claim goes through runs here too."""
        from app.genesis.runner import _month_refusal

        assert _month_refusal(_row(participants_json='["user"]')).startswith(
            "interaction_scope_world_mismatch"
        )

    def test_a_good_row_passes(self) -> None:
        from app.genesis.runner import _month_refusal, _stored_participants

        row = _row(participants_json='["npc"]')
        assert _month_refusal(row) == ""
        assert _stored_participants(row) == ("npc",)

    @pytest.mark.parametrize(
        "stage", ["_extract", "_synthesise", "_review_year", "_month"]
    )
    def test_every_stage_consults_the_month_provenance(self, stage) -> None:
        """Structural. One missed stage is the whole chain: a stale month that
        reaches extraction comes back as a current experience."""
        import inspect

        from app.genesis.runner import GenesisRunner

        source = inspect.getsource(getattr(GenesisRunner, stage))

        assert "_month_refusal(" in source or "_stale_months(" in source, stage

    def test_extraction_checks_before_it_calls(self) -> None:
        """Not "called and discarded". A stale month costs nothing."""
        import inspect

        from app.genesis.runner import GenesisRunner

        source = inspect.getsource(GenesisRunner._extract)  # noqa: SLF001

        assert source.index("_month_refusal(") < source.index("await self._generate(")

    def test_synthesis_checks_before_it_calls(self) -> None:
        import inspect

        from app.genesis.runner import GenesisRunner

        source = inspect.getsource(GenesisRunner._synthesise)  # noqa: SLF001

        assert source.index("_stale_months(") < source.index("await self._generate(")

    def test_the_critic_gets_the_stored_metadata(self) -> None:
        """Otherwise `check_identity` falls back to the substring pass, which a
        paraphrase walks past — that is the layer this replaced."""
        import inspect

        from app.genesis.runner import GenesisRunner

        source = inspect.getsource(GenesisRunner._review_year)  # noqa: SLF001

        assert "participants=_stored_participants(month)" in source
        assert "interaction_scope=" in source


def _row(**overrides):
    """A `life_months` row as the repository returns one."""
    base = {
        "month_id": "lmo_x",
        "world_model_version": CURRENT_WORLD_MODEL_VERSION,
        "participants_json": "[]",
        "interaction_scope": "local_to_subject_world",
    }
    base.update(overrides)

    class Row(dict):
        def __getitem__(self, key):
            return self.get(key)

    return Row(base)


# =============================================================================
# Blocking and advisory are different answers
# =============================================================================


class TestBlockingAndAdvisory:
    @staticmethod
    def _gate(db):
        from app.live.readiness import LiveReadiness

        class Born:
            def status(self):
                return "COMPLETE"

        return LiveReadiness(
            first_boot=Born(),
            shadow=None,
            shadow_decisions=None,
            world_provenance=lambda: read_world_provenance(db),
        )

    @staticmethod
    def _check(db):
        return next(
            item
            for item in TestBlockingAndAdvisory._gate(db).check().checks
            if item.name == "world_model_current"
        )

    def _current_life(self, db, clock):
        from app.storage.repositories.genesis import GenesisRunRepository
        from app.storage.repositories.rebuild import RebuildEpochRepository

        RebuildEpochRepository(db).record(started_at=clock.now(), reason="fresh")
        return GenesisRunRepository(db).start(
            birth=datetime(2007, 1, 1, tzinfo=timezone.utc),
            present=clock.now(),
            years=19,
            now=clock.now(),
        )

    def test_a_current_life_passes(self, db, clock) -> None:
        self._current_life(db, clock)

        assert self._check(db).passed

    def test_a_stale_common_ground_row_alone_does_not_block(
        self, db, clock
    ) -> None:
        """It cannot become a fact — the correction boundary already refuses
        it — so holding the door over one would stop her talking about
        nothing. Reported, because the OWNER should know it is there."""
        self._current_life(db, clock)
        _legacy_common_ground(db, clock)

        report = read_world_provenance(db)
        assert not report.rebuild_required
        assert any(item.startswith("common_ground_claims=") for item in report.advisory)

        check = self._check(db)
        assert check.passed
        assert check.advisory
        assert "legacy rows present" in check.detail

    @pytest.mark.parametrize("store", ["life_months", "genesis_experiences"])
    def test_a_stale_life_row_does_block(self, db, clock, store) -> None:
        run_id = self._current_life(db, clock)
        _legacy_life_row(db, store, run_id)

        report = read_world_provenance(db)
        assert report.rebuild_required
        assert any(item.startswith(f"{store}=") for item in report.blocking)
        assert not self._check(db).passed

    def test_a_stale_run_blocks(self, db, clock) -> None:
        from app.storage.repositories.rebuild import RebuildEpochRepository

        RebuildEpochRepository(db).record(started_at=clock.now(), reason="fresh")
        db.execute(
            "INSERT INTO genesis_runs (genesis_run_id, started_at, birth_datetime, "
            "present_datetime, years, world_model_version) "
            "VALUES ('gen_old', ?, ?, ?, 19, 0)",
            (
                "2026-01-01T00:00:00+00:00",
                "2007-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

        report = read_world_provenance(db)
        assert "genesis_run=1" in report.blocking
        assert not self._check(db).passed

    def test_a_missing_reader_fails_closed(self) -> None:
        """"The reader was not wired" is indistinguishable from "nothing was
        checked", which is the state it exists to detect. A wiring regression
        must not read as a pass."""
        from app.live.readiness import LiveReadiness

        class Born:
            def status(self):
                return "COMPLETE"

        gate = LiveReadiness(
            first_boot=Born(),
            shadow=None,
            shadow_decisions=None,
            world_provenance=None,
        )

        check = next(
            item for item in gate.check().checks if item.name == "world_model_current"
        )
        assert not check.passed
        assert "not wired" in check.detail

    def test_production_wires_it(self, application) -> None:
        """So a bootstrap regression fails here rather than silently going
        live over a life nothing checked."""
        assert application.live._world_provenance is not None  # noqa: SLF001

        report = application.live._world_provenance()  # noqa: SLF001
        assert report.current == CURRENT_WORLD_MODEL_VERSION


def _legacy_common_ground(db, clock) -> None:
    from app.storage.repositories.conversations import ConversationRepository

    conversation = ConversationRepository(db).ensure_conversation(
        channel_id="chan_x", channel_type="direct_message", now=clock.now()
    )
    db.execute(
        "INSERT INTO common_ground_claims (claim_id, conversation_id, kind, "
        "statement, status, source, confidence, evidence_json, asserted_at, "
        "updated_at) VALUES ('cgc_old', ?, 'yui_completed_action', '本を読んだ', "
        "'provisional', 'claim_extractor', 'low', '[]', ?, ?)",
        (
            conversation.conversation_id,
            clock.now().isoformat(),
            clock.now().isoformat(),
        ),
    )


def _legacy_life_row(db, store: str, run_id: str) -> None:
    if store == "life_months":
        db.execute(
            "INSERT INTO life_years (year_id, genesis_run_id, year_number, "
            "calendar_start, calendar_end, age_start, age_end, created_at) "
            "VALUES ('lyr_x', ?, 1, ?, ?, 0, 1, ?)",
            (
                run_id,
                "2007-01-01T00:00:00+00:00",
                "2008-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.execute(
            "INSERT INTO life_months (month_id, year_id, month_number, "
            "month_start, month_end, age_start, age_end, narrative) "
            "VALUES ('lmo_x', 'lyr_x', 1, ?, ?, 0, 0, '君と映画を見に行った')",
            ("2007-01-01T00:00:00+00:00", "2007-02-01T00:00:00+00:00"),
        )
        return
    db.execute(
        "INSERT INTO genesis_experiences (experience_id, genesis_run_id, "
        "month_id, year_number, sequence, occurred_at) "
        "VALUES ('exp_old', ?, 'lmo_x', 1, 0, ?)",
        (run_id, "2007-01-01T00:00:00+00:00"),
    )
