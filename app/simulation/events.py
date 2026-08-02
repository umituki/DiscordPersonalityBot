"""Past simulation events (spec 8.1, 22.6, 22.7).

Every event here carries ``origin="simulated_past"``, which is the permanent
mark that separates a life YUI was given from a life she had with the USER
(spec 2.10, 2.16, 34.2-4). ``occurred_at`` is the simulated moment while
``recorded_at`` is now — the event model keeps them apart precisely so a
simulated 2003 can be recorded in 2026 without either becoming a lie.
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

SIMULATED_EXPERIENCE = "SIMULATED_EXPERIENCE"
LIFE_PHASE_STARTED = "LIFE_PHASE_STARTED"
SIMULATION_BLOCK_COMPLETED = "SIMULATION_BLOCK_COMPLETED"
FIRST_BOOT = "FIRST_BOOT"


@register_payload(SIMULATED_EXPERIENCE)
class SimulatedExperiencePayload(EventPayload):
    """One thing that happened in the simulated past."""

    block_id: str
    experience_class: str
    valence: float = 0.0
    summary: str = ""
    text: str = ""
    topics: tuple[str, ...] = ()
    #: The model's own sense of how much this landed. A signal that the
    #: encoding gate may read, never the final importance (spec 2.6).
    felt_significance: float = 0.0
    involves_other_person: bool = False
    #: Set when a ceiling demoted this experience (spec 22.4).
    demoted_from: str | None = None


@register_payload(LIFE_PHASE_STARTED)
class LifePhaseStartedPayload(EventPayload):
    phase_id: str
    name: str
    life_stage: str = ""


@register_payload(SIMULATION_BLOCK_COMPLETED)
class BlockCompletedPayload(EventPayload):
    block_id: str
    detail_level: str
    experience_class: str
    days: float = 0.0
    event_count: int = 0


@register_payload(FIRST_BOOT)
class FirstBootPayload(EventPayload):
    """The line between a given past and a lived present (spec 22.7)."""

    simulation_id: str
    simulated_years: float = 0.0
    experiences: int = 0
    blocks: int = 0
    audits_passed: tuple[str, ...] = ()
