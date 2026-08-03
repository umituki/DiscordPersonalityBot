"""INVARIANT: final acceptance is complete, deterministic and never goes live."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.admin.acceptance import (
    DEFERRED_HEAVY_GATES,
    audit_capabilities,
    collect_preflight,
    run_light,
    write_report,
)
from app.bootstrap import Application

pytestmark = pytest.mark.invariant
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_capability_audit_matches_tests_and_production_wiring(temp_config, clock) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        report = audit_capabilities(temp_config.root_dir, runtime=application.runtime)
    finally:
        application.db.close()

    assert report["passed"], report
    assert report["summary"] == {
        "NOT_STARTED": 0,
        "CODE_ONLY": 0,
        "WIRED": 0,
        "E2E_VERIFIED": 20,
    }
    assert report["runtime_wiring"]["sources"]
    assert "spontaneous_recall" in report["runtime_wiring"]["kinds"]
    assert "recall_from_cue" in report["runtime_wiring"]["actions"]


def test_preflight_reports_machine_state_without_probing_ollama(
    temp_config, clock, monkeypatch
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    monkeypatch.setattr("app.admin.acceptance._gpu_metadata", lambda: {"status": "test"})
    try:
        report = collect_preflight(temp_config, application)
    finally:
        application.db.close()

    assert report["result"] == "PASS"
    assert report["llm"]["reachable"] == "NOT_PROBED_DEFERRED"
    assert report["schema"]["pending_migrations"] == []
    assert report["database"]["integrity"] == "ok"
    assert report["capability_audit"]["passed"]
    assert report["unclaimed_runtime_kinds"] == []
    assert report["deferred_heavy_gates"] == list(DEFERRED_HEAVY_GATES)
    # Live readiness remains honest; deterministic software completion is not
    # permission to start the character plane.
    assert not report["live_readiness"]["ready"]


def test_run_light_records_the_deterministic_command_and_never_a_heavy_gate(tmp_path) -> None:
    seen = []

    def runner(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "1513 passed in 1.00s\n")

    preflight = {"result": "PASS"}
    result = run_light(tmp_path, preflight, runner=runner)

    assert result["result"] == "PASS"
    assert seen[0][0][1:] == ["-m", "pytest", "-q"]
    assert "ollama" not in " ".join(seen[0][0]).lower()
    assert "genesis" not in " ".join(seen[0][0]).lower()
    saved = json.loads(
        (tmp_path / "artifacts" / "acceptance" / "LIGHT_RUN_LATEST.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["result"] == "PASS"


def test_report_requires_a_passing_light_run_and_writes_both_formats(tmp_path) -> None:
    artifact = tmp_path / "artifacts" / "acceptance"
    artifact.mkdir(parents=True)
    (artifact / "LIGHT_RUN_LATEST.json").write_text(
        json.dumps({"result": "PASS", "pytest_output": "all passed"}),
        encoding="utf-8",
    )
    preflight = {
        "result": "PASS",
        "repository": {"commit": "abc", "branch": "agent/test"},
        "schema": {"current": 32, "latest": 32},
        "capability_audit": {"summary": {"E2E_VERIFIED": 20}},
        "live_readiness": {"blockers": [{"name": "live_enabled", "detail": "false"}]},
    }

    result = write_report(tmp_path, preflight)

    assert result["result"] == "PASS"
    assert Path(result["json"]).is_file()
    markdown = Path(result["markdown"]).read_text(encoding="utf-8")
    assert "LiveReadiness blockers" in markdown
    assert "19-year Full GEN-GATE estimate" in markdown
    assert "19-year Full Genesis: about 1,900-2,100 model calls" in markdown
    assert "does not authorize or start it" in markdown
