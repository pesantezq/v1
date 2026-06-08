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
import yaml

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

GREEN, AMBER, RED = "green", "amber", "red"

DEFAULT_REGISTRY_PATH = Path(__file__).with_name("artifact_registry.yaml")
_STATUS_REL = "artifact_registry_status.json"  # under outputs/latest/

# allowed enum values (a row with anything else is flagged schema_invalid)
LENSES = {"developer", "quant_learning", "market_discovery", "risk_action",
          "decision_core", "meta_governance"}
ROLES = {"source_of_truth", "advisor", "probe", "telemetry", "narrative"}
CADENCES = {"daily", "weekend", "weekly", "monthly", "yearly", "on_demand"}
SEVERITIES = {"critical", "warning", "info"}
_REQUIRED_ROW_FIELDS = ("path", "lens", "role", "required", "cadence", "producer",
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


CADENCE_MAX_AGE_HOURS: dict[str, int | None] = {
    "daily": 30, "weekend": 100, "weekly": 192,
    "monthly": 768, "yearly": 9000, "on_demand": None,
}


def max_age_hours(row: dict) -> int | None:
    """Staleness window for a row: explicit override, else cadence default,
    else None (never auto-stale)."""
    ov = row.get("staleness_hours_override")
    if isinstance(ov, (int, float)):
        return int(ov)
    return CADENCE_MAX_AGE_HOURS.get(row.get("cadence"), None)


def is_stale(row: dict, age_hours: float) -> bool:
    """Return True if age_hours exceeds the row's max_age_hours; on_demand never stale."""
    mx = max_age_hours(row)
    return mx is not None and age_hours > mx


def required_artifacts(registry: dict | None = None) -> list[tuple[str, str, bool]]:
    """Return (rel_path, label, required) triples for the daily_run_status-tracked
    subset, in tracked order — the exact shape of the legacy _EXPECTED_ARTIFACTS."""
    reg = registry if registry is not None else load_registry()
    arts = reg.get("artifacts", {})
    out: list[tuple[str, str, bool]] = []
    for key in reg.get("daily_run_status_tracked", []):
        row = arts.get(key)
        if not isinstance(row, dict):
            continue
        path = row.get("path") or f"outputs/latest/{key}"
        out.append((path, row.get("label", key), bool(row.get("required", False))))
    return out


def _row_schema_ok(row: dict) -> bool:
    """Return True iff the row has all required fields with valid enum values."""
    return (isinstance(row, dict)
            and all(f in row for f in _REQUIRED_ROW_FIELDS)
            and row.get("lens") in LENSES and row.get("role") in ROLES
            and row.get("cadence") in CADENCES
            and row.get("severity_if_missing") in SEVERITIES
            and isinstance(row.get("consumers"), list) and bool(row.get("consumers")))


def validate_registry(registry: dict, artifacts_root: str | Path, now: datetime) -> dict:
    """Classify every cataloged artifact and roll up to an observe-only status dict.

    now must be timezone-aware (UTC).
    """
    root = Path(artifacts_root)
    arts = registry.get("artifacts") or {}
    if not isinstance(arts, dict):
        arts = {}
    present = 0
    missing, stale, invalid_json, unattributed, schema_invalid = [], [], [], [], []
    sev_counts: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    by_lens: dict[str, dict] = {}

    for key, row in arts.items():
        if not _row_schema_ok(row):
            schema_invalid.append(key)
            continue
        path = root / (row.get("path") or f"outputs/latest/{key}")
        sev = row.get("severity_if_missing", "info")
        lens = row["lens"]
        lens_bucket = by_lens.setdefault(lens, {"total": 0, "present": 0, "issues": 0})
        lens_bucket["total"] += 1

        if "UNATTRIBUTED" in row.get("consumers", []):
            unattributed.append(key)

        exists = path.exists()
        is_missing = not exists
        is_stale_flag = False
        is_bad_json = False
        if exists:
            age_h = None
            try:
                age_h = (now.timestamp() - path.stat().st_mtime) / 3600.0
            except OSError:
                age_h = None
            if age_h is not None:
                is_stale_flag = is_stale(row, age_h)
                if is_stale_flag:
                    stale.append({"artifact": key, "cadence": row["cadence"],
                                  "age_hours": round(age_h, 1)})
            if str(path).endswith(".json"):
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    is_bad_json = True
                    invalid_json.append(key)

        problem = is_missing or is_stale_flag or is_bad_json
        if is_missing:
            missing.append(key)
        if problem:
            lens_bucket["issues"] += 1
            sev_counts[sev] = sev_counts.get(sev, 0) + 1
        else:
            lens_bucket["present"] += 1
            present += 1

    if sev_counts["critical"] > 0:
        overall = RED
    elif sev_counts["warning"] > 0:
        overall = AMBER
    else:
        overall = GREEN
    msg_bits = []
    if missing:
        msg_bits.append(f"{len(missing)} missing")
    if stale:
        msg_bits.append(f"{len(stale)} stale")
    if invalid_json:
        msg_bits.append(f"{len(invalid_json)} invalid-json")
    if unattributed:
        msg_bits.append(f"{len(unattributed)} unattributed")
    operator_message = "; ".join(msg_bits) or "all artifacts present, fresh, attributed"

    return {
        "generated_at": now.isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "source": "artifact_registry",
        "overall_status": overall,
        "counts": {"total": len(arts), "present": present, "stale": len(stale),
                   "invalid_json": len(invalid_json), "missing": len(missing),
                   "missing_required": sum(1 for k in missing
                                           if arts.get(k, {}).get("required")),
                   "unattributed": len(unattributed), "schema_invalid": len(schema_invalid)},
        "missing": missing, "stale": stale, "invalid_json": invalid_json,
        "unattributed": unattributed, "schema_invalid": schema_invalid,
        "severity": sev_counts, "by_lens": by_lens,
        "operator_message": operator_message,
        "disclaimer": ("Observe-only artifact-governance validator. Reads the registry "
                       "+ artifact mtimes; classifies coverage/freshness. Does not call "
                       "APIs or mutate any decision, allocation, score, or portfolio state."),
    }


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
        if "required" in row and not isinstance(row["required"], bool):
            errs.append(f"{key}: required must be a boolean")
        if not isinstance(row.get("consumers"), list) or not row.get("consumers"):
            errs.append(f"{key}: consumers must be a non-empty list")
    for key in registry.get("daily_run_status_tracked", []):
        if key not in arts:
            errs.append(f"tracked key not in artifacts: {key}")
    return errs


def run_artifact_registry(*, root: str | Path = ".", now=None,
                          write_files: bool = True) -> dict:
    """Load registry → validate corpus → write status artifact. Never raises."""
    root_path = Path(root).resolve()
    ts = now or datetime.now(timezone.utc)
    try:
        registry = load_registry()
        status = validate_registry(registry, root_path, ts)
        if write_files:
            safe_write_json(OutputNamespace.LATEST, _STATUS_REL, status,
                            base_dir=root_path / "outputs")
        return status
    except Exception as exc:
        return {"generated_at": ts.isoformat(), "observe_only": True,
                "schema_version": "1", "source": "artifact_registry",
                "overall_status": AMBER, "counts": {}, "missing": [], "stale": [],
                "invalid_json": [], "unattributed": [], "schema_invalid": [],
                "severity": {"critical": 0, "warning": 0, "info": 0}, "by_lens": {},
                "operator_message": f"degraded: {exc}",
                "disclaimer": "Observe-only artifact-governance validator (degraded)."}
