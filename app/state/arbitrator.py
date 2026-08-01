"""State arbitration (spec 9.4, 9.8, 12.3).

The arbitrator is pure: proposals plus the S0 snapshot plus policy in, an
accept/reject decision out. It performs no I/O, so its rules are directly
testable and the same rules apply to normal runs, admin operations and past
simulation.

Rejection is the safe outcome. Per the storage rule, an out-of-policy proposal
is **rejected and recorded**, never clamped into an acceptable range: a
functional degradation is preferable to committing an invalid state
(spec 2.12).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.state import dependency_graph, ownership
from app.state.policy import ArbitrationPolicy, DomainPolicy
from app.state.proposal import StateChangeProposal
from app.state.snapshot import StateSnapshot
from app.state.value import JSONValue

EPSILON = 1e-9


class RejectionReason:
    """Stable reason codes. Recorded in ``failures.reason_code``."""

    UNKNOWN_DOMAIN = "unknown_domain"
    NOT_STATE_OWNER = "not_state_owner"
    UNDECLARED_LAYER = "undeclared_layer"
    DEEP_UPDATE_REQUIRES_CONSOLIDATION = "deep_update_requires_consolidation"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_MAGNITUDE = "invalid_magnitude"
    UNINITIALIZED_TARGET = "uninitialized_target"
    NON_NUMERIC_TARGET = "non_numeric_target"
    CONFLICTING_OPERATIONS = "conflicting_operations"
    CONFLICTING_SET_VALUES = "conflicting_set_values"
    DELTA_EXCEEDS_POLICY = "delta_exceeds_policy"
    VALUE_OUT_OF_BOUNDS = "value_out_of_bounds"


@dataclass(frozen=True, slots=True)
class AcceptedChange:
    """One committable change, possibly merged from several proposals."""

    domain: str
    key: str
    operation: str
    previous_value: JSONValue
    new_value: JSONValue
    delta: float | None
    confidence: float | None
    expected_version: int | None
    source_module: str
    source_event_id: str
    proposal_id: str
    merged_proposal_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    @property
    def target(self) -> str:
        return f"{self.domain}.{self.key}"


@dataclass(frozen=True, slots=True)
class Rejection:
    reason_code: str
    detail: str
    proposal_ids: tuple[str, ...]
    domain: str
    key: str
    source_modules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArbitrationResult:
    accepted: tuple[AcceptedChange, ...] = ()
    rejected: tuple[Rejection, ...] = ()
    received: int = 0

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def rejected_proposal_count(self) -> int:
        return sum(len(rejection.proposal_ids) for rejection in self.rejected)

    def targets(self) -> tuple[str, ...]:
        return tuple(change.target for change in self.accepted)


@dataclass
class _Group:
    domain: str
    key: str
    proposals: list[StateChangeProposal] = field(default_factory=list)


class StateArbitrator:
    """Validates and merges proposals into committable changes."""

    def __init__(self, policy: ArbitrationPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> ArbitrationPolicy:
        return self._policy

    def arbitrate(
        self, proposals: Sequence[StateChangeProposal], snapshot: StateSnapshot
    ) -> ArbitrationResult:
        rejections: list[Rejection] = []
        admissible: list[StateChangeProposal] = []

        for proposal in proposals:
            rejection = self._screen(proposal)
            if rejection is None:
                admissible.append(proposal)
            else:
                rejections.append(rejection)

        accepted: list[AcceptedChange] = []
        for group in self._group(admissible):
            outcome = self._resolve_group(group, snapshot)
            if isinstance(outcome, Rejection):
                rejections.append(outcome)
            else:
                accepted.append(outcome)

        accepted.sort(key=lambda change: dependency_graph.commit_order_key(change.domain))
        return ArbitrationResult(
            accepted=tuple(accepted), rejected=tuple(rejections), received=len(proposals)
        )

    # --- per-proposal screening -------------------------------------------
    def _screen(self, proposal: StateChangeProposal) -> Rejection | None:
        domain = proposal.target_domain

        def reject(reason: str, detail: str) -> Rejection:
            return Rejection(
                reason_code=reason,
                detail=detail,
                proposal_ids=(proposal.proposal_id,),
                domain=domain,
                key=proposal.target_key,
                source_modules=(proposal.source_module,),
            )

        if not ownership.is_known_domain(domain):
            return reject(RejectionReason.UNKNOWN_DOMAIN, f"no registered writer for {domain!r}")

        if not ownership.may_write(domain, proposal.source_module):
            return reject(
                RejectionReason.NOT_STATE_OWNER,
                f"{proposal.source_module!r} is not the writer of {domain!r} "
                f"(owner: {ownership.owner_of(domain)!r})",
            )

        try:
            is_deep = dependency_graph.is_deep(domain)
        except dependency_graph.LayerError as exc:
            return reject(RejectionReason.UNDECLARED_LAYER, str(exc))

        policy = self._policy.for_domain(domain)

        if (is_deep or policy.requires_consolidation) and not dependency_graph.is_consolidation_writer(
            proposal.source_module
        ):
            return reject(
                RejectionReason.DEEP_UPDATE_REQUIRES_CONSOLIDATION,
                f"{domain!r} is deep state; it changes only through consolidation, "
                "never directly from a single event (spec 12.3)",
            )

        if len(proposal.evidence_ids) < policy.min_evidence_count:
            return reject(
                RejectionReason.INSUFFICIENT_EVIDENCE,
                f"{domain!r} requires at least {policy.min_evidence_count} evidence records, "
                f"got {len(proposal.evidence_ids)}",
            )

        if proposal.operation == "adjust":
            magnitude = proposal.magnitude
            if magnitude is None or not math.isfinite(magnitude):
                return reject(
                    RejectionReason.INVALID_MAGNITUDE, f"magnitude is not finite: {magnitude!r}"
                )

        if proposal.operation == "set" and isinstance(proposal.value, float):
            if not math.isfinite(proposal.value):
                return reject(
                    RejectionReason.INVALID_MAGNITUDE, f"value is not finite: {proposal.value!r}"
                )

        return None

    # --- grouping / merging ------------------------------------------------
    @staticmethod
    def _group(proposals: Iterable[StateChangeProposal]) -> list[_Group]:
        groups: dict[tuple[str, str], _Group] = {}
        for proposal in proposals:
            key = (proposal.target_domain, proposal.target_key)
            group = groups.get(key)
            if group is None:
                group = _Group(domain=proposal.target_domain, key=proposal.target_key)
                groups[key] = group
            group.proposals.append(proposal)
        return [groups[key] for key in sorted(groups)]

    def _resolve_group(self, group: _Group, snapshot: StateSnapshot) -> AcceptedChange | Rejection:
        proposals = group.proposals
        policy = self._policy.for_domain(group.domain)
        ids_ = tuple(proposal.proposal_id for proposal in proposals)
        modules = tuple(dict.fromkeys(proposal.source_module for proposal in proposals))
        operations = {proposal.operation for proposal in proposals}

        def reject(reason: str, detail: str) -> Rejection:
            return Rejection(
                reason_code=reason,
                detail=detail,
                proposal_ids=ids_,
                domain=group.domain,
                key=group.key,
                source_modules=modules,
            )

        if len(operations) > 1:
            return reject(
                RejectionReason.CONFLICTING_OPERATIONS,
                f"{sorted(operations)} proposed for the same key in one run",
            )

        current = snapshot.get(group.domain, group.key)
        operation = operations.pop()

        if operation == "set":
            values = {_hashable(proposal.value) for proposal in proposals}
            if len(values) > 1:
                return reject(
                    RejectionReason.CONFLICTING_SET_VALUES,
                    f"{len(values)} different values proposed for {group.domain}.{group.key}",
                )
            new_value = proposals[0].value
            bounds_error = _check_bounds(new_value, policy)
            if bounds_error is not None:
                return reject(RejectionReason.VALUE_OUT_OF_BOUNDS, bounds_error)
            delta = None
            if current is not None and current.numeric is not None and isinstance(
                new_value, (int, float)
            ) and not isinstance(new_value, bool):
                delta = float(new_value) - current.numeric
                limit_error = _check_delta(delta, policy, group)
                if limit_error is not None:
                    return reject(RejectionReason.DELTA_EXCEEDS_POLICY, limit_error)

        elif operation == "invalidate":
            if current is None:
                return reject(
                    RejectionReason.UNINITIALIZED_TARGET,
                    f"{group.domain}.{group.key} has never been written",
                )
            new_value = None
            delta = None

        else:  # adjust
            if current is None:
                return reject(
                    RejectionReason.UNINITIALIZED_TARGET,
                    f"{group.domain}.{group.key} has no baseline; an adjust cannot resolve "
                    "an unknown value (spec 24: unknown is not zero)",
                )
            base = current.numeric
            if base is None:
                return reject(
                    RejectionReason.NON_NUMERIC_TARGET,
                    f"{group.domain}.{group.key} holds {current.value_type}, not a number",
                )
            delta = math.fsum(float(proposal.magnitude or 0.0) for proposal in proposals)
            limit_error = _check_delta(delta, policy, group)
            if limit_error is not None:
                return reject(RejectionReason.DELTA_EXCEEDS_POLICY, limit_error)
            new_value = base + delta
            bounds_error = _check_bounds(new_value, policy)
            if bounds_error is not None:
                return reject(RejectionReason.VALUE_OUT_OF_BOUNDS, bounds_error)

        confidences = [p.confidence for p in proposals if p.confidence is not None]
        primary = proposals[0]
        return AcceptedChange(
            domain=group.domain,
            key=group.key,
            operation=operation,
            previous_value=None if current is None else current.value,
            new_value=new_value,
            delta=delta,
            confidence=min(confidences) if confidences else None,
            expected_version=None if current is None else current.version,
            source_module=primary.source_module,
            source_event_id=primary.source_event_id,
            proposal_id=primary.proposal_id,
            merged_proposal_ids=ids_,
            reason_codes=tuple(
                dict.fromkeys(code for proposal in proposals for code in proposal.reason_codes)
            ),
            evidence_ids=tuple(
                dict.fromkeys(eid for proposal in proposals for eid in proposal.evidence_ids)
            ),
        )


def _hashable(value: JSONValue) -> object:
    if isinstance(value, (dict, list)):
        return repr(value)
    return value


def _check_bounds(value: JSONValue, policy: DomainPolicy) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if policy.value_min is not None and number < policy.value_min - EPSILON:
        return f"{number} is below the policy minimum {policy.value_min}"
    if policy.value_max is not None and number > policy.value_max + EPSILON:
        return f"{number} is above the policy maximum {policy.value_max}"
    return None


def _check_delta(delta: float, policy: DomainPolicy, group: _Group) -> str | None:
    limit = policy.max_delta_per_event
    if limit is None:
        return None
    if abs(delta) > limit + EPSILON:
        return (
            f"requested change {delta:+.6g} for {group.domain}.{group.key} exceeds the "
            f"per-event limit {limit} (policy rejects rather than clamps)"
        )
    return None
