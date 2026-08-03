"""The admin command registry (rebuild spec 30, Phase 5).

Thirty-odd commands, and none of them is a handler. Each entry names a query
method, validates its own arguments, and stops. Everything else — reading,
redacting, formatting, paginating — belongs to the layers on either side.

That shape is the whole point of having a registry at all. Phases 6 to 12 add
world, NPC, diary, genesis and knowledge views; if each of those were a bespoke
handler the router would be two thousand lines by Phase 12, and the read-only
guarantee would be spread across all of them instead of living in one service.

Commands are also *classified*, because they are not all the same risk:

    READ_ONLY       changes nothing at all
    SAFE_MUTATING   writes outside YUI's life — ``backup`` creates a file
    CEREMONY        the destructive operations, which keep their existing
                    confirmation flow in :mod:`app.admin.control_plane`

The distinction matters for the capability contract: ``backup`` must not be
counted as read-only just because it does not change any of her rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Sequence

from app.admin.queries import DebugQueryService
from app.admin.results import DebugResult


class CommandKind(str, Enum):
    READ_ONLY = "read_only"
    SAFE_MUTATING = "safe_mutating"
    CEREMONY = "ceremony"


@dataclass(frozen=True, slots=True)
class AdminCommand:
    """One command: what it is called, what it does, and how risky it is."""

    name: str
    help: str
    run: Callable[[DebugQueryService, Sequence[str]], DebugResult | Awaitable[DebugResult]]
    kind: CommandKind = CommandKind.READ_ONLY
    #: Sub-name, for ``memory find`` and the like.
    subcommand: str = ""

    @property
    def key(self) -> str:
        return f"{self.name} {self.subcommand}".strip()

    @property
    def is_read_only(self) -> bool:
        return self.kind is CommandKind.READ_ONLY


def _limit(args: Sequence[str], default: int = 10) -> int:
    for arg in args:
        if arg.isdigit():
            return int(arg)
    return default


def _domain(name: str):
    def run(service: DebugQueryService, args: Sequence[str]) -> DebugResult:
        return service.domain(name)

    return run


def _listing(command: str, source: str, method: str, fields: tuple[str, ...]):
    def run(service: DebugQueryService, args: Sequence[str]) -> DebugResult:
        return service.listing(
            command, source, method, fields, limit=_limit(args)
        )

    return run


def _build_registry() -> dict[str, AdminCommand]:
    commands: list[AdminCommand] = [
        AdminCommand("help", "list the commands", lambda service, args: _help()),
        AdminCommand("status", "schema, manifest and counters", lambda s, a: s.status()),
        AdminCommand("version", "runtime manifest", lambda s, a: s.version()),
        AdminCommand("state", "every state value", lambda s, a: s.state(a[0] if a else None)),
        # --- psychology -------------------------------------------------
        AdminCommand("emotion", "active emotions", _domain("emotion")),
        AdminCommand("mood", "mood", _domain("mood")),
        AdminCommand("needs", "needs", _domain("needs")),
        AdminCommand("relationship", "relationship with the USER", _domain("relationship")),
        AdminCommand("attachment", "attachment state", _domain("attachment")),
        AdminCommand("usermodel", "what she believes about the USER", _domain("user_model")),
        AdminCommand("personality", "personality traits", _domain("personality")),
        AdminCommand("values", "values", _domain("values")),
        AdminCommand("world", "world state", _domain("world")),
        # --- life -------------------------------------------------------
        AdminCommand(
            "activity",
            "recent activities",
            _listing(
                "activity", "activities", "completed",
                ("activity_id", "name", "kind", "status", "started_at", "ended_at"),
            ),
        ),
        AdminCommand(
            "goals",
            "active goals",
            _listing("goals", "goals", "active", ("goal_id", "description", "status", "progress")),
        ),
        AdminCommand(
            "habits",
            "habits",
            _listing("habits", "habits", "all", ("habit_id", "name", "cue", "automaticity")),
        ),
        AdminCommand(
            "scheduler",
            "pending scheduled jobs",
            _listing(
                "scheduler", "jobs", "pending",
                ("job_id", "job_type", "job_class", "status", "due_at"),
            ),
        ),
        AdminCommand(
            "proactive",
            "proactive contact history",
            _listing(
                "proactive", "proactive", "recent",
                ("contact_id", "opportunity", "sent_at", "answered_at"),
            ),
        ),
        AdminCommand(
            "proactive",
            "what would happen if she considered reaching out now",
            lambda s, a: s.proactive_dryrun(),
            subcommand="dryrun",
        ),
        AdminCommand(
            "proactive",
            "proactive deliberations, including the shadow ones",
            _listing(
                "proactive shadow", "proactive_deliberations", "recent",
                (
                    "considered_at",
                    "mode",
                    "trigger_kind",
                    "gate_passed",
                    "gate_reason",
                    "judgment",
                    "would_send",
                    "sent",
                ),
            ),
            subcommand="shadow",
        ),
        AdminCommand(
            "npc",
            "known NPCs",
            _listing("npc", "npcs", "all", ("npc_id", "name", "tier", "role", "status")),
        ),
        AdminCommand(
            "groups",
            "groups she belongs to",
            _listing(
                "groups", "groups", "all",
                ("group_id", "name", "activity_type", "status"),
            ),
        ),
        AdminCommand(
            "diary",
            "diary entries",
            _listing(
                "diary", "diary", "recent",
                (
                    "diary_id",
                    "life_day_id",
                    "intended_at",
                    "generated_at",
                    "status",
                    "summary",
                ),
            ),
        ),
        AdminCommand(
            "diary",
            "life days, waking to sleeping",
            _listing(
                "diary days", "life_days", "recent",
                ("life_day_id", "ordinal", "started_at", "ended_at"),
            ),
            subcommand="days",
        ),
        AdminCommand(
            "knowledge",
            "acquired knowledge",
            _listing(
                "knowledge", "knowledge", "all_knowledge",
                ("knowledge_id", "statement", "topic", "available_from"),
            ),
        ),
        AdminCommand(
            "growth",
            "consolidation runs",
            _listing(
                "growth", "consolidations", "recent",
                ("consolidation_id", "kind", "status", "started_at", "ended_at"),
            ),
        ),
        AdminCommand(
            "runtime",
            "recent autonomous wake-ups",
            _listing(
                "runtime", "runtime_ticks", "recent",
                (
                    "woke_at",
                    "wake_reason",
                    "opportunities",
                    "opportunity_kinds",
                    "candidates",
                    "chosen_action",
                    "outcome",
                    "next_wake_at",
                ),
            ),
        ),
        AdminCommand("genesis", "rebuild epoch and Genesis status", lambda s, a: s.genesis()),
        # --- memory -----------------------------------------------------
        AdminCommand("memory", "recent memories", lambda s, a: s.memory(limit=_limit(a))),
        AdminCommand(
            "memory",
            "why a memory would or would not be recalled",
            lambda s, a: s.memory_find(" ".join(a)),
            subcommand="find",
        ),
        AdminCommand("appraisal", "recent appraisal calls", lambda s, a: s.appraisal(limit=_limit(a))),
        # --- operations -------------------------------------------------
        AdminCommand("trace", "recent conversation traces", lambda s, a: s.trace(limit=_limit(a))),
        AdminCommand("latency", "reply latency percentiles", lambda s, a: s.latency()),
        AdminCommand("llm", "model call health", lambda s, a: s.llm(limit=_limit(a))),
        AdminCommand("failures", "recorded failures", lambda s, a: s.failures(limit=_limit(a))),
        AdminCommand("events", "recent events", lambda s, a: s.events(limit=_limit(a))),
        AdminCommand("runs", "recent processing runs", lambda s, a: s.runs(limit=_limit(a))),
        # --- not read-only ----------------------------------------------
        AdminCommand(
            "backup",
            "take a verified backup",
            lambda s, a: DebugResult.failure(
                "backup", "backup is wired by the router, not by the query service"
            ),
            kind=CommandKind.SAFE_MUTATING,
        ),
    ]
    return {command.key: command for command in commands}


REGISTRY: dict[str, AdminCommand] = _build_registry()

#: Every command that must leave the database exactly as it found it.
READ_ONLY_COMMANDS: tuple[str, ...] = tuple(
    key for key, command in REGISTRY.items() if command.is_read_only
)


def _help() -> DebugResult:
    rows = [
        {"command": f"!yui {key}", "kind": command.kind.value, "what": command.help}
        for key, command in sorted(REGISTRY.items())
    ]
    return DebugResult.of("help", summary=f"{len(rows)} command(s)", rows=rows)


def lookup(tokens: Sequence[str]) -> tuple[AdminCommand | None, tuple[str, ...]]:
    """Find the command these tokens name, longest match first.

    ``memory find 本`` has to beat ``memory``, which is why this tries two
    tokens before one rather than splitting on the first space.
    """
    if not tokens:
        return REGISTRY.get("help"), ()
    if len(tokens) >= 2:
        command = REGISTRY.get(f"{tokens[0]} {tokens[1]}")
        if command is not None:
            return command, tuple(tokens[2:])
    return REGISTRY.get(tokens[0]), tuple(tokens[1:])


__all__ = [
    "REGISTRY",
    "READ_ONLY_COMMANDS",
    "AdminCommand",
    "CommandKind",
    "lookup",
]
