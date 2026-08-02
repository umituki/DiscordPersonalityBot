"""INVARIANT: layer boundaries hold (spec 37, architecture rules).

These are structural guards against the shortcuts spec 37 forbids. They fail
loudly if a later phase starts writing SQL in an engine or turns the objective
archive into a recall path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.events.store import EventStore

pytestmark = pytest.mark.invariant

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
SQL_PATTERN = re.compile(
    r"""["'\s(](SELECT\s|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE|"""
    r"""CREATE\s+INDEX|CREATE\s+TRIGGER|PRAGMA\s)""",
    re.IGNORECASE,
)

#: Only these may contain SQL (architecture rule: storage is the SQL boundary).
SQL_ALLOWED = {
    Path("storage/database.py"),
    Path("storage/migrations.py"),
}


def _python_files() -> list[Path]:
    return sorted(path for path in APP_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def test_sql_lives_only_in_the_storage_layer() -> None:
    offenders: list[str] = []
    for path in _python_files():
        relative = path.relative_to(APP_ROOT)
        if relative.parts[:1] == ("storage",) and (
            relative in SQL_ALLOWED or relative.parts[:2] == ("storage", "repositories")
        ):
            continue
        if SQL_PATTERN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(relative))
    assert offenders == [], (
        f"SQL found outside app/storage: {offenders}. Repositories are the only SQL boundary."
    )


def test_interfaces_do_not_import_storage_directly() -> None:
    """Spec 37: ``Discord handler → DB direct write`` is forbidden."""
    interfaces = APP_ROOT / "interfaces"
    if not interfaces.exists():
        pytest.skip("no interface implemented yet (Phase 3)")
    offenders = [
        str(path.relative_to(APP_ROOT))
        for path in interfaces.rglob("*.py")
        if re.search(r"^from app\.storage|^import app\.storage", path.read_text("utf-8"), re.M)
    ]
    assert offenders == []


def test_objective_archive_is_not_a_recall_api() -> None:
    """Spec 10.1 / 37: the event store must not double as subjective recall."""
    recall_names = {"recall", "remember", "retrieve_memories", "search_memories", "relevant"}
    exposed = {name for name in dir(EventStore) if not name.startswith("_")}
    assert not (recall_names & exposed), (
        "EventStore must not expose subjective recall; memory retrieval belongs to the "
        "Memory Engine over subjective memory (spec 10.1)"
    )


def test_state_writes_go_through_the_committer() -> None:
    """No module outside app/state and app/storage may call write_value."""
    offenders: list[str] = []
    for path in _python_files():
        relative = path.relative_to(APP_ROOT)
        if relative.parts[0] in ("state", "storage"):
            continue
        if "write_value(" in path.read_text(encoding="utf-8"):
            offenders.append(str(relative))
    assert offenders == [], f"direct state writes outside the state layer: {offenders}"


@pytest.mark.parametrize(
    "module",
    ["app.main", "app.bootstrap", "app.storage.repositories", "app.state.snapshot"],
)
def test_entry_points_import_cleanly_in_a_fresh_interpreter(module: str) -> None:
    """Import cycles depend on which module is imported first.

    The test suite always imports through conftest, so it can hide a cycle that
    breaks ``python -m app.main``. Each entry point is therefore imported in its
    own interpreter, in isolation.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        cwd=APP_ROOT.parent,
    )
    assert result.returncode == 0, f"importing {module} failed:\n{result.stderr}"


def test_no_print_in_operational_code() -> None:
    """Spec 38: ``print()`` は運用 logging に使用しない."""
    offenders = [
        str(path.relative_to(APP_ROOT))
        for path in _python_files()
        if re.search(r"^\s*print\(", path.read_text(encoding="utf-8"), re.M)
    ]
    assert offenders == []
