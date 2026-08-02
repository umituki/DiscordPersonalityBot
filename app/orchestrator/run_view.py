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
from typing import Any

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

    def with_interpretation(self, interpretation: Interpretation) -> RunView:
        return RunView(
            snapshot=self.snapshot,
            interpretation=interpretation,
            run_id=self.run_id,
            mode=self.mode,
        )
