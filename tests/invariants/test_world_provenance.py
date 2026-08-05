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
        assert schema_version(db) == LATEST_VERSION == 37

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

            assert result.applied == (37,)
            assert schema_version(db) == 37
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
