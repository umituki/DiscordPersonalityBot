"""Deterministic final preflight and acceptance evidence.

This module deliberately never contacts Ollama or Discord.  Real-model,
Genesis and human review gates are reported as deferred until the OWNER starts
them explicitly.
"""

from __future__ import annotations

import ast
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from app.storage.migrations import LATEST_VERSION, schema_version
from app.versioning.capabilities import REQUIRED_CAPABILITIES, load_contracts, summary

DEFERRED_HEAVY_GATES: tuple[str, ...] = (
    "real Ollama appraisal 100-turn",
    "memory 100-500-turn saturation",
    "naturalness question/repetition runs",
    "real proactive Discord send",
    "long real Shadow review",
    "blind human naturalness evaluation",
    "GEN-GATE one-year Genesis (about 1,900-2,100 model calls; typically about 8 hours)",
)

LIGHT_COVERAGE: tuple[str, ...] = (
    "deterministic pytest suite",
    "capability contract audit",
    "migration and SQLite integrity",
    "fixture vertical slices",
    "restart recovery",
    "shadow wiring",
    "admin side-effect safety",
)


@dataclass(frozen=True, slots=True)
class AuditCheck:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def audit_capabilities(root: Path, *, runtime: Any | None = None) -> dict[str, Any]:
    """Cross-check contract claims against tests and production registration."""
    contracts = load_contracts(root / "config" / "capabilities")
    source_root = root if (root / "tests").is_dir() else Path(__file__).resolve().parents[2]
    checks: list[AuditCheck] = []
    findings: dict[str, dict[str, Any]] = {}

    checks.append(
        AuditCheck(
            "required_contracts",
            set(contracts) == set(REQUIRED_CAPABILITIES),
            f"loaded {len(contracts)}/{len(REQUIRED_CAPABILITIES)} required contracts",
        )
    )
    all_verified = all(contract.complete for contract in contracts.values())
    checks.append(
        AuditCheck(
            "all_e2e_verified",
            all_verified,
            f"{summary(contracts)['E2E_VERIFIED']}/{len(contracts)} E2E_VERIFIED",
        )
    )

    for name, contract in sorted(contracts.items()):
        relative, separator, node = contract.acceptance_test.partition("::")
        test_path = source_root / relative
        exists = test_path.is_file()
        source = test_path.read_text(encoding="utf-8") if exists else ""
        node_exists = not separator or _test_node_exists(source, node)
        invariant_proof = relative.replace("\\", "/").startswith("tests/invariants/")

        event_tokens = _event_tokens(contract.success_event)
        event_observed = not event_tokens or all(token in source for token in event_tokens)
        persistence_terms = _claim_terms(contract.persistence)
        persistence_asserted = "assert" in source and any(
            term in source.lower() for term in persistence_terms
        )

        passed = all(
            (
                contract.complete,
                exists,
                node_exists,
                invariant_proof,
                event_observed,
                persistence_asserted,
            )
        )
        findings[name] = {
            "passed": passed,
            "status": contract.status,
            "acceptance_test": contract.acceptance_test,
            "test_exists": exists,
            "test_node_exists": node_exists,
            "invariant_or_e2e_proof": invariant_proof,
            "success_events_observed": event_observed,
            "success_event_tokens": event_tokens,
            "persistence_asserted": persistence_asserted,
            "runtime_entry": contract.runtime_entry,
            "observability": contract.observability,
        }

    checks.append(
        AuditCheck(
            "contract_evidence",
            all(item["passed"] for item in findings.values()),
            "acceptance tests exist and contain event/persistence evidence",
        )
    )

    wiring: dict[str, Sequence[str]] = {"sources": (), "kinds": (), "actions": ()}
    if runtime is not None:
        wiring = runtime.wiring_audit()
    wiring_ok = bool(wiring["sources"] and wiring["kinds"] and wiring["actions"])
    checks.append(
        AuditCheck(
            "production_runtime_wiring",
            wiring_ok,
            (
                f"sources={len(wiring['sources'])}, kinds={len(wiring['kinds'])}, "
                f"actions={len(wiring['actions'])}"
                if runtime is not None
                else "runtime was not supplied"
            ),
        )
    )
    passed = all(check.passed for check in checks)
    return {
        "passed": passed,
        "summary": summary(contracts),
        "checks": [check.as_dict() for check in checks],
        "capabilities": findings,
        "runtime_wiring": {key: list(value) for key, value in wiring.items()},
    }


