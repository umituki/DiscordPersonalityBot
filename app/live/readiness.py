"""Going live (rebuild spec 50 Phase 15, 54).

    Phase 15 — Live
    Discord Character mode 開始。

One sentence in the spec, and the whole rebuild behind it. This module is the
gate between "everything is implemented" and "a real person is talking to her",
and it exists because those are different claims and only the first one has
tests.

§54's Definition of Done is mostly about work that has already happened —
acceptance suites, blind evaluation, restart recovery. Those are conditions on
the *repository*, checked by its test suite. What is left for a running process
to check is narrower and different in kind: is this database the one that
finished a Genesis, is the model reachable, is there a backup to fall back to,
has every capability that is about to go live actually been watched in shadow
first.

That last one is the point of the phase. Phase 14 built the shadow machinery;
its value is only realised if something refuses to go live on a capability that
has never been observed. 「shadow/live pathways tested」 is not satisfied by a
mode nobody switched.

The gate reports rather than raises, and every check names itself, so an
operator gets the whole list at once rather than one restart at a time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from app.runtime.shadow import SHADOW_CAPABILITIES

logger = logging.getLogger(__name__)

MODULE = "live_readiness"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""
    #: A check that does not block going live but that the OWNER should see.
    advisory: bool = False

    @property
    def blocks(self) -> bool:
        return not self.passed and not self.advisory

    def describe(self) -> str:
        if self.passed:
            mark = "ok"
        else:
            mark = "warn" if self.advisory else "FAIL"
        return f"[{mark}] {self.name}{': ' + self.detail if self.detail else ''}"


@dataclass(frozen=True, slots=True)
class LiveReport:
    checks: tuple[Check, ...]

    @property
    def ready(self) -> bool:
        return not any(check.blocks for check in self.checks)

    @property
    def blockers(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.blocks)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(
            check for check in self.checks if not check.passed and check.advisory
        )

    def describe(self) -> str:
        head = "READY" if self.ready else "NOT READY"
        lines = [f"live: {head}"]
        lines.extend(check.describe() for check in self.checks)
        return "\n".join(lines)


class LiveReadiness:
    """Whether Discord Character mode may start.

    Deliberately not an authority over anything else. It reads — first boot,
    the contracts, the shadow record, the model, the backups — and answers one
    question. Everything it consults keeps its own authority over its own
    domain, which is why this can be run at any time without side effects.
    """

    name = MODULE

    def __init__(
        self,
        *,
        first_boot: Any,
        shadow: Any,
        shadow_decisions: Any,
        contracts: Any = None,
        backups: Any = None,
        runtime: Any = None,
        ticks: Any = None,
        #: The rebuild epoch, the life months and the staged experiences —
        #: read to answer one question: was any of this produced under the
        #: world model in force now?
        rebuild: Any = None,
        world_provenance: Any = None,
        owner_id: str = "",
        channel_id: str = "",
        token: str | None = None,
        llm_healthy: bool | None = None,
        schema_version: int = 0,
        latest_schema: int = 0,
        enabled: bool = False,
    ) -> None:
        self._first_boot = first_boot
        self._shadow = shadow
        self._shadow_decisions = shadow_decisions
        self._contracts = contracts
        self._backups = backups
        self._runtime = runtime
        self._ticks = ticks
        self._rebuild = rebuild
        self._world_provenance = world_provenance
        self._owner_id = owner_id
        self._channel_id = channel_id
        self._token = token
        self._llm_healthy = llm_healthy
        self._schema_version = schema_version
        self._latest_schema = latest_schema
        self._enabled = enabled

    # --- the one question ----------------------------------------------------
    def character_plane_open(self) -> bool:
        """What the Discord gateway asks at the door.

        The same method name the FIRST BOOT Authority answers, because the
        gateway must keep asking exactly one object exactly one question. Phase
        15 makes the answer stricter; it does not make it plural.
        """
        try:
            return self.check().ready
        except Exception:  # noqa: BLE001 - an unreadable gate stays shut
            logger.exception("could not evaluate live readiness")
            return False

    def check(self) -> LiveReport:
        checks: list[Check] = [
            self._first_boot_complete(),
            self._schema_is_latest(),
            self._explicitly_enabled(),
            self._owner_configured(),
            self._token_present(),
            self._model_reachable(),
            self._capabilities_verified(),
            self._no_dead_subsystem(),
            self._backup_available(),
            self._world_model_is_current(),
        ]
        checks.extend(self._shadow_evaluated())
        return LiveReport(checks=tuple(checks))

    # --- checks ---------------------------------------------------------------
    def _world_model_is_current(self) -> Check:
        """Was this life produced under the world model in force now?

        The migration that added the provenance columns could not answer this,
        which is the whole reason the columns exist. A month generated under
        the old substring critic came out of migration 36 with
        `participants=[]` and `interaction_scope=local` — structurally
        identical to a month the current validator had actually passed. Only a
        version stamped by the code that ran the validation tells them apart.
        
        A mismatch is not repaired here and never repaired automatically:
        inferring what an old month meant is the structure the world model
        replaced. It is reported, and the resolution is an explicit
        rebuild-reset.
        """
        if self._world_provenance is None:
            # Fail closed. This is a hard go-live dependency, and "the reader
            # was not wired" is indistinguishable from "nothing was checked" —
            # which is the state it exists to detect. A wiring regression must
            # not read as a pass.
            return Check(
                "world_model_current", False, "world provenance reader is not wired"
            )
        try:
            report = self._world_provenance()
        except Exception as exc:  # noqa: BLE001
            return Check("world_model_current", False, repr(exc)[:120])
        if report.blocking:
            return Check(
                "world_model_current",
                False,
                "rebuild_required_world_model_version: "
                + "; ".join(report.blocking),
            )
        if report.advisory:
            # Reported, not blocking. A legacy Common Ground row cannot become
            # a fact — the correction boundary already refuses it — so holding
            # the door over one would stop her talking about nothing.
            return Check(
                "world_model_current",
                True,
                f"world model v{report.current}; legacy rows present: "
                + "; ".join(report.advisory),
                advisory=True,
            )
        return Check(
            "world_model_current", True, f"world model v{report.current}"
        )

    def _first_boot_complete(self) -> Check:
        """The hard one. Nothing else on this list matters if she does not
        exist yet, and Phase 13 already refuses in that case — this repeats it
        so the go-live report explains itself rather than failing obscurely."""
        status = "PENDING"
        try:
            status = self._first_boot.status()
        except Exception as exc:  # noqa: BLE001
            return Check("first_boot_complete", False, repr(exc)[:120])
        return Check(
            "first_boot_complete", status == "COMPLETE", f"first boot is {status}"
        )

    def _schema_is_latest(self) -> Check:
        ok = self._schema_version == self._latest_schema
        return Check(
            "schema_is_latest",
            ok,
            f"schema {self._schema_version}, latest {self._latest_schema}",
        )

    def _explicitly_enabled(self) -> Check:
        """Going live is a decision somebody makes, not a state drifted into.

        Everything else here can become true on its own — a Genesis finishes, a
        backup is taken. This one cannot, which is the point.
        """
        return Check(
            "live_enabled",
            self._enabled,
            "runtime.live is false; character mode stays closed",
        )

    def _owner_configured(self) -> Check:
        ok = bool(self._owner_id) and bool(self._channel_id)
        return Check(
            "owner_configured", ok, "the single USER and channel must both be set"
        )

    def _token_present(self) -> Check:
        return Check(
            "discord_token", bool(self._token), "no DISCORD_BOT_TOKEN is configured"
        )

    def _model_reachable(self) -> Check:
        """Advisory. A degraded model is a bad first conversation, not an
        unsafe one, and refusing to start makes the failure harder to see."""
        if self._llm_healthy is None:
            return Check("model_reachable", False, "not checked yet", advisory=True)
        return Check(
            "model_reachable",
            bool(self._llm_healthy),
            "the local model is not answering",
            advisory=True,
        )

    def _capabilities_verified(self) -> Check:
        """§54: ``Implementation Ledger all required IDs E2E_VERIFIED``."""
        if self._contracts is None:
            return Check("capabilities_verified", False, "contracts were not loaded")
        unfinished = sorted(
            name
            for name, contract in self._contracts.items()
            if not getattr(contract, "complete", False)
        )
        return Check(
            "capabilities_verified",
            not unfinished,
            "not yet verified: " + ", ".join(unfinished[:6]) if unfinished else "",
        )

    def _no_dead_subsystem(self) -> Check:
        """§54: ``no required dead subsystem``.

        The runtime's own audit: an opportunity kind that no builder claims is
        a source firing into nothing, which is exactly the shape the whole
        rebuild exists to prevent.
        """
        if self._ticks is None:
            return Check("no_dead_subsystem", True, "no ticks recorded yet")
        try:
            rows = self._ticks.recent(limit=50)
        except Exception as exc:  # noqa: BLE001
            return Check("no_dead_subsystem", False, repr(exc)[:120])
        unclaimed: set[str] = set()
        for row in rows:
            for kind in (row["unclaimed_kinds"] or "").split(","):
                if kind.strip():
                    unclaimed.add(kind.strip())
        return Check(
            "no_dead_subsystem",
            not unclaimed,
            "opportunity kinds nothing claims: " + ", ".join(sorted(unclaimed)),
        )

    def _backup_available(self) -> Check:
        if self._backups is None:
            return Check("backup_available", False, "no backup record", advisory=True)
        try:
            recent = self._backups.recent(limit=1)
        except Exception as exc:  # noqa: BLE001
            return Check("backup_available", False, repr(exc)[:120], advisory=True)
        return Check(
            "backup_available",
            bool(recent),
            "no backup has ever been taken",
            advisory=True,
        )

    def _shadow_evaluated(self) -> Sequence[Check]:
        """§47 → §50. A capability may not go LIVE unevaluated.

        This is the check the phase is for. Shadow mode costs nothing to enable
        and everything to ignore: a capability switched straight from OFF to
        LIVE has had exactly as much review as one that was never built, and
        the mode setting alone cannot tell the difference. So the evidence is
        the record — she wanted to, at least once, while somebody was watching.
        """
        checks: list[Check] = []
        try:
            tally = {
                row["capability"]: row for row in self._shadow_decisions.tally()
            }
        except Exception as exc:  # noqa: BLE001
            return [Check("shadow_evaluated", False, repr(exc)[:120])]
        for capability in SHADOW_CAPABILITIES:
            mode = self._shadow.mode(capability)
            if mode != "LIVE":
                continue
            row = tally.get(capability)
            wanted = 0 if row is None else int(row["wanted"] or 0)
            checks.append(
                Check(
                    f"shadow_evaluated:{capability}",
                    wanted > 0,
                    "is LIVE and was never observed wanting to act in shadow",
                )
            )
        if not checks:
            checks.append(
                Check(
                    "shadow_evaluated",
                    True,
                    "nothing is live yet; all four stay in shadow",
                )
            )
        return checks


__all__ = ["Check", "LiveReadiness", "LiveReport", "MODULE"]
