"""INVARIANT: running every debug command does not advance YUI's life
(rebuild spec 30, Phase 5).

The completion condition for this phase is not "the commands reply". It is
**"you can run all of them and nothing about her has moved"** — and that is a
much harder property, because the dangerous paths write as a side effect of
reading. ``MemoryEngine.recall()`` practises what it returns. ``observe()``
opens an episode. The processor commits. Any one of those reached from a debug
query would mean that asking about her changes her, and every answer after the
first is about a system the question modified.

So the test is a byte-for-byte comparison. Snapshot every table that holds her
life, run the whole registry, snapshot again, and require equality — not "no
obvious change", not "counts match".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.admin.commands import READ_ONLY_COMMANDS, REGISTRY, CommandKind, lookup
from app.admin.formatter import DISCORD_LIMIT, DiscordAdminFormatter
from app.admin.queries import DebugQueryService, DebugSources
from app.admin.results import REDACTED, DebugResult, redact, safe_row
from app.admin.router import ADMIN_PREFIX, AdminRouter
from app.bootstrap import Application
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
OWNER = "111111111111111111"
CHANNEL = "222222222222222222"

#: Everything that is YUI's life. If a debug command changes a row in any of
#: these, the phase has failed however good the output looked.
LIFE_TABLES: tuple[str, ...] = (
    "events",
    "conversations",
    "conversation_turns",
    "episodes",
    "episodic_memories",
    "semantic_memories",
    "memory_retrievals",
    "memory_revisions",
    "state_values",
    "state_changes",
    "beliefs",
    "self_schemas",
    "processing_runs",
    "state_snapshots",
    "personality_traits",
    "values",
    "adaptations",
    "activities",
    "goals",
    "habits",
    "npcs",
    "npc_relationships",
    "common_ground_claims",
    "llm_calls",
    "failures",
    "conversation_traces",
)


def fingerprint(db) -> dict[str, str]:
    """A hash per table. Cheap, exact, and it names the table that moved."""
    existing = {
        row["name"]
        for row in db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    digest: dict[str, str] = {}
    for table in LIFE_TABLES:
        if table not in existing:
            continue
        rows = db.query_all(f"SELECT * FROM {table}")  # noqa: S608 - fixed names
        payload = json.dumps(
            [[str(value) for value in tuple(row)] for row in rows], sort_keys=True
        )
        digest[table] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest


@pytest.fixture
def owned_config(temp_config):
    """A config with an owner, so bootstrap wires a real admin router.

    The tests below drive ``application.admin_router`` — the object the running
    system uses — rather than a parallel one assembled in the test. A read-only
    guarantee proven on a hand-built copy would say nothing about production.
    """
    return temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )


@pytest.fixture
def application(owned_config, clock):
    built = Application.build(owned_config, clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


@pytest.fixture
def router(application):
    """The router the running system actually uses."""
    return application.admin_router


async def _seed_a_life(application, clock, make_event) -> None:
    """Give the database something to be unchanged."""
    for _ in range(3):
        await application.processor.process(make_event(actor_type="user"))
        clock.advance(seconds=90)


# --- the completion condition ------------------------------------------------


async def test_every_read_only_command_leaves_her_life_identical(
    application, router, clock, make_event
) -> None:
    """Phase 5's actual definition of done."""
    await _seed_a_life(application, clock, make_event)

    before = fingerprint(application.db)
    assert before, "the fixture must have something to protect"

    answered = 0
    for key in READ_ONLY_COMMANDS:
        outcome = await router.route(
            text=f"{ADMIN_PREFIX} {key}", author_id=OWNER, channel_id=CHANNEL
        )
        assert outcome.handled, key
        assert outcome.result is not None, key
        assert not outcome.result.failed, (key, outcome.result.error)
        answered += 1

    assert answered >= 30, f"only {answered} read-only commands are registered"

    after = fingerprint(application.db)
    changed = [table for table in before if before[table] != after.get(table)]
    assert changed == [], f"admin commands modified: {changed}"


