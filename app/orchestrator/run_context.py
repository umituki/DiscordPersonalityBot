"""Run context (spec 7.2).

One root event produces one run. Everything the run writes — events, state
changes, failures — is traceable to this record.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app import ids
from app.clock import Clock, SystemClock, ensure_aware
from app.config import RuntimeMode
from app.events.model import Priority


class RunContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    root_event_id: str
    state_snapshot_id: str
    runtime_manifest_id: str | None
    started_at: datetime
    priority: Priority
    mode: RuntimeMode

    @field_validator("started_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @classmethod
    def create(
        cls,
        *,
        root_event_id: str,
        state_snapshot_id: str,
        runtime_manifest_id: str | None,
        priority: Priority = "P3",
        mode: RuntimeMode = "normal",
        clock: Clock | None = None,
        run_id: str | None = None,
    ) -> RunContext:
        return cls(
            run_id=run_id or ids.new_id(ids.RUN),
            root_event_id=root_event_id,
            state_snapshot_id=state_snapshot_id,
            runtime_manifest_id=runtime_manifest_id,
            started_at=(clock or SystemClock()).now(),
            priority=priority,
            mode=mode,
        )

    @property
    def is_simulation(self) -> bool:
        return self.mode == "simulation"
