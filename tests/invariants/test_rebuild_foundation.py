"""INVARIANT: a rebuild starts a fresh person and loses nothing by accident.

Rebuild spec 3 and 4. Two things have to be true at once, and they pull in
opposite directions:

* the new YUI carries nothing forward — no events, no memories, no
  relationship, no personality (REB-3.3);
* and nothing is deleted, because the OWNER's permission to discard the old
  history is not permission to lose it irrecoverably (REB-3.1, REB-3.4b).

The reset satisfies both by *moving* the old database and taking a second,
independently verified copy of it first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.admin.rebuild import (
    BACKUP_SUBDIR,
    CONFIRMATION,
    MUST_BE_EMPTY,
    RebuildRefused,
)
from app.bootstrap import Application
from app.storage.database import Database
from app.storage.migrations import LATEST_VERSION
from app.storage.repositories.rebuild import RebuildEpochRepository
from app.versioning.capabilities import (
    REQUIRED_CAPABILITIES,
    REQUIRED_FIELDS,
    STATUSES,
    CapabilityContractError,
    load_contracts,
    summary,
)

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "docs" / "IMPLEMENTATION_LEDGER.md"
CAPABILITY_DIR = REPO_ROOT / "config" / "capabilities"


@pytest.fixture
def lived_in(temp_config, clock):
    """An application with something in it worth being careful about."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    return application


def _make_history(application, make_event) -> int:
    """Give the database a past: events, a conversation and some state."""
    conversation = application.conversations.ensure_conversation(
        channel_id="1", channel_type="direct_message", now=application.clock.now()
    )
    for index in range(3):
        event = make_event(text=f"やっほー{index}")
        application.event_store.append(event)
        application.conversations.record_turn(
            conversation_id=conversation.conversation_id,
            event_id=event.event_id,
            speaker="user",
            author_id="1",
            content=f"やっほー{index}",
            occurred_at=event.occurred_at,
        )
    application.state.write_value(
        domain="emotion", key="joy", value=0.7, confidence=None,
        now=application.clock.now(), run_id=None, event_id=None, expected_version=None,
    )
    return application.event_store.count()


# --- REB-3.4: it is its own command, and it needs the word -------------------
def test_a_reset_without_the_confirmation_is_refused(lived_in, make_event) -> None:
    """REB-3.4: a reset that can happen by accident is data loss with a name."""
    before = _make_history(lived_in, make_event)

    for wrong in ("", "yes", "erase_yui_state", CONFIRMATION.lower()):
        with pytest.raises(RebuildRefused):
            lived_in.rebuild.reset(confirmation=wrong)

    assert lived_in.event_store.count() == before, "nothing may have been touched"
    lived_in.db.close()


