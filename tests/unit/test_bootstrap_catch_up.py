"""Startup catch-up regression tests."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.bootstrap import Application


def test_catch_up_starts_from_newest_world_value_and_keeps_zero_pressure(clock) -> None:
    genesis_time = clock.now() - timedelta(days=19 * 365)
    latest_world_time = clock.now() - timedelta(minutes=5)
    pressure = SimpleNamespace(updated_at=genesis_time, numeric=0.0)
    circadian = SimpleNamespace(updated_at=latest_world_time, numeric=0.4)

    class State:
        def get(self, domain, key):
            assert (domain, key) == ("world", "sleep_pressure")
            return pressure

        def list_domain(self, domain):
            assert domain == "world"
            return [pressure, circadian]

    class World:
        def __init__(self) -> None:
            self.calls = []

        def catch_up(self, **kwargs):
            self.calls.append(kwargs)
            return "caught-up"

    app = object.__new__(Application)
    app.state = State()
    app.world = World()

    result = app._catch_up_world()

    assert result == "caught-up"
    assert app.world.calls == [
        {"since": latest_world_time, "sleep_pressure": 0.0}
    ]