def collect_preflight(config: Any, application: Any) -> dict[str, Any]:
    """Collect read-only machine and application evidence without network use."""
    root = config.root_dir
    current_schema = schema_version(application.db)
    live = application.live.check()
    audit = audit_capabilities(root, runtime=application.runtime)
    unclaimed: set[str] = set()
    for row in application.runtime_ticks.recent(limit=50):
        unclaimed.update(
            value.strip() for value in (row["unclaimed_kinds"] or "").split(",") if value.strip()
        )

    latest_backup = application.backups.latest_usable()
    git = _git_metadata(root)
    integrity = application.db.integrity_check()
    blockers = [
        {"name": check.name, "detail": check.detail}
        for check in live.blockers
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "result": (
            "PASS"
            if current_schema == LATEST_VERSION
            and integrity == "ok"
            and audit["passed"]
            and not unclaimed
            else "FAIL"
        ),
        "repository": git,
        "machine": {
            "python": sys.version.split()[0],
            "os": platform.platform(),
            "gpu": _gpu_metadata(),
        },
        "schema": {
            "current": current_schema,
            "latest": LATEST_VERSION,
            "pending_migrations": [] if current_schema == LATEST_VERSION else [
                f"{current_schema + 1}..{LATEST_VERSION}"
            ],
        },
        "llm": {
            "reachable": "NOT_PROBED_DEFERRED",
            "reason": "deterministic preflight never contacts real Ollama",
            "model": config.llm.model,
            "base_url": config.llm.base_url,
            "num_ctx": config.llm.num_ctx,
            "temperature": config.llm.temperature,
            "thinking": "not configured",
        },
        "database": {
            "path": str(config.database_path),
            "integrity": integrity,
            "usable_backup": None if latest_backup is None else str(latest_backup.path),
        },
        "first_boot": application.first_boot.status(),
        "live_readiness": {
            "ready": live.ready,
            "blockers": blockers,
            "warnings": [
                {"name": check.name, "detail": check.detail} for check in live.warnings
            ],
        },
        "capability_audit": audit,
        "unclaimed_runtime_kinds": sorted(unclaimed),
        "deferred_heavy_gates": list(DEFERRED_HEAVY_GATES),
    }