def test_normal_startup_never_resets(temp_config, clock, make_event) -> None:
    """REB-3.4: ``通常 startup で自動 DB 削除してはならない``."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    before = _make_history(application, make_event)
    application.db.close()

    again = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        assert again.event_store.count() == before
        assert again.rebuild.current_epoch() is None
    finally:
        again.db.close()


def test_the_reset_is_not_reachable_from_the_run_path() -> None:
    """Structural: nothing on the startup path may call it."""
    import re

    startup = (REPO_ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    assert not re.search(r"\.reset\(", startup), (
        "bootstrap must never call the rebuild reset (REB-3.4)"
    )


# --- REB-3.1 / 3.2 / 3.3 / 3.4b: the reset itself ---------------------------
def test_a_reset_produces_a_fresh_database(lived_in, make_event) -> None:
    """Phase 0 gate. The whole thing, end to end, on real files."""
    before = _make_history(lived_in, make_event)
    assert before > 0
    production = Path(lived_in.db.path)

    result = lived_in.rebuild.reset(confirmation=CONFIRMATION, reason="phase 0 gate")

    assert result.ok, result.refusal
    # 3.1 — a verified backup, filed where the spec says.
    assert result.backup is not None and result.backup.usable
    assert result.backup.path.parent.name == BACKUP_SUBDIR
    assert result.backup.path.exists()
    # 3.4b — the old database was moved, not removed.
    assert result.archived_db_path is not None
    assert result.archived_db_path.exists()
    # 3.2 — the live file is a new database at the latest schema.
    assert result.fresh_db_path == production
    assert result.schema_version == LATEST_VERSION
    # 3.3 — and it is empty.
    assert result.imported_rows == {}
    assert lived_in.event_store.count() == 0
    # 3.4 — the epoch is recorded and Genesis is pending.
    assert result.epoch_id is not None
    epoch = RebuildEpochRepository(lived_in.db).current()
    assert epoch["genesis_status"] == "pending"
    lived_in.db.close()


def test_nothing_from_the_old_life_survives_the_reset(lived_in, make_event) -> None:
    """REB-3.3: ``旧情報を import しない``, checked table by table."""
    _make_history(lived_in, make_event)

    lived_in.rebuild.reset(confirmation=CONFIRMATION)

    existing = {
        row["name"]
        for row in lived_in.db.query_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    # Some of MUST_BE_EMPTY belong to phases that have not built their tables
    # yet (diary is Phase 10). Those are listed ahead of time on purpose, so
    # the sweep covers them the moment they appear.
    checked = [table for table in MUST_BE_EMPTY if table in existing]
    assert len(checked) >= 20, "the sweep is not covering enough of the schema"
    for table in checked:
        count = lived_in.db.scalar(f"SELECT COUNT(*) FROM {table}")
        assert count == 0, f"{table} carried {count} row(s) into the new life"
    lived_in.db.close()


def test_the_old_database_is_archived_not_deleted(lived_in, make_event) -> None:
    """REB-3.4b: the forensic copy is the answer to 'where did she go'."""
    before = _make_history(lived_in, make_event)

    result = lived_in.rebuild.reset(confirmation=CONFIRMATION)
    lived_in.db.close()

    archived = Database(result.archived_db_path)
    try:
        archived.connect()
        assert archived.scalar("SELECT COUNT(*) FROM events") == before
    finally:
        archived.close()


def test_the_backup_is_a_second_independent_copy(lived_in, make_event) -> None:
    """REB-3.1: the backup and the archive are two copies, not one moved twice."""
    before = _make_history(lived_in, make_event)

    result = lived_in.rebuild.reset(confirmation=CONFIRMATION)
    lived_in.db.close()

    assert result.backup.path != result.archived_db_path
    backup = Database(result.backup.path)
    try:
        backup.connect()
        assert backup.scalar("SELECT COUNT(*) FROM events") == before
    finally:
        backup.close()


def test_the_fresh_database_is_at_the_latest_schema(lived_in, make_event) -> None:
    """REB-3.2: built from migrations, not migrated from the old file."""
    _make_history(lived_in, make_event)

    lived_in.rebuild.reset(confirmation=CONFIRMATION)

    from app.storage.migrations import schema_version

    assert schema_version(lived_in.db) == LATEST_VERSION
    lived_in.db.close()


def test_the_epoch_records_where_the_old_life_went(lived_in, make_event) -> None:
    """REB-3.4c: the reset is a row, not something somebody remembers doing."""
    _make_history(lived_in, make_event)

    result = lived_in.rebuild.reset(confirmation=CONFIRMATION, reason="a fresh start")

    epoch = RebuildEpochRepository(lived_in.db).current()
    assert epoch["epoch_id"] == result.epoch_id
    assert epoch["reason"] == "a fresh start"
    assert epoch["archived_db_path"] == str(result.archived_db_path)
    assert epoch["backup_path"] == str(result.backup.path)
    assert epoch["schema_version"] == LATEST_VERSION
    assert epoch["previous_epoch_id"] is None
    lived_in.db.close()


def test_a_second_reset_chains_to_the_first(lived_in, make_event) -> None:
    _make_history(lived_in, make_event)
    first = lived_in.rebuild.reset(confirmation=CONFIRMATION)
    lived_in.clock.advance(seconds=3600)

    second = lived_in.rebuild.reset(confirmation=CONFIRMATION)

    epoch = RebuildEpochRepository(lived_in.db).current()
    assert epoch["epoch_id"] == second.epoch_id
    assert epoch["previous_epoch_id"] == first.epoch_id
    lived_in.db.close()


def test_a_reset_takes_a_verified_backup_first(lived_in, make_event, monkeypatch) -> None:
    """REB-3.1: an unverified backup stops the reset before anything moves."""
    before = _make_history(lived_in, make_event)
    original = lived_in.backups.create

    def unusable(**kwargs):
        record = original(**kwargs)
        return type(record)(
            **{**record.__dict__, "integrity": "malformed", "restore_tested": False}
            if hasattr(record, "__dict__")
            else {}
        )

    import dataclasses

    monkeypatch.setattr(
        lived_in.backups,
        "create",
        lambda **kwargs: dataclasses.replace(
            original(**kwargs), integrity="malformed", restore_tested=False
        ),
    )

    result = lived_in.rebuild.reset(confirmation=CONFIRMATION)

    assert result.ok is False
    assert "did not verify" in result.refusal
    assert lived_in.event_store.count() == before, "the old life is still there"
    lived_in.db.close()


# --- REB-4.3: capability contracts ------------------------------------------
def test_every_required_capability_has_a_contract() -> None:
    """Rebuild spec 4.3 lists thirteen; all thirteen must be written down."""
    contracts = load_contracts(CAPABILITY_DIR)

    assert set(contracts) == set(REQUIRED_CAPABILITIES)


def test_every_contract_names_its_whole_vertical_path() -> None:
    """A contract with no action or no event is a subsystem that never runs."""
    for name, contract in load_contracts(CAPABILITY_DIR).items():
        for field in REQUIRED_FIELDS:
            value = getattr(contract, field)
            assert value.strip(), f"{name} has an empty {field}"


def test_a_contract_status_is_one_of_the_four() -> None:
    """``DONE`` is deliberately not available (rebuild spec 4.2)."""
    for contract in load_contracts(CAPABILITY_DIR).values():
        assert contract.status in STATUSES
        assert contract.status != "DONE"


def test_a_missing_capability_is_an_error(tmp_path) -> None:
    (tmp_path / "normal_reply.yaml").write_text(
        "\n".join(f'{field}: "x"' for field in REQUIRED_FIELDS).replace(
            'status: "x"', 'status: "NOT_STARTED"'
        ).replace('capability: "x"', 'capability: "normal_reply"'),
        encoding="utf-8",
    )
    with pytest.raises(CapabilityContractError):
        load_contracts(tmp_path)


def test_a_contract_missing_a_field_is_an_error(tmp_path) -> None:
    (tmp_path / "activity.yaml").write_text(
        'capability: "activity"\nstatus: "WIRED"\n', encoding="utf-8"
    )
    with pytest.raises(CapabilityContractError):
        load_contracts(tmp_path)


def test_an_unknown_status_is_an_error(tmp_path) -> None:
    body = {field: "x" for field in REQUIRED_FIELDS}
    body["capability"] = "activity"
    body["status"] = "DONE"
    (tmp_path / "activity.yaml").write_text(
        "\n".join(f'{key}: "{value}"' for key, value in body.items()), encoding="utf-8"
    )
    with pytest.raises(CapabilityContractError):
        load_contracts(tmp_path)


#: Capabilities that have been driven end to end, named one at a time as the
#: phase that verified them lands. Rebuild spec 4.4: instantiation is not
#: completion, so this list may only grow deliberately.
VERIFIED_CAPABILITIES: frozenset[str] = frozenset({
    # Phase 3: a real inbound message drives interpretation, planning,
    # reference lookup, realization, the hard gates and delivery, and
    # tests/invariants/test_conversation_realization_e2e.py asserts the rows.
    "natural_conversation_realization",
    # Phase 4: a chosen silence produces its own event, no turn, no failure
    # row and no typing indicator, and tests/invariants/
    # test_intentional_silence_e2e.py asserts each of those.
    "intentional_silence",
    # Phase 5: the whole registry runs against a seeded database and
    # tests/invariants/test_admin_readonly_e2e.py fingerprints 26 life tables
    # before and after, requiring byte equality. `backup` is separate because
    # it writes a file.
    "admin_debug_readonly",
    "admin_backup",
    # Phase 7: the loop drives the first real handlers, so the runtime itself
    # is finally proven end to end, and activity and sleep with it.
    "autonomous_runtime",
    "activity",
    "sleep",
    # Phase 8: §31's "connect the existing engines to the runtime", plus the
    # people and groups that stop the USER being her only source of company.
    "goal_action",
    "habit_action",
    "npc_interaction",
    "group_activity",
    # Phase 9: the gate, the judgment and the shadow record. A real unprompted
    # message to the real USER is a final-gate item, not this one.
    "proactive_contact",
    # Phase 10.
    "diary",
    # Phase 11: the formal entrance for information from outside.
    "web_search",
    # Phase 12.
    "genesis",
    # Phase 13: the one gate that decides whether she exists at all. Verified
    # against a two-month fixture life; the nineteen-year run is GEN-GATE.
    "first_boot",
})


def test_only_the_named_capabilities_claim_to_be_finished() -> None:
    """Rebuild spec 4.4: instantiation is not completion.

    This asserts the *honest* current state. A capability reaching
    E2E_VERIFIED has to be added here by the phase that verified it, so the
    count cannot drift upward on its own.
    """
    contracts = load_contracts(CAPABILITY_DIR)
    verified = {
        name for name, contract in contracts.items() if contract.status == "E2E_VERIFIED"
    }
    assert verified == VERIFIED_CAPABILITIES

    counts = summary(contracts)
    assert counts["E2E_VERIFIED"] == len(VERIFIED_CAPABILITIES)


# --- REB-4.2: the ledger -----------------------------------------------------
def test_the_ledger_exists() -> None:
    assert LEDGER.is_file()


def test_the_ledger_never_says_done() -> None:
    """Rebuild spec 4.2: ``DONE`` は使用しない."""
    text = LEDGER.read_text(encoding="utf-8")
    import re

    assert not re.search(r"\|\s*DONE\s*\|", text), (
        "the ledger must not use DONE as a status (rebuild spec 4.2)"
    )
    for status in STATUSES:
        assert status in text, f"the ledger does not explain {status}"


def test_every_capability_appears_in_the_ledger() -> None:
    text = LEDGER.read_text(encoding="utf-8")
    for name in REQUIRED_CAPABILITIES:
        assert name in text, f"{name} is not tracked in the ledger"


# --- the CLI ----------------------------------------------------------------
def test_the_cli_refuses_without_the_confirmation(temp_config, tmp_path, capsys) -> None:
    """REB-3.4 through the real entry point."""
    from app import main

    settings = temp_config.root_dir / "config" / "settings.yaml"
    code = main.main(
        [
            "--config", str(settings),
            # Never the repo's own data/ — the testing rules forbid a test
            # opening the production database, and without --root the CLI
            # resolves it against the working directory.
            "--root", str(temp_config.root_dir),
            "rebuild-reset", "--confirm", "nope",
        ]
    )

    assert code == 3


def test_the_cli_reports_capability_status(temp_config, capsys) -> None:
    from app import main

    settings = temp_config.root_dir / "config" / "settings.yaml"
    code = main.main(
        ["--config", str(settings), "--root", str(temp_config.root_dir), "capabilities"]
    )

    report = json.loads(capsys.readouterr().out)
    assert set(report["capabilities"]) == set(REQUIRED_CAPABILITIES)
    # Non-zero while anything is unfinished, so it is usable as a gate.
    assert code == 1
