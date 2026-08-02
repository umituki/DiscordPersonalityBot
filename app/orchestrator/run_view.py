"""What an engine may read during one run (spec 9.1, 9.2).

A run hands every engine the same view:

* ``snapshot`` — S0, the committed state as it was when the run started. Layer 0
  to Layer 4 state is read from here and nowhere else, so a value written later
  in the same run cannot feed back and re-amplify itself (spec 9.2).
* ``interpretation`` — Layer 1. Appraisal and other per-run derivations. These
  are *not* committed state: they exist for the duration of the run, are
  recorded as provenance on the proposals they justify, and are recomputed for
  the next event.

Engines return proposals. They never write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.clock import Clock, SystemClock
from app.config import RuntimeMode
from app.state.snapshot import StateSnapshot


@dataclass(frozen=True, slots=True)
class Interpretation:
    """Layer 1 derivations for one root event."""

    #: Appraisal of the event, when one was produced (spec 11.1).
    appraisal: Any = None
    #: Free-form additional derivations keyed by producer.
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def has_appraisal(self) -> bool:
        return self.appraisal is not None

    def with_appraisal(self, appraisal: Any) -> Interpretation:
        return Interpretation(appraisal=appraisal, extras=dict(self.extras))

    def with_extra(self, key: str, value: Any) -> Interpretation:
        return Interpretation(appraisal=self.appraisal, extras={**self.extras, key: value})


@dataclass(frozen=True, slots=True)
class RunView:
    """Read-only view of everything an engine may consult during a run."""

    snapshot: StateSnapshot
    interpretation: Interpretation = field(default_factory=Interpretation)
    run_id: str | None = None
    mode: RuntimeMode = "normal"
    #: The psychological "now" for this run (patch spec 13).
    #:
    #: ``recorded_at`` is when the machine did the work and ``occurred_at`` is
    #: when the experience happened; this is the one an engine must use for
    #: anything time-shaped — decay, drift, persistence, forgetting, evidence
    #: windows. In normal runtime it is the wall clock. In a simulation it is
    #: the simulated moment, because a decade of simulated life that decays
    #: against wall-clock seconds has no time in it at all.
    #:
    #: An engine that reads its own ``SystemClock`` instead breaks this, which
    #: is why it travels on the view rather than being left to each engine.
    effective_now: datetime | None = None

    # --- convenience state reads ------------------------------------------
    def number(self, domain: str, key: str, default: float | None = None) -> float | None:
        return self.snapshot.number_of(domain, key, default)

    def value(self, domain: str, key: str, default: Any = None) -> Any:
        return self.snapshot.value_of(domain, key, default)

    def known(self, domain: str, key: str) -> bool:
        return self.snapshot.get(domain, key) is not None

    @property
    def appraisal(self) -> Any:
        return self.interpretation.appraisal

    def now(self, clock: Clock | None = None) -> datetime:
        """The moment this run should reason about (patch spec 13).

        Falls back to the caller's clock only when no effective time was set,
        so an engine written against this never silently reads wall time during
        a simulation.
        """
        if self.effective_now is not None:
            return self.effective_now
        return (clock or SystemClock()).now()

    def with_interpretation(self, interpretation: Interpretation) -> RunView:
        return RunView(
            snapshot=self.snapshot,
            interpretation=interpretation,
            run_id=self.run_id,
            mode=self.mode,
            effective_now=self.effective_now,
        )
