"""Admin Control Plane (spec 30).

The three rules this module exists to make true:

* **Character Mode and Admin Mode are completely separate.** Admin operations
  arrive here, never through conversation, and every one of them is recorded.
* **"Forget that" in conversation is not a hard delete.** The conversational
  request maps to *suppression* — unreachable by recall, still on disk — and
  :meth:`AdminControlPlane.from_conversation` exists to make that mapping the
  only one available from that direction.
* **YUI has no admin tool.** :func:`assert_not_yui` refuses any operation whose
  actor is YUI, and the tool registry never receives an admin tool.

A destructive operation walks the whole sequence of spec 30::

    impact analysis → dry-run / preview → confirmation → pre-operation snapshot
    → mutation → cascade re-evaluation → validation → audit

Each step is recorded before the next begins, so an operation interrupted
half-way leaves a record saying exactly how far it got.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.admin.models import OPERATION_RISK, AdminAction, ImpactAnalysis, RiskClass
from app.clock import Clock, SystemClock
from app.memory.engine import MemoryEngine
from app.storage.backup import BackupService
from app.storage.repositories.admin import AdminActionRepository, MemoryAdminRepository

logger = logging.getLogger(__name__)

MODULE = "admin_control_plane"

#: Spec 30: YUI must never hold this authority.
YUI_ACTOR = "yui"

#: What a conversational "forget that" is allowed to mean (spec 30).
CONVERSATIONAL_OPERATION = "suppress"


class AdminRefused(PermissionError):
    """Raised when an operation may not proceed as asked."""


def assert_not_yui(actor: str) -> None:
    """Spec 30: ``YUI 自身に Admin Tool 権限を与えない``."""
    if actor.strip().lower() == YUI_ACTOR:
        raise AdminRefused(
            "YUI has no admin authority; admin operations belong to the owner "
            "through the admin control plane (spec 30)"
        )


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    action: AdminAction
    impact: ImpactAnalysis
    applied: bool
    preview: dict[str, Any]

    @property
    def refused(self) -> bool:
        return self.action.refused


class AdminControlPlane:
    name = MODULE

    def __init__(
        self,
        *,
        actions: AdminActionRepository,
        memories: MemoryAdminRepository,
        memory: MemoryEngine,
        backups: BackupService,
        clock: Clock | None = None,
    ) -> None:
        self._actions = actions
        self._memories = memories
        self._memory = memory
        self._backups = backups
        self._clock = clock or SystemClock()

    # --- the conversational boundary (spec 30) ------------------------------
    def from_conversation(self, memory_id: str, *, actor: str) -> OperationOutcome:
        """What "忘れて" in a normal conversation is permitted to do.

        Suppression only. Nothing said in character mode can reach a
        destructive operation, whatever words are used (spec 30).
        """
        assert_not_yui(actor)
        return self.memory_operation(
            CONVERSATIONAL_OPERATION,
            memory_id,
            actor=actor,
            reason="conversational request",
            dry_run=False,
        )

    # --- operations ---------------------------------------------------------
    def memory_operation(
        self,
        operation: str,
        memory_id: str,
        *,
        actor: str,
        reason: str = "",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> OperationOutcome:
        """Run one memory admin operation at the ceremony its risk demands."""
        assert_not_yui(actor)
        risk = OPERATION_RISK.get(operation)
        if risk is None:
            raise AdminRefused(f"unknown admin operation: {operation!r}")

        action = self._open(
            operation=operation,
            risk=risk,
            target_type="memory",
            target_id=memory_id,
            dry_run=dry_run,
            reason=reason,
        )

        # 1. impact analysis, always, before anything is touched.
        impact = self._analyse_memory(memory_id, operation)
        action = self._advance(action, "impact_analysed", impact=impact.model_dump())

        preview = {
            "operation": operation,
            "risk_class": risk,
            "target": memory_id,
            "would_affect": impact.total,
            "reversible": impact.reversible,
        }
        action = self._advance(action, "previewed", result=preview)

        if dry_run:
            # A dry run ends here, recorded, having changed nothing.
            self._advance(action, "audited", result={**preview, "dry_run": True})
            return OperationOutcome(self._get(action.action_id), impact, False, preview)

        if risk == "DESTRUCTIVE":
            if not confirm:
                refused = self._advance(
                    action,
                    "refused",
                    result={**preview, "refusal": "confirmation required"},
                )
                logger.warning(
                    "destructive admin operation refused: no confirmation (%s)", operation
                )
                return OperationOutcome(refused, impact, False, preview)
            action = self._advance(action, "confirmed", confirmed_by=actor)

            # 4. pre-operation snapshot. If it cannot be verified, nothing runs.
            backup = self._backups.before_operation(f"{operation}:{memory_id}")
            action = self._advance(
                action, "snapshotted", snapshot_path=str(backup.path)
            )

        # 5. mutation
        applied = self._apply(operation, memory_id)
        action = self._advance(action, "mutated", result={**preview, "applied": applied})

        # 6. cascade re-evaluation
        cascade = self._cascade(memory_id, operation)
        action = self._advance(action, "cascaded", result={**preview, "cascade": cascade})

        # 7. validation
        validation = self._validate(operation, memory_id)
        action = self._advance(
            action, "validated", result={**preview, "validation": validation}
        )

        # 8. audit
        audited = self._advance(
            action,
            "audited",
            result={**preview, "applied": applied, "validation": validation},
            completed=True,
        )
        logger.info(
            "admin operation completed op=%s risk=%s target=%s", operation, risk, memory_id
        )
        return OperationOutcome(audited, impact, applied, preview)

    # --- the steps ----------------------------------------------------------
    def _analyse_memory(self, memory_id: str, operation: str) -> ImpactAnalysis:
        direct = self._memories.exists(memory_id)
        cascade = self._memories.link_count(memory_id) + self._memories.retrieval_count(
            memory_id
        )
        return ImpactAnalysis(
            target_type="memory",
            target_id=memory_id,
            directly_affected=direct,
            cascade_affected=cascade,
            reversible=operation in ("suppress", "invalidate"),
            notes=(
                ("the objective archive keeps the underlying events either way",)
                if operation != "hard_delete"
                else ("this removes the subjective memory permanently",)
            ),
        )

    def _apply(self, operation: str, memory_id: str) -> bool:
        if operation == "suppress":
            self._memory.suppress(memory_id)
            return True
        if operation == "invalidate":
            self._memory.invalidate(memory_id)
            return True
        if operation == "redact":
            self._memory.revise(
                memory_id,
                new_summary="（削除された内容）",
                reason_code="admin_redaction",
            )
            self._memory.suppress(memory_id)
            return True
        if operation == "hard_delete":
            self._memories.hard_delete(memory_id)
            return True
        return False

    def _cascade(self, memory_id: str, operation: str) -> dict[str, int]:
        """Re-evaluate what pointed at the target (spec 30)."""
        return {
            "dangling_links": self._memories.link_count(memory_id),
            "operation_applied": 1,
        }

    def _validate(self, operation: str, memory_id: str) -> dict[str, Any]:
        """Confirm the world matches what the operation claimed to do."""
        status = self._memories.status_of(memory_id)
        if operation == "hard_delete":
            return {"status": status, "expected_absent": True, "ok": status is None}
        expected = {
            "suppress": "suppressed",
            "invalidate": "invalidated",
            "redact": "suppressed",
        }.get(operation)
        return {"status": status, "expected": expected, "ok": status == expected}

    # --- recording ----------------------------------------------------------
    def _open(
        self,
        *,
        operation: str,
        risk: RiskClass,
        target_type: str,
        target_id: str | None,
        dry_run: bool,
        reason: str,
    ) -> AdminAction:
        return self._actions.open(
            operation=operation,
            risk_class=risk,
            target_type=target_type,
            target_id=target_id,
            dry_run=dry_run,
            reason=reason,
            now=self._clock.now(),
        )

    def _advance(
        self,
        action: AdminAction,
        stage: str,
        *,
        impact: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        confirmed_by: str | None = None,
        snapshot_path: str | None = None,
        completed: bool = False,
    ) -> AdminAction:
        return self._actions.advance(
            action.action_id,
            stage,
            impact=impact,
            result=result,
            confirmed_by=confirmed_by,
            snapshot_path=snapshot_path,
            completed_at=self._clock.now() if completed else None,
        )

    def _get(self, action_id: str) -> AdminAction:
        action = self._actions.get(action_id)
        if action is None:  # pragma: no cover - the row was just written
            raise AdminRefused(f"admin action vanished: {action_id!r}")
        return action

    # --- reads --------------------------------------------------------------
    def history(self, *, limit: int = 50) -> list[AdminAction]:
        return self._actions.history(limit=limit)

    def for_target(self, target_id: str) -> list[AdminAction]:
        return self._actions.for_target(target_id)

    def count(self) -> int:
        return self._actions.count()

    @staticmethod
    def risk_of(operation: str) -> RiskClass | None:
        return OPERATION_RISK.get(operation)
