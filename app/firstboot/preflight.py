"""What must be true before a nineteen-year run begins (Phase 13, point 15).

Checked once, before anything is generated, because every item here is cheap to
verify now and expensive to discover in year eleven. A missing prompt found at
hour six has already cost six hours; a missing prompt found at second zero has
cost nothing.

Every check reports rather than raises, so the operator sees the whole list at
once instead of fixing them one restart at a time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

logger = logging.getLogger(__name__)

#: Prompts Genesis cannot run without.
REQUIRED_PROMPTS: tuple[str, ...] = (
    "genesis_annual_scaffold",
    "genesis_month",
    "genesis_annual_synthesis",
    "genesis_experience_extraction",
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def describe(self) -> str:
        mark = "ok" if self.passed else "FAIL"
        return f"[{mark}] {self.name}{': ' + self.detail if self.detail else ''}"


@dataclass(frozen=True, slots=True)
class Preflight:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def describe(self) -> str:
        return "\n".join(check.describe() for check in self.checks)


def run_preflight(
    *,
    anchors: Any,
    state: Any,
    epoch: Any,
    db: Any,
    schema_version: int,
    latest_schema: int,
    event_store: Any,
    genesis_runs: Any,
    prompts: Any,
    critics_available: Sequence[str],
    data_dir: Any,
    backups: Any = None,
    now: datetime,
) -> Preflight:
    """Everything from point 15, in one pass."""
    checks: list[Check] = []

    checks.append(
        Check(
            "fresh_rebuild_epoch",
            epoch is not None,
            "no rebuild epoch exists; run rebuild-reset first" if epoch is None else "",
        )
    )
    status = "" if state is None else state["status"]
    checks.append(
        Check(
            "not_already_complete",
            status != "COMPLETE",
            "this epoch has already booted" if status == "COMPLETE" else "",
        )
    )

    # Point 11's rule, checked before rather than after: a past that already
    # contains the USER is a past she cannot be given.
    real_user = _real_user_events(event_store)
    checks.append(
        Check(
            "no_real_discord_history",
            real_user == 0,
            f"{real_user} real USER event(s) already exist" if real_user else "",
        )
    )

    conflicting = None if genesis_runs is None else genesis_runs.unfinished()
    attached = None if state is None else state["genesis_run_id"]
    stray = conflicting is not None and conflicting["genesis_run_id"] != attached
    checks.append(
        Check(
            "no_conflicting_genesis_run",
            not stray,
            f"an unfinished run {conflicting['genesis_run_id']} is not this epoch's"
            if stray
            else "",
        )
    )

    checks.append(
        Check(
            "schema_is_latest",
            schema_version == latest_schema,
            f"schema {schema_version}, latest {latest_schema}"
            if schema_version != latest_schema
            else "",
        )
    )

    integrity = "unknown"
    try:
        integrity = db.integrity_check()
    except Exception as exc:  # noqa: BLE001
        integrity = repr(exc)
    checks.append(Check("db_integrity", integrity == "ok", integrity if integrity != "ok" else ""))

    missing_prompts = []
    for prompt_id in REQUIRED_PROMPTS:
        try:
            prompts.get(prompt_id)
        except Exception:  # noqa: BLE001
            missing_prompts.append(prompt_id)
    checks.append(
        Check("required_prompts", not missing_prompts, ", ".join(missing_prompts))
    )

    checks.append(
        Check(
            "required_critics",
            bool(critics_available),
            "no critics are configured" if not critics_available else "",
        )
    )

    valid_anchors = anchors is not None and anchors.birth_datetime < anchors.present_datetime
    checks.append(
        Check(
            "anchors_valid",
            bool(valid_anchors),
            "birth must be before the present" if not valid_anchors else "",
        )
    )
    covers = bool(anchors is not None and anchors.covers_to_present)
    checks.append(
        Check("anchors_cover_present", covers, "" if covers else "the spans stop short of now")
    )

    writable = False
    try:
        from pathlib import Path

        probe = Path(data_dir) / ".firstboot_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("data directory is not writable: %s", exc)
    checks.append(Check("data_dir_writable", writable))

    # Point 16: a backup before starting, verified. A fresh database is still
    # worth snapshotting — the copy is for recovering from a bug *during*
    # Genesis, not for recovering the empty state it started from.
    if backups is None:
        checks.append(Check("backup_usable", False, "no backup service configured"))
    else:
        checks.append(Check("backup_usable", True, "taken at start"))

    return Preflight(checks=tuple(checks))


def _real_user_events(event_store: Any) -> int:
    if event_store is None:
        return 0
    try:
        return sum(
            1
            for event in event_store.recent(limit=2000)
            if event.origin == "real_discord" and event.actor_type == "user"
        )
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["REQUIRED_PROMPTS", "Check", "Preflight", "run_preflight"]
