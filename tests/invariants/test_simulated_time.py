"""INVARIANT: engines reason at the run's time, not the machine's.

Patch spec 13. An engine that reaches for its own ``SystemClock`` during a
simulation measures a decade of simulated life in wall-clock milliseconds:
emotion never decays, needs never drift, nothing ever persists long enough to
count as evidence, and the resulting person never changes.

The structural test below is the one that keeps this true as engines are added.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.orchestrator.run_view import RunView
from app.state.snapshot import StateSnapshot

pytestmark = pytest.mark.invariant

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Engines that participate in a simulated life and therefore must read the
#: run's effective time inside ``handle``.
TIME_SENSITIVE = (
    Path("psychology/emotion.py"),
    Path("psychology/mood.py"),
    Path("psychology/needs.py"),
    Path("social/relationship.py"),
    Path("social/attachment.py"),
    Path("social/user_model.py"),
    Path("world/service.py"),
)

_HANDLE = re.compile(r"    async def handle\(self, event.*?(?=\n    (?:async )?def |\Z)", re.S)


def _handle_body(path: Path) -> str:
    match = _HANDLE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path} has no handle() to check"
    return match.group(0)


@pytest.mark.parametrize("relative", TIME_SENSITIVE, ids=lambda path: str(path))
def test_an_engine_reads_the_runs_time_not_the_wall_clock(relative: Path) -> None:
    body = _handle_body(APP_ROOT / relative)
    assert "view.now(" in body, (
        f"{relative} must take its 'now' from the run view (patch spec 13)"
    )
    assert "self._clock.now()" not in body, (
        f"{relative} reads the wall clock inside handle(); in a simulation that is "
        "the wrong decade"
    )


def test_the_view_prefers_the_effective_time(clock) -> None:
    snapshot = StateSnapshot.empty(snapshot_id="snap_1", created_at=clock.now())
    simulated = clock.now().replace(year=2003)

    assert RunView(snapshot=snapshot, effective_now=simulated).now(clock) == simulated
    assert RunView(snapshot=snapshot).now(clock) == clock.now()


def test_the_effective_time_survives_a_new_interpretation(clock) -> None:
    """Layer 1 is recomputed mid-run; the run's clock is not."""
    from app.orchestrator.run_view import Interpretation

    snapshot = StateSnapshot.empty(snapshot_id="snap_1", created_at=clock.now())
    simulated = clock.now().replace(year=2003)
    view = RunView(snapshot=snapshot, effective_now=simulated)

    assert view.with_interpretation(Interpretation()).now(clock) == simulated
