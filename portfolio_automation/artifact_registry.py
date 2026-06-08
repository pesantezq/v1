"""artifact_registry — observe-only governance of outputs/* artifacts.

Single machine-readable contract (artifact_registry.yaml) describing every
tracked artifact: lens, role, required, cadence, producer, consumers, severity.
The validator classifies the live corpus (present / stale / invalid / unattributed)
and writes outputs/latest/artifact_registry_status.json. daily_run_status consumes
required_artifacts() instead of a hardcoded list — single source of truth.

Observe-only: reads the registry + artifact mtimes; writes only its status
artifact. Never mutates decision/score/allocation/portfolio state. See
docs/superpowers/specs/2026-06-08-artifact-registry-governance-design.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

GREEN, AMBER, RED = "green", "amber", "red"

DEFAULT_REGISTRY_PATH = Path(__file__).with_name("artifact_registry.yaml")
_STATUS_REL = "artifact_registry_status.json"  # under outputs/latest/

# allowed enum values (a row with anything else is flagged schema_invalid)
LENSES = {"developer", "quant_learning", "market_discovery", "risk_action",
          "decision_core", "meta_governance"}
ROLES = {"source_of_truth", "advisor", "probe", "telemetry", "narrative"}
CADENCES = {"daily", "weekend", "weekly", "monthly", "yearly", "on_demand"}
SEVERITIES = {"critical", "warning", "info"}
_REQUIRED_ROW_FIELDS = ("lens", "role", "required", "cadence", "producer",
                        "consumers", "severity_if_missing")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict:
    """Parse the YAML registry; return {} on missing/corrupt (fault-tolerant)."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        data.setdefault("artifacts", {})
        data.setdefault("daily_run_status_tracked", [])
        if not isinstance(data["artifacts"], dict):
            return {}
        return data
    except Exception:
        return {}


def schema_errors(registry: dict) -> list[str]:
    """Return a list of human-readable schema problems (empty == valid)."""
    errs: list[str] = []
    arts = registry.get("artifacts", {})
    for key, row in arts.items():
        if not isinstance(row, dict):
            errs.append(f"{key}: row is not a mapping")
            continue
        for f in _REQUIRED_ROW_FIELDS:
            if f not in row:
                errs.append(f"{key}: missing field {f}")
        if row.get("lens") not in LENSES:
            errs.append(f"{key}: bad lens {row.get('lens')!r}")
        if row.get("role") not in ROLES:
            errs.append(f"{key}: bad role {row.get('role')!r}")
        if row.get("cadence") not in CADENCES:
            errs.append(f"{key}: bad cadence {row.get('cadence')!r}")
        if row.get("severity_if_missing") not in SEVERITIES:
            errs.append(f"{key}: bad severity {row.get('severity_if_missing')!r}")
        if not isinstance(row.get("consumers"), list) or not row.get("consumers"):
            errs.append(f"{key}: consumers must be a non-empty list")
    for key in registry.get("daily_run_status_tracked", []):
        if key not in arts:
            errs.append(f"tracked key not in artifacts: {key}")
    return errs