async def test_memory_find_practises_nothing(
    application, router, clock, make_event
) -> None:
    """The one read-only command that reaches memory. Phase 2 §2Q built the
    inspector for exactly this; running it ten times must be free."""
    await _seed_a_life(application, clock, make_event)
    before = fingerprint(application.db)

    for _ in range(10):
        outcome = await router.route(
            text=f"{ADMIN_PREFIX} memory find 海", author_id=OWNER, channel_id=CHANNEL
        )
        assert outcome.result is not None
        assert not outcome.result.failed

    assert fingerprint(application.db) == before


def test_the_query_service_holds_no_engine_that_could_write() -> None:
    """Structural. The read-only guarantee is a property of the object graph,
    not of remembering to be careful in thirty-odd methods."""
    import inspect

    source = inspect.getsource(DebugQueryService)
    for writer in (
        "MemoryEngine",
        "EventProcessor",
        "ConversationService",
        ".recall(",
        ".observe(",
        ".process(",
        "record_retrieval",
        "update_accessibility",
        "write_value",
        "record_change",
    ):
        assert writer not in source, writer

    fields = set(DebugSources.__dataclass_fields__)
    for engine in ("processor", "memory_engine", "conversation", "committer"):
        assert engine not in fields, engine


# --- routing happens first ---------------------------------------------------


async def test_an_admin_command_never_reaches_the_conversation_service(
    application, router, clock, make_event
) -> None:
    """The boundary has to be upstream. Noticing afterwards is not available:
    by then the USER event exists, the appraisal ran and the episode is open."""
    await _seed_a_life(application, clock, make_event)
    before = fingerprint(application.db)

    outcome = await router.route(
        text=f"{ADMIN_PREFIX} status", author_id=OWNER, channel_id=CHANNEL
    )

    assert outcome.handled
    # No USER_MESSAGE_RECEIVED, no turn, no appraisal, no state change.
    assert fingerprint(application.db) == before


async def test_a_non_owner_is_refused_and_does_not_fall_through(
    application, router
) -> None:
    """An outsider must not be able to make YUI talk by prefixing a message."""
    outcome = await router.route(
        text=f"{ADMIN_PREFIX} status", author_id="999999999999999999", channel_id=CHANNEL
    )

    assert outcome.handled  # handled, so the gateway does not converse
    assert outcome.refused
    assert outcome.result is None


async def test_a_disallowed_channel_is_refused(router) -> None:
    outcome = await router.route(
        text=f"{ADMIN_PREFIX} status", author_id=OWNER, channel_id="some-other-channel"
    )

    assert outcome.handled
    assert outcome.refusal == "channel_not_allowed"


async def test_ordinary_messages_are_not_handled(application, router) -> None:
    outcome = await router.route(
        text="やっほー", author_id=OWNER, channel_id=CHANNEL
    )
    assert not outcome.handled
    assert outcome.result is None


def test_ownership_is_checked_in_exactly_one_place() -> None:
    """Repeated checks drift, and a command that forgot one is a hole."""
    import inspect

    from app.admin import commands as commands_module

    source = inspect.getsource(commands_module)
    for marker in ("owner", "author_id", "permission"):
        assert marker not in source.lower(), marker


# --- an admin error stays an admin error -------------------------------------


async def test_a_broken_query_does_not_become_a_psychological_failure(
    application, clock, make_event
) -> None:
    """The failure table feeds drift and health. A mistyped debug query is not
    something that happened to her."""
    await _seed_a_life(application, clock, make_event)

    class Exploding:
        def recent_memories(self, **kwargs):
            raise RuntimeError("the disk is gone")

        def memory_count(self, **kwargs):
            raise RuntimeError("the disk is gone")

    router = AdminRouter(
        DebugQueryService(
            DebugSources(memories=Exploding(), failures=application.failures),
            clock=application.clock,
        ),
        owner_user_id=OWNER,
        clock=application.clock,
    )
    before_failures = len(application.failures.recent(limit=100))

    outcome = await router.route(
        text=f"{ADMIN_PREFIX} memory", author_id=OWNER, channel_id=CHANNEL
    )

    assert outcome.handled
    assert outcome.result is not None
    assert outcome.result.failed
    assert "RuntimeError" in outcome.result.error
    # No FailureRecord was written.
    assert len(application.failures.recent(limit=100)) == before_failures


