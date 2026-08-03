"""Read-only debug queries (rebuild spec 30, Phase 5).

The one rule: **running these must not advance YUI's life by one row.**

That is stricter than "does not obviously write". The dangerous paths are the
ones that write as a side effect of reading, and memory is full of them —
``MemoryEngine.recall()`` practises what it returns, ``observe()`` opens an
episode, the processor commits state. None of those are reachable from here.
What this service holds is repository read methods and
:class:`~app.memory.inspector.MemoryInspector`, which was built for exactly this
in Phase 2 and holds no writer at all.

It also holds no ``EventProcessor``, no ``ConversationService`` and no
``MemoryEngine``. A debug question is not something that happened to her.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from app.admin.results import DebugResult, safe_row
from app.clock import Clock, SystemClock
from app.memory.recall_mode import RecallMode
from app.world.models import Opportunity

logger = logging.getLogger(__name__)

#: How many rows a listing command returns by default. Discord has a limit and
#: an operator has an attention span; both are smaller than a table.
DEFAULT_LIMIT = 10
MAX_LIMIT = 50


@dataclass(frozen=True, slots=True)
class DebugSources:
    """Everything the query service is allowed to read.

    Deliberately a bag of *repositories* and read services. Nothing in here can
    commit, encode, practise or send. Phases 6-12 add fields; they do not add
    engines.
    """

    state: Any = None
    events: Any = None
    conversations: Any = None
    traces: Any = None
    memories: Any = None
    memory_inspector: Any = None
    failures: Any = None
    runs: Any = None
    llm_calls: Any = None
    beliefs: Any = None
    self_model: Any = None
    personality: Any = None
    values: Any = None
    narrative: Any = None
    adaptations: Any = None
    consolidations: Any = None
    drift: Any = None
    activities: Any = None
    sleep: Any = None
    jobs: Any = None
    proactive: Any = None
    goals: Any = None
    habits: Any = None
    plans: Any = None
    decisions: Any = None
    npcs: Any = None
    npc_relationships: Any = None
    npc_interactions: Any = None
    groups: Any = None
    knowledge: Any = None
    acquisitions: Any = None
    health: Any = None
    manifests: Any = None
    rebuild: Any = None
    common_ground: Any = None
    admin_actions: Any = None
    runtime_ticks: Any = None
    proactive_deliberations: Any = None
    #: Phase 9. Both are read-only collaborators: the engine only
    #: assesses, and the source only looks for a trigger.
    proactive_engine: Any = None
    proactive_source: Any = None
    backups: Any = None
    #: Phase 10 built these. The declared-and-``None`` convention stays for
    #: whatever lands next: a declared source that is ``None`` has not been
    #: built yet, and an *undeclared* name is a typo.
    diary: Any = None
    life_days: Any = None
    gaps: Any = None
    searches: Any = None
    tools: Any = None
    genesis_runs: Any = None
    life_records: Any = None
    generation_audits: Any = None
    genesis_experiences: Any = None
    first_boot: Any = None
    #: Phase 14. The controller answers "what mode", the repository answers
    #: "what has she nearly done".
    shadow: Any = None
    shadow_decisions: Any = None
    #: Phase 15. The go-live gate, read-only like everything here.
    live: Any = None


class DebugQueryService:
    """Answers admin questions. Writes nothing, ever."""

    name = "debug_query_service"

    def __init__(self, sources: DebugSources, *, clock: Clock | None = None) -> None:
        self._sources = sources
        self._clock = clock or SystemClock()

    # --- state ---------------------------------------------------------------
    def state(self, domain: str | None = None) -> DebugResult:
        repository = self._require("state")
        values = (
            repository.list_domain(domain) if domain else repository.list_all()
        )
        rows = [
            {
                "domain": value.domain,
                "key": value.key,
                "value": value.value,
                "version": value.version,
                "updated_at": _iso(value.updated_at),
            }
            for value in values
        ]
        return DebugResult.of(
            "state",
            summary=f"{len(rows)} value(s)" + (f" in {domain}" if domain else ""),
            rows=rows[:MAX_LIMIT],
            title=domain or "state",
        )

    def domain(self, name: str) -> DebugResult:
        """One state domain — emotion, mood, needs, relationship, and so on."""
        result = self.state(name)
        return DebugResult(
            command=name, sections=result.sections, summary=result.summary
        )

    # --- conversation --------------------------------------------------------
    def events(self, *, limit: int = DEFAULT_LIMIT) -> DebugResult:
        store = self._require("events")
        rows = [
            {
                "event_id": event.event_id,
                "type": event.event_type,
                "category": event.category,
                "actor": event.actor_type,
                "origin": event.origin,
                "occurred_at": _iso(event.occurred_at),
            }
            for event in store.recent(limit=_bounded(limit))
        ]
        return DebugResult.of("events", summary=f"{len(rows)} event(s)", rows=rows)

    def runs(self, *, limit: int = DEFAULT_LIMIT) -> DebugResult:
        repository = self._require("runs")
        rows = [
            safe_row(
                row,
                fields=(
                    "run_id",
                    "root_event_id",
                    "status",
                    "started_at",
                    "finished_at",
                ),
            )
            for row in repository.recent(limit=_bounded(limit))
        ]
        return DebugResult.of("runs", summary=f"{len(rows)} run(s)", rows=rows)

    def failures(self, *, limit: int = DEFAULT_LIMIT) -> DebugResult:
        repository = self._require("failures")
        rows = [
            safe_row(
                row,
                fields=(
                    "failure_type",
                    "component",
                    "reason_code",
                    "severity",
                    "occurred_at",
                ),
            )
            for row in repository.recent(limit=_bounded(limit))
        ]
        return DebugResult.of("failures", summary=f"{len(rows)} failure(s)", rows=rows)

    def trace(self, *, limit: int = DEFAULT_LIMIT) -> DebugResult:
        repository = self._require("traces")
        rows = [
            safe_row(
                row,
                fields=(
                    "trace_id",
                    "outcome",
                    "total_ms",
                    "queue_wait_ms",
                    "inference_ms",
                    "model_calls",
                    "received_at",
                ),
            )
            for row in repository.recent(limit=_bounded(limit))
        ]
        return DebugResult.of("trace", summary=f"{len(rows)} turn(s)", rows=rows)

    def latency(self, *, limit: int = 200) -> DebugResult:
        repository = self._require("traces")
        samples = sorted(repository.latencies(limit=_bounded(limit, MAX_LIMIT * 10)))
        if not samples:
            return DebugResult(
                command="latency", summary="no conversation turns recorded yet"
            )
        return DebugResult.of(
            "latency",
            summary=f"{len(samples)} turn(s)",
            rows=[
                {
                    "median_ms": _percentile(samples, 50),
                    "p95_ms": _percentile(samples, 95),
                    "max_ms": samples[-1],
                }
            ],
        )

    def llm(self, *, limit: int = DEFAULT_LIMIT) -> DebugResult:
        """Model health only. Never a prompt, never a response body."""
        repository = self._require("llm_calls")
        rows = [
            safe_row(
                row,
                fields=("purpose", "model", "status", "latency_ms", "queue_wait_ms", "started_at"),
            )
            for row in repository.recent(limit=_bounded(limit))
        ]
        return DebugResult.of("llm", summary=f"{len(rows)} call(s)", rows=rows)

    # --- memory --------------------------------------------------------------
    def memory(self, *, limit: int = DEFAULT_LIMIT) -> DebugResult:
        repository = self._require("memories")
        rows = [
            {
                "memory_id": memory.memory_id,
                "summary": memory.summary,
                "importance": round(memory.importance, 3),
                "accessibility": round(memory.accessibility, 3),
                "status": memory.status,
                "occurred_at": _iso(memory.occurred_at),
            }
            for memory in repository.recent_memories(limit=_bounded(limit))
        ]
        return DebugResult.of(
            "memory",
            summary=f"{repository.memory_count()} memory row(s)",
            rows=rows,
        )

    async def memory_find(self, query: str, *, mode: str | None = None) -> DebugResult:
        """Phase 2 §2Q. The inspector, not ``recall`` — a debug search that
        practised what it found would make every question about memory change
        the answer to the next one."""
        inspector = self._require("memory_inspector")
        report = await inspector.preview_retrieval(
            query, mode=RecallMode(mode) if mode else None
        )
        rows = [
            {
                "memory_id": candidate.memory_id,
                "found_by": ", ".join(candidate.reasons),
                "relevance": (
                    judgement.relevance
                    if (judgement := report.judgement_for(candidate.memory_id))
                    else "-"
                ),
                "accessibility": round(candidate.memory.accessibility, 3),
                "selected": candidate.memory_id in report.selected_ids,
                "practice_applied": False,
            }
            for candidate in report.candidates
        ]
        return DebugResult.of(
            "memory find",
            summary=(
                f"mode={report.mode.value} candidates={len(report.candidates)} "
                f"selected={len(report.selected)} relevance={report.relevance_source}"
            ),
            rows=rows,
        )

    def proactive_dryrun(self) -> DebugResult:
        """What would happen if she considered reaching out right now (28.1).

        Read-only in the strict sense the OWNER specified for Phase 5: no
        ``proactive_contacts`` row, no decision, no deliberation row, no
        opportunity consumed, no event — and no model call either. It evaluates
        the *hard gate* and reports the trigger scan, which is the part an
        operator actually needs to see and the only part with no side effects
        at all.

        The LLM judgment of 28.2 is deliberately not run here. It would cost a
        model call and, more to the point, asking "would you want to send
        something?" thirty times is not a question with a stable answer.
        """
        engine = self._sources.proactive_engine
        source = self._sources.proactive_source
        if engine is None or source is None:
            return DebugResult(
                command="proactive dryrun",
                summary="proactive contact is not wired yet in this phase",
            )

        now = self._clock.now()
        trigger = source.find_trigger(now)
        rows: list[dict[str, Any]] = [
            {
                "trigger": "none" if trigger is None else trigger.kind,
                "detail": "" if trigger is None else trigger.detail,
            }
        ]
        if trigger is not None:
            opportunity = Opportunity(
                kind="proactive_contact",
                detail=f"{trigger.kind}:{trigger.detail}",
                created_at=now,
            )
            assessment = engine.assess(opportunity, _NoState(), now=now)
            rows.append(
                {
                    "gate": "pass" if assessment.allowed else "block",
                    "reason": assessment.reason,
                    "desire": round(assessment.desire, 4),
                    "unanswered": assessment.unanswered,
                    "required_wait_h": round(assessment.required_wait_hours, 2),
                }
            )
        return DebugResult.of(
            "proactive dryrun",
            summary="nothing was decided, sent or recorded",
            rows=rows,
        )

    def knowledge_find(self, query: str) -> DebugResult:
        """What is known about a topic, and where it came from.

        Provenance is the point: for the Genesis leakage audit, "what does she
        know" is much less useful than "when could she have known it, and on
        whose word". Read-only — it does not search.
        """
        repository = self._sources.knowledge
        if repository is None:
            return DebugResult(
                command="knowledge find", summary="knowledge is not wired yet"
            )
        needle = (query or "").strip()
        rows: list[dict[str, Any]] = []
        for item in repository.all_knowledge(limit=200):
            if needle and needle not in item.statement and needle not in item.topic:
                continue
            provenance = repository.provenance(item.knowledge_id) or {}
            rows.append(
                safe_row(
                    {
                        "statement": item.statement,
                        "topic": item.topic,
                        "available_from": _iso(item.available_from),
                        "source_type": provenance.get("source_type", ""),
                        "source_url": provenance.get("source_url") or "",
                        "retrieved_at": provenance.get("retrieved_at") or "",
                        "confidence": round(item.truth_confidence, 3),
                    },
                    fields=(
                        "statement",
                        "topic",
                        "available_from",
                        "source_type",
                        "source_url",
                        "retrieved_at",
                        "confidence",
                    ),
                )
            )
            if len(rows) >= MAX_LIMIT:
                break
        return DebugResult.of(
            "knowledge find",
            summary=f"{len(rows)} item(s) matching {needle!r}" if needle else f"{len(rows)} item(s)",
            rows=rows,
        )

    def appraisal(self, *, limit: int = DEFAULT_LIMIT) -> DebugResult:
        """What recent events were read as. Reads ``llm_calls``, not the engine."""
        repository = self._require("llm_calls")
        rows = [
            safe_row(row, fields=("event_id", "status", "latency_ms", "started_at"))
            for row in repository.recent(limit=_bounded(limit) * 4)
            if row["purpose"] == "appraisal"
        ]
        return DebugResult.of(
            "appraisal", summary=f"{len(rows)} appraisal call(s)", rows=rows[:limit]
        )

    # --- simple listings -----------------------------------------------------
    def listing(
        self,
        command: str,
        source: str,
        method: str,
        fields: Sequence[str],
        *,
        limit: int = DEFAULT_LIMIT,
        **kwargs: Any,
    ) -> DebugResult:
        """A generic 'show me the recent rows of X'.

        Phases 6-12 add their views by naming a source, a read method and the
        fields worth printing — not by writing another handler.
        """
        if not hasattr(self._sources, source):
            # An undeclared source is a registry typo, and a typo must be loud:
            # a silent "not wired yet" would hide a broken command for a whole
            # phase. ``test_every_listing_names_something_real`` catches these
            # before they reach a channel.
            return DebugResult.failure(command, f"{source} is not a debug source")
        repository = getattr(self._sources, source)
        if repository is None:
            return DebugResult(
                command=command, summary=f"{source} is not wired yet in this phase"
            )
        reader = getattr(repository, method, None)
        if reader is None:
            return DebugResult.failure(command, f"{source}.{method} does not exist")
        items = reader(limit=_bounded(limit), **kwargs) or ()
        rows = [safe_row(item, fields=fields) for item in items]
        return DebugResult.of(command, summary=f"{len(rows)} row(s)", rows=rows)

    # --- system --------------------------------------------------------------
    def status(self) -> DebugResult:
        rows: list[dict[str, Any]] = []
        manifests = self._sources.manifests
        if manifests is not None:
            current = manifests.latest()
            if current is not None:
                rows.append(
                    safe_row(
                        current,
                        fields=("manifest_id", "schema_version", "policy_version"),
                    )
                )
        events = self._sources.events
        if events is not None:
            rows.append({"events": events.count()})
        memories = self._sources.memories
        if memories is not None:
            rows.append({"memories": memories.memory_count()})
        return DebugResult.of("status", summary="runtime status", rows=rows)

    def version(self) -> DebugResult:
        manifests = self._sources.manifests
        current = None if manifests is None else manifests.latest()
        if current is None:
            return DebugResult(command="version", summary="no manifest recorded yet")
        return DebugResult.of(
            "version",
            summary="runtime manifest",
            rows=[
                safe_row(
                    current,
                    fields=(
                        "manifest_id",
                        "schema_version",
                        "policy_version",
                        "created_at",
                    ),
                )
            ],
        )

    def first_boot(self) -> DebugResult:
        """What the FIRST BOOT Authority says. Read-only, like everything here.

        Point 41: this shows, it does not start. Beginning a nineteen-year
        generation from a chat message is not a capability worth having — the
        CLI is the only place it can be asked for, and that is a feature.
        """
        orchestrator = self._sources.first_boot
        if orchestrator is None:
            return DebugResult(
                command="firstboot", summary="first boot is not wired yet"
            )
        view = orchestrator.progress()
        rows = [
            {
                "status": view.status,
                "recovery_required": view.recovery_required,
                "run": view.genesis_run_id or "-",
                "stage": view.stage or "-",
                "years": f"{view.life_year}/{view.expected_years}",
                "months": f"{view.months_done}/{view.expected_months}",
            },
            {
                "extracted": view.experiences_extracted,
                "replayed": view.experiences_replayed,
                "memories": view.memories,
                "critics_passed": view.critics_passed,
                "critics_failed": view.critics_failed,
            },
            {
                "last_checkpoint": view.last_checkpoint or "-",
                "last_progress": view.last_progress_at or "-",
                "attempts": view.attempt_count,
                "lease": view.lease_holder or "-",
            },
        ]
        if view.block_kind:
            rows.append({"blocked": view.block_kind, "reason": view.block_reason})
        return DebugResult.of(
            "firstboot",
            summary=f"{view.status}"
            + (" (recovery required)" if view.recovery_required else ""),
            rows=rows,
        )

    def live(self) -> DebugResult:
        """The go-live checklist (spec 50 Phase 15, 54).

        Read-only, and deliberately so: throwing the switch is a configuration
        change the OWNER makes on the machine, not something a chat message can
        do. This says what is stopping it.
        """
        gate = self._sources.live
        if gate is None:
            return DebugResult(command="live", summary="the go-live gate is not wired yet")
        report = gate.check()
        rows = [
            {
                "check": check.name,
                "ok": check.passed,
                "blocking": check.blocks,
                "detail": check.detail,
            }
            for check in report.checks
        ]
        return DebugResult.of(
            "live",
            summary=(
                "READY" if report.ready else f"NOT READY ({len(report.blockers)} blocker(s))"
            ),
            rows=rows,
        )

    def shadow(self) -> DebugResult:
        """The shadow review surface (spec 47).

        Two things at once, because they answer different questions: the modes
        say what is switched on, and the tally says whether shadow mode has
        actually seen anything. A capability in SHADOW with `wanted = 0` has
        not been evaluated — it has merely been enabled, and reporting the
        first as the second is how a review gets skipped.
        """
        controller = self._sources.shadow
        repository = self._sources.shadow_decisions
        if controller is None or repository is None:
            return DebugResult(command="shadow", summary="shadow modes are not wired yet")
        modes = controller.as_dict()
        rows = [{"capability": name, "mode": mode} for name, mode in sorted(modes.items())]
        seen = {row["capability"] for row in repository.tally()}
        for row in rows:
            row["evaluated"] = row["capability"] in seen
        tally = [
            {
                "capability": row["capability"],
                "mode": row["mode"],
                "considered": row["considered"],
                "wanted": row["wanted"] or 0,
                "acted": row["acted"] or 0,
                "last_at": row["last_at"],
            }
            for row in repository.tally()
        ]
        unreviewed = repository.unreviewed_count()
        return DebugResult.of(
            "shadow",
            summary=f"{len(rows)} capability(ies); {unreviewed} decision(s) awaiting review",
            rows=rows + tally,
        )

    def genesis(self) -> DebugResult:
        repository = self._sources.rebuild
        if repository is None:
            return DebugResult(command="genesis", summary="no rebuild epoch recorded")
        epoch = repository.current()
        if epoch is None:
            return DebugResult(command="genesis", summary="no rebuild epoch recorded")
        return DebugResult.of(
            "genesis",
            summary="current rebuild epoch",
            rows=[
                safe_row(
                    epoch,
                    fields=("epoch_id", "started_at", "reason", "genesis_status"),
                )
            ],
        )

    # --- helpers -------------------------------------------------------------
    def _require(self, name: str) -> Any:
        source = getattr(self._sources, name, None)
        if source is None:
            raise LookupError(f"{name} is not available")
        return source


def _bounded(limit: int, ceiling: int = MAX_LIMIT) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, ceiling))


def _iso(value: Any) -> str:
    return "" if value is None else str(value)


def _percentile(samples: Sequence[int], percent: float) -> int:
    """Nearest-rank, the same definition the ``latency`` command already uses."""
    if not samples:
        return 0
    index = max(1, int(round(percent / 100.0 * len(samples)))) - 1
    return int(samples[min(index, len(samples) - 1)])


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "DebugQueryService", "DebugSources"]


class _NoState:
    """A state view that knows nothing.

    The dry run must not read a snapshot: taking one is cheap but it is still
    a decision about *when* state was observed, and a debug command should not
    be a point in her timeline. Defaults make the gate report its structural
    verdict — cooldown, backoff, quiet hours — which is what is being asked.
    """

    def number(self, domain: str, key: str, default: float = 0.0) -> float:
        return default
