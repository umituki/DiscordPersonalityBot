"""State layer.

Spec architecture rule: ``app/state/`` owns proposals, dependency validation,
arbitration and atomic commit. Domain engines write only through it.
"""

from app.state.arbitrator import (
    AcceptedChange,
    ArbitrationResult,
    Rejection,
    RejectionReason,
    StateArbitrator,
)
from app.state.committer import CommitResult, StateCommitter
from app.state.ownership import OwnershipError, may_write, owner_of
from app.state.policy import ArbitrationPolicy
from app.state.proposal import StateChangeProposal
from app.state.snapshot import SnapshotService, StateSnapshot
from app.state.value import StateValue, UnknownValueError

__all__ = [
    "AcceptedChange",
    "ArbitrationPolicy",
    "ArbitrationResult",
    "CommitResult",
    "OwnershipError",
    "Rejection",
    "RejectionReason",
    "SnapshotService",
    "StateArbitrator",
    "StateChangeProposal",
    "StateCommitter",
    "StateSnapshot",
    "StateValue",
    "UnknownValueError",
    "may_write",
    "owner_of",
]