async def test_an_unknown_command_is_answered_not_ignored(router) -> None:
    outcome = await router.route(
        text=f"{ADMIN_PREFIX} nonsense", author_id=OWNER, channel_id=CHANNEL
    )
    assert outcome.handled
    assert outcome.result is not None
    assert outcome.result.failed
    assert "help" in outcome.result.error


# --- secrets never reach the channel -----------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "discord_bot_token",
        "DISCORD_BOT_TOKEN",
        "api_key",
        "openai_api_key",
        "password",
        "webhook_url",
        "session_key",
        "request_transcript",
        "system_prompt",
        "response_text",
    ],
)
def test_a_secret_or_prompt_field_is_never_printed(field: str) -> None:
    assert redact(field, "hunter2-and-then-some") == REDACTED


def test_a_credential_shaped_value_is_redacted_whatever_it_is_called() -> None:
    assert redact("note", "sk-abcdefghijklmnopqrstuvwxyz") == REDACTED
    assert redact("detail", "Bot " + "a" * 60) == REDACTED


def test_an_ordinary_value_survives() -> None:
    assert redact("summary", "海に行った話をした") == "海に行った話をした"


def test_redaction_reaches_into_nested_payloads() -> None:
    cleaned = redact("detail", {"ok": "fine", "auth_header": "Bearer xyz"})
    assert cleaned == {"ok": "fine", "auth_header": REDACTED}


def test_a_row_is_reduced_to_named_fields() -> None:
    row = {"kept": 1, "discord_bot_token": "secret", "ignored": "no"}
    assert safe_row(row, fields=("kept", "discord_bot_token")) == {
        "kept": 1,
        "discord_bot_token": REDACTED,
    }


async def test_the_llm_command_shows_health_not_content(
    application, router, clock, make_event
) -> None:
    await _seed_a_life(application, clock, make_event)

    outcome = await router.route(
        text=f"{ADMIN_PREFIX} llm", author_id=OWNER, channel_id=CHANNEL
    )
    rendered = "\n".join(DiscordAdminFormatter().format(outcome.result))

    assert "prompt" not in rendered.lower()
    assert "transcript" not in rendered.lower()
    for section in outcome.result.sections:
        for row in section.rows:
            assert "request_transcript" not in row
            assert "response_text" not in row


async def test_events_and_failures_never_print_a_payload(
    application, router, clock, make_event
) -> None:
    """A raw dump is how a token reaches a channel six months after somebody
    adds a column. Both commands name their fields; neither prints ``payload``."""
    await _seed_a_life(application, clock, make_event)

    for command in ("events", "failures"):
        outcome = await router.route(
            text=f"{ADMIN_PREFIX} {command}", author_id=OWNER, channel_id=CHANNEL
        )
        assert not outcome.result.failed, outcome.result.error
        for section in outcome.result.sections:
            for row in section.rows:
                assert "payload" not in row, command
                assert "detail" not in row, command


def test_no_admin_path_reads_configuration_or_the_environment() -> None:
    """`.env`, the token and the config object are not debuggable material."""
    import inspect

    from app.admin import commands as commands_module
    from app.admin import formatter as formatter_module
    from app.admin import queries as queries_module
    from app.admin import results as results_module
    from app.admin import router as router_module

    for module in (
        queries_module,
        commands_module,
        router_module,
        formatter_module,
        results_module,
    ):
        source = inspect.getsource(module)
        for forbidden in ("os.environ", "getenv", "dotenv", ".env", "AppConfig", "secrets."):
            assert forbidden not in source, (module.__name__, forbidden)


# --- output is readable ------------------------------------------------------


def test_a_long_result_is_paginated_rather_than_truncated() -> None:
    rows = [{"memory_id": f"mem_{index}", "summary": "あ" * 120} for index in range(60)]
    pages = DiscordAdminFormatter().format(
        DebugResult.of("memory", summary="many", rows=rows)
    )

    assert len(pages) > 1
    for page in pages:
        assert len(page) <= DISCORD_LIMIT
        # Every page is a complete fenced block, never a cut-off one.
        assert page.startswith("```\n")
        assert page.endswith("\n```")
    assert "page 1/" in pages[0]


