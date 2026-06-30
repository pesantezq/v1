"""Phase 6 — semantic-liveness / degeneracy detectors (observe-only).

Catches pipelines that are technically green but semantically broken: a field
collapsed to one value over a varied window, excessive default usage, zero
variance, or an expected class that disappeared. These are reusable, pure
detectors with **min-sample** + **documented-exception** guards so a
legitimately single-state window (e.g. a genuinely calm regime) does NOT
false-positive.

Observe-only / meta-monitor: surfaces findings (and routes sub-RED concerns to
the quant-watch ledger) but never changes a decision, score, or any production
state, and never escalates the daily check to RED on its own.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import observe_only_envelope

__all__ = [
    "detect_single_value_collapse", "detect_excessive_default",
    "detect_zero_variance", "detect_class_disappearance", "detect_low_cardinality",
    "run_semantic_liveness",
]


def _finding(kind: str, probe: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "probe": probe, "severity": "amber", "detail": detail, **extra}


def detect_single_value_collapse(
    values: list[Any], *, probe: str, min_sample: int = 30,
    allowed_single_values: set | None = None,
) -> dict[str, Any] | None:
    """One unique value over a window of at least ``min_sample`` observations.

    Guarded: windows smaller than ``min_sample`` are not enough evidence; a
    value in ``allowed_single_values`` is a documented legitimate single state.
    """
    vals = [v for v in values if v is not None]
    if len(vals) < min_sample:
        return None
    distinct = set(vals)
    if len(distinct) != 1:
        return None
    only = next(iter(distinct))
    if allowed_single_values and only in allowed_single_values:
        return None
    return _finding("single_value_collapse", probe,
                    f"{probe} collapsed to a single value {only!r} over {len(vals)} samples",
                    observed_distinct=1, only_value=str(only), n_samples=len(vals))


def detect_excessive_default(
    values: list[Any], *, default: Any, probe: str, min_sample: int = 30,
    max_default_frac: float = 0.9,
) -> dict[str, Any] | None:
    vals = [v for v in values if v is not None]
    if len(vals) < min_sample:
        return None
    frac = sum(1 for v in vals if v == default) / len(vals)
    if frac < max_default_frac:
        return None
    return _finding("excessive_default", probe,
                    f"{probe} is the default {default!r} in {frac:.0%} of {len(vals)} samples",
                    default_frac=round(frac, 4), default_value=str(default), n_samples=len(vals))


def detect_zero_variance(
    values: list[float], *, probe: str, min_sample: int = 30,
) -> dict[str, Any] | None:
    vals = [float(v) for v in values if isinstance(v, (int, float))]
    if len(vals) < min_sample:
        return None
    if statistics.pstdev(vals) > 1e-12:
        return None
    return _finding("zero_variance", probe,
                    f"{probe} has zero variance over {len(vals)} numeric samples",
                    value=round(vals[0], 6), n_samples=len(vals))


def detect_class_disappearance(
    *, current: Iterable[Any], expected: Iterable[Any], probe: str,
) -> dict[str, Any] | None:
    cur, exp = set(current), set(expected)
    missing = sorted(str(m) for m in (exp - cur))
    if not missing:
        return None
    return _finding("class_disappearance", probe,
                    f"{probe} lost expected class(es): {', '.join(missing)}",
                    missing=missing)


def detect_low_cardinality(
    values: list[Any], *, probe: str, min_sample: int = 30, min_distinct: int = 2,
) -> dict[str, Any] | None:
    vals = [v for v in values if v is not None]
    if len(vals) < min_sample:
        return None
    distinct = len(set(vals))
    if distinct >= min_distinct:
        return None
    return _finding("low_cardinality", probe,
                    f"{probe} has only {distinct} distinct value(s) over {len(vals)} samples "
                    f"(expected >= {min_distinct})", observed_distinct=distinct, n_samples=len(vals))


# ---------------------------------------------------------------------------
# Runner — apply detectors to live artifacts; surface + route to quant-watch
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _read_csv_col(path: Path, col: str) -> list[str]:
    try:
        if not path.exists():
            return []
        import csv
        with path.open(newline="") as fh:
            return [row.get(col, "") for row in csv.DictReader(fh)]
    except Exception:
        return []


def run_semantic_liveness(root: Path | str = ".", now: str | None = None) -> dict[str, Any]:
    """Apply the detectors to a representative set of live fields. Never raises;
    AMBER-max (meta-monitor). Findings route to the quant-watch ledger."""
    root = Path(root)
    now = now or datetime.now(timezone.utc).isoformat()
    findings: list[dict[str, Any]] = []

    # regime label collapse (legitimately-calm "neutral" is an allowed single
    # state per the documented producer-ordering fix; only flag a NON-allowed
    # collapse). Cross-checks the known manual:regime_classifier_neutral_collapse.
    regimes = _read_csv_col(root / "outputs" / "performance" / "signal_outcomes.csv", "regime_label")
    f = detect_single_value_collapse(regimes, probe="regime_label", min_sample=30,
                                     allowed_single_values={"neutral", ""})
    if f:
        findings.append(f)

    # decision priority excessive-default (0.55 fallback plateau)
    plan = _read_json(root / "outputs" / "latest" / "decision_plan.json") or {}
    priorities = [d.get("priority") for d in (plan.get("decisions") or []) if isinstance(d, dict)]
    f = detect_excessive_default(priorities, default=0.55, probe="decision_priority",
                                 min_sample=30, max_default_frac=0.95)
    if f:
        findings.append(f)

    payload = dict(observe_only_envelope(now))
    payload.update({
        "source": "semantic_liveness",
        "schema_version": "1",
        "overall_status": "amber" if findings else "green",
        "finding_count": len(findings),
        "findings": findings,
        "disclaimer": (
            "Observe-only semantic-liveness meta-monitor. Detects degenerate "
            "(constant/default/zero-variance/class-disappeared) outputs with "
            "min-sample + documented-exception guards. Never RED; never mutates "
            "decisions, scores, or production state. Sub-RED findings route to "
            "the quant-watch ledger for continuity."
        ),
    })

    # route sub-RED findings to the quant-watch ledger (best-effort, non-fatal)
    if findings:
        try:
            from portfolio_automation import quant_watch_probes as qw
            register = getattr(qw, "register_manual_concern", None) or getattr(qw, "register_concern", None)
            if callable(register):
                for fnd in findings:
                    try:
                        register(root=str(root), concern=fnd["detail"],
                                 detector="semantic_liveness", severity="amber")
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        safe_write_json(OutputNamespace.LATEST, "semantic_liveness_status.json",
                        payload, base_dir=str(root / "outputs"))
    except Exception:
        pass
    return payload