def run_light(
    root: Path,
    preflight: dict[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Run the deterministic suite only and persist its machine-readable result."""
    started = time.monotonic()
    command = [sys.executable, "-m", "pytest", "-q"]
    completed = runner(
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    passed = completed.returncode == 0 and preflight["result"] == "PASS"
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "result": "PASS" if passed else "FAIL",
        "duration_seconds": round(time.monotonic() - started, 3),
        "command": command,
        "exit_code": completed.returncode,
        "coverage": list(LIGHT_COVERAGE),
        "pytest_output": completed.stdout[-12000:],
        "preflight": preflight,
        "deferred_heavy_gates": list(DEFERRED_HEAVY_GATES),
    }
    destination = root / "artifacts" / "acceptance" / "LIGHT_RUN_LATEST.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    payload["artifact"] = str(destination)
    return payload


def write_report(root: Path, preflight: dict[str, Any]) -> dict[str, Any]:
    """Write matching Markdown and JSON final-precheck artifacts."""
    latest = root / "artifacts" / "acceptance" / "LIGHT_RUN_LATEST.json"
    light = json.loads(latest.read_text(encoding="utf-8")) if latest.is_file() else None
    overall = "PASS" if preflight["result"] == "PASS" and light and light["result"] == "PASS" else "FAIL"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "result": overall,
        "preflight": preflight,
        "light_run": light,
        "blocker_category": "deferred_owner_or_real_machine_gates" if overall == "PASS" else "software_or_light_acceptance",
        "deferred_heavy_gates": list(DEFERRED_HEAVY_GATES),
        "gen_gate_estimate": "about 1,900-2,100 model calls; typically about 8 hours",
        "next_command": "OWNER authorization is required before any real-machine or GEN-GATE run",
    }
    json_path = root / "artifacts" / "acceptance" / f"FINAL_PRECHECK_{stamp}.json"
    md_path = root / "docs" / "evaluations" / f"FINAL_PRECHECK_{stamp}.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(_markdown_report(payload), encoding="utf-8")
    return {"result": overall, "json": str(json_path), "markdown": str(md_path)}


def _event_tokens(claim: str) -> list[str]:
    if claim.strip().lower().startswith(("none", "no event")):
        return []
    return list(dict.fromkeys(re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", claim)))


def _claim_terms(claim: str) -> list[str]:
    stop = {"status", "with", "after", "only", "file", "files", "none", "state", "history"}
    terms = re.findall(r"\b[a-z][a-z0-9_]{3,}\b", claim.lower())
    return [term for term in dict.fromkeys(terms) if term not in stop]


def _test_node_exists(source: str, node: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == node for item in ast.walk(tree))


def _git_metadata(root: Path) -> dict[str, Any]:
    def read(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=False
        )
        return completed.stdout.strip() if completed.returncode == 0 else "unknown"

    return {
        "commit": read("rev-parse", "HEAD"),
        "branch": read("branch", "--show-current"),
        "dirty": bool(read("status", "--porcelain")),
    }


def _gpu_metadata() -> dict[str, str]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {"status": "not_detected"}
    completed = subprocess.run(
        [executable, "--query-gpu=name,memory.total", "--format=csv,noheader"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if completed.returncode != 0:
        return {"status": "detected_but_unreadable"}
    return {"status": "detected", "detail": completed.stdout.strip()}


def _markdown_report(payload: dict[str, Any]) -> str:
    preflight = payload["preflight"]
    light = payload["light_run"] or {"result": "NOT_RUN", "pytest_output": ""}
    blockers = preflight["live_readiness"]["blockers"]
    lines = [
        "# YUI final deterministic precheck",
        "",
        f"- Result: **{payload['result']}**",
        f"- Preflight: **{preflight['result']}**",
        f"- Light deterministic run: **{light['result']}**",
        f"- Commit: `{preflight['repository']['commit']}`",
        f"- Branch: `{preflight['repository']['branch']}`",
        f"- Schema: `{preflight['schema']['current']}/{preflight['schema']['latest']}`",
        f"- Capabilities: `{preflight['capability_audit']['summary']['E2E_VERIFIED']}/{len(REQUIRED_CAPABILITIES)} E2E_VERIFIED`",
        "",
        "## LiveReadiness blockers",
        "",
    ]
    lines.extend(f"- `{item['name']}`: {item['detail']}" for item in blockers)
    if not blockers:
        lines.append("- None")
    lines.extend(["", "## Deferred heavy and human gates", ""])
    lines.extend(f"- {item}" for item in payload["deferred_heavy_gates"])
    lines.extend(
        [
            "",
            "## GEN-GATE estimate",
            "",
            f"- {payload['gen_gate_estimate']}",
            "- This report does not authorize or start it.",
            "",
            "## Next step",
            "",
            f"{payload['next_command']}.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "DEFERRED_HEAVY_GATES",
    "LIGHT_COVERAGE",
    "audit_capabilities",
    "collect_preflight",
    "run_light",
    "write_report",
]