def test_a_short_result_is_one_page() -> None:
    pages = DiscordAdminFormatter().format(
        DebugResult.of("status", summary="ok", rows=[{"events": 3}])
    )
    assert len(pages) == 1
    assert "page" not in pages[0]


def test_an_error_renders_as_an_error() -> None:
    pages = DiscordAdminFormatter().format(DebugResult.failure("memory", "gone"))
    assert len(pages) == 1
    assert "error" in pages[0]


# --- the registry ------------------------------------------------------------


def test_the_registry_covers_the_documented_commands() -> None:
    expected = {
        "help", "status", "state", "emotion", "mood", "needs", "relationship",
        "attachment", "usermodel", "personality", "values", "world", "activity",
        "goals", "habits", "memory", "memory find", "appraisal", "trace",
        "latency", "llm", "failures", "events", "runs", "scheduler",
        "proactive", "npc", "groups", "diary", "knowledge", "genesis",
        "growth", "version", "backup",
    }
    assert expected <= set(REGISTRY)


def test_backup_is_not_counted_as_read_only() -> None:
    """It changes nothing in the database and still writes a file. Mixing it
    into the read-only capability would make the guarantee a half-truth."""
    assert REGISTRY["backup"].kind is CommandKind.SAFE_MUTATING
    assert "backup" not in READ_ONLY_COMMANDS


async def test_only_the_unlanded_subsystems_report_not_wired(
    application, router
) -> None:
    """Every command must reach something real.

    ``listing`` answers "not wired yet in this phase" when a declared source is
    still ``None``. That is honest for a subsystem which genuinely has not
    landed, and a trap for a mistyped source name — so the set is pinned and
    may only shrink deliberately.

    It is empty as of Phase 10, which built the diary — the last source that
    was declared and unbuilt. It stays here rather than being deleted: the next
    phase that declares a source ahead of building it has to add itself to this
    set on purpose, which is the whole mechanism.
    """
    unwired = set()
    for key in READ_ONLY_COMMANDS:
        outcome = await router.route(
            text=f"{ADMIN_PREFIX} {key}", author_id=OWNER, channel_id=CHANNEL
        )
        assert not outcome.result.failed, (key, outcome.result.error)
        if "not wired yet" in outcome.result.summary:
            unwired.add(key)

    assert unwired == set(), unwired


def test_a_typo_in_a_source_name_is_loud() -> None:
    """The graceful path is for phases that have not landed, not for mistakes."""
    service = DebugQueryService(DebugSources())

    result = service.listing("bogus", "notasource", "recent", ("x",))

    assert result.failed
    assert "not a debug source" in result.error


async def test_backup_writes_a_file_and_still_no_row(
    application, clock, make_event, tmp_path
) -> None:
    """``backup`` is SAFE_MUTATING: it changes the filesystem, never her life."""
    await _seed_a_life(application, clock, make_event)
    before = fingerprint(application.db)

    taken: list[str] = []

    class Backups:
        def create(self, *, reason: str):
            taken.append(reason)
            return type(
                "Record", (), {"path": tmp_path / "yui.db", "integrity": "ok", "size_bytes": 10}
            )()

    router = AdminRouter(
        DebugQueryService(DebugSources(), clock=application.clock),
        owner_user_id=OWNER,
        backup=Backups(),
        clock=application.clock,
    )

    outcome = await router.route(
        text=f"{ADMIN_PREFIX} backup", author_id=OWNER, channel_id=CHANNEL
    )

    assert outcome.handled
    assert not outcome.result.failed
    assert taken == ["admin_command"]
    assert fingerprint(application.db) == before


def test_a_subcommand_beats_its_parent() -> None:
    command, args = lookup(["memory", "find", "海"])
    assert command is not None
    assert command.key == "memory find"
    assert args == ("海",)

    command, args = lookup(["memory", "5"])
    assert command is not None
    assert command.key == "memory"
    assert args == ("5",)
