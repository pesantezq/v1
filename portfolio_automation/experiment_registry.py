"""Phase 8 — experiment registry + research-integrity controls (observe-only).

A durable, auditable record of every research experiment so a result can be
traced hypothesis -> source_commit -> data sources -> discovery/calibration/
validation/OOS windows -> result -> promotion state. Two guarantees:

1. **Failures are retained** (rejected/inconclusive/degraded stay in the
   registry — never deleted), so negative results stay visible.
2. **In-sample discovery cannot masquerade as out-of-sample validation** — the
   research controls flag overlapping discovery/validation windows, datasets
   that cannot provide point-in-time data, multiple-testing risk, and
   insufficient OOS samples.

Observe-only: the registry records research; it never mutates production, scores,
or weights, and AI/research may not self-promote (promotion stays human-gated in
Phase 10). Pure except injected timestamps.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

EXPERIMENT_STATUSES = (
    "proposed", "running", "inconclusive", "rejected", "validated",
    "promoted", "degraded", "retired", "superseded",
)

_REGISTRY_FILENAME = "experiment_registry.json"
_PIT_OK = {"point_in_time", "pit", "as_of"}
_MULTIPLE_TESTING_THRESHOLD = 10
_MIN_OOS_SAMPLE = 30

__all__ = [
    "EXPERIMENT_STATUSES", "new_experiment", "validate_research_controls",
    "windows_overlap", "register_experiment", "update_experiment", "read_registry",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def new_experiment(
    *, experiment_id: str, hypothesis: str, rationale: str, owner: str,
    source_commit: str, data_sources: list[str], pit_policy: str,
    discovery_window: list[str], calibration_window: list[str],
    validation_window: list[str], oos_window: list[str], benchmark: str,
    cost_assumptions: dict[str, Any], variants_tested: int, now: str,
    selected_variant: str | None = None, failure_conditions: list[str] | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Build a fully-populated experiment record (status=proposed). No I/O."""
    return {
        "experiment_id": experiment_id,
        "hypothesis": hypothesis,
        "rationale": rationale,
        "owner": owner,
        "source_commit": source_commit,
        "data_sources": list(data_sources),
        "pit_policy": pit_policy,
        "discovery_window": list(discovery_window),
        "calibration_window": list(calibration_window),
        "validation_window": list(validation_window),
        "oos_window": list(oos_window),
        "benchmark": benchmark,
        "cost_assumptions": dict(cost_assumptions),
        "variants_tested": int(variants_tested),
        "selected_variant": selected_variant,
        "status": "proposed",
        "result": None,
        "failure_conditions": list(failure_conditions or []),
        "promotion_state": "not_promoted",
        "supersedes": supersedes,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Research-integrity controls
# ---------------------------------------------------------------------------


def windows_overlap(a: list[str], b: list[str]) -> bool:
    """True if two ``[start, end]`` string windows overlap (lexicographic on
    ISO-ish ``YYYY-MM`` bounds — sufficient for monthly/period windows)."""
    if not (a and b and len(a) == 2 and len(b) == 2):
        return False
    a0, a1 = sorted(a)
    b0, b1 = sorted(b)
    return a0 <= b1 and b0 <= a1


def validate_research_controls(
    exp: dict[str, Any], *, oos_sample_size: int | None = None,
) -> dict[str, Any]:
    """Return integrity findings for an experiment. Does not mutate it."""
    warnings: list[str] = []

    disjoint = not windows_overlap(exp.get("discovery_window", []),
                                   exp.get("validation_window", []))
    if not disjoint:
        warnings.append("discovery_validation_overlap")

    pit_supported = str(exp.get("pit_policy", "")).lower() in _PIT_OK
    if not pit_supported:
        warnings.append("pit_unsupported_dataset")

    multiple_testing = int(exp.get("variants_tested", 0)) >= _MULTIPLE_TESTING_THRESHOLD
    if multiple_testing:
        warnings.append("multiple_testing_risk")

    sample_sufficient = True
    if oos_sample_size is not None:
        sample_sufficient = oos_sample_size >= _MIN_OOS_SAMPLE
        if not sample_sufficient:
            warnings.append("insufficient_oos_sample")

    # an experiment whose dataset can't support PIT, or whose discovery leaks
    # into validation, is not safely validatable -> recommend degraded.
    recommended_status = "degraded" if (not pit_supported or not disjoint) else None

    return {
        "discovery_validation_disjoint": disjoint,
        "pit_supported": pit_supported,
        "multiple_testing_risk": multiple_testing,
        "sample_sufficient": sample_sufficient,
        "recommended_status": recommended_status,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Persistence (durable; retains failures; idempotent)
# ---------------------------------------------------------------------------


def read_registry(root: Path | str) -> list[dict[str, Any]]:
    path = Path(root) / "outputs" / "sandbox" / _REGISTRY_FILENAME
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("experiments", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _write_registry(root: Path | str, experiments: list[dict[str, Any]]) -> Path:
    payload = {
        "observe_only": True,
        "schema_version": "1",
        "source": "experiment_registry",
        "experiment_count": len(experiments),
        "experiments": experiments,
        "disclaimer": (
            "Observe-only research registry. Retains failed/rejected experiments; "
            "never mutates production/scores/weights; promotion is human-gated."
        ),
    }
    return safe_write_json(OutputNamespace.SANDBOX, _REGISTRY_FILENAME, payload,
                           base_dir=str(Path(root) / "outputs"))


def register_experiment(root: Path | str, exp: dict[str, Any]) -> Path:
    """Add an experiment. Idempotent: an existing id is left unchanged
    (use :func:`update_experiment` to advance it)."""
    experiments = read_registry(root)
    if any(e.get("experiment_id") == exp.get("experiment_id") for e in experiments):
        return _write_registry(root, experiments)
    experiments.append(exp)
    return _write_registry(root, experiments)


def update_experiment(
    root: Path | str, experiment_id: str, *, status: str | None = None,
    result: dict[str, Any] | None = None, promotion_state: str | None = None,
    now: str,
) -> Path:
    """Advance an experiment's status/result IN PLACE, retaining it. A failed or
    rejected experiment is updated, never removed (negative results persist)."""
    experiments = read_registry(root)
    for e in experiments:
        if e.get("experiment_id") == experiment_id:
            if status is not None:
                if status not in EXPERIMENT_STATUSES:
                    raise ValueError(f"unknown experiment status: {status}")
                e["status"] = status
            if result is not None:
                e["result"] = result
            if promotion_state is not None:
                e["promotion_state"] = promotion_state
            e["updated_at"] = now
            break
    return _write_registry(root, experiments)
