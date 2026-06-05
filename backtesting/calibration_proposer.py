"""
Calibration-correction proposer  (additive | advisory-only | observe-only | proposes-only)

Pattern-Improvement Loop — sub-project D1. The first real_signals_live run showed an
INVERTED confidence calibration (slope -11.345; the 40-60 band outperformed the 80-100
band — confidence is anti-predictive on the in-sample window). This module turns that
observation into a bounded, owner-gated *review artifact*: it detects the inversion and
proposes a monotone recalibration map (empirical hit-rate per confidence band, isotonically
smoothed to be non-decreasing).

It NEVER mutates confidence_score or any scoring logic, and it NEVER applies the map.
Because the slope is computed on an in-sample window, an apply fit on it would overfit —
so `apply_gate` is "oos_unconfirmed" until the walk-forward OOS window matures
(`oos_window.folds_possible`). The owner reviews; the protected apply path (E) is separate.

Observe-only: reads a poc_simulation_results-shaped dict and writes one review artifact to
OutputNamespace.POLICY. Any failure degrades to a status dict; never raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_OBSERVE_ONLY = True
_GENERATED_BY = "backtesting.calibration_proposer"


def _band_midpoint(label: str) -> float | None:
    """Midpoint of an 'A-B' band label as a fraction in [0,1]; None if unparseable."""
    try:
        lo, hi = (float(x) for x in str(label).split("-", 1))
    except (ValueError, TypeError):
        return None
    return (lo + hi) / 200.0  # /100 to fraction, /2 for midpoint


def _isotonic_nondecreasing(values: list[float]) -> list[float]:
    """Pool-adjacent-violators: smallest non-decreasing sequence closest (L2) to
    *values*. Dependency-free. Returns a new list of the same length."""
    # Each block: (sum, count) so the pooled value is sum/count.
    blocks: list[list[float]] = []  # [sum, count]
    for v in values:
        blocks.append([float(v), 1.0])
        while len(blocks) >= 2 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out: list[float] = []
    for s, c in blocks:
        out.extend([s / c] * int(c))
    return out


def _spearman_decreasing(mids: list[float], hits: list[float]) -> bool:
    """True when higher-confidence bands tend to have LOWER hit rates (a fall-back
    inversion signal when slope is absent). Pure, total."""
    n = len(mids)
    if n < 2:
        return False
    # Count concordant vs discordant ordered pairs (Kendall-style sign test).
    disc = conc = 0
    for i in range(n):
        for j in range(i + 1, n):
            dm = mids[j] - mids[i]
            dh = hits[j] - hits[i]
            if dm == 0 or dh == 0:
                continue
            if (dm > 0) == (dh > 0):
                conc += 1
            else:
                disc += 1
    return disc > conc


def propose_calibration_correction(results: dict, *, min_band_n: int = 20) -> dict[str, Any]:
    """Detect calibration inversion in a poc_simulation_results-shaped dict and propose
    a monotone recalibration map over the confidence bands with enough sample.

    Returns ``{observe_only, proposed_only, advisory_only, generated_by, status,
    calibration_slope, inverted, bands, apply_gate, rationale}``. ``status`` is
    'ok' | 'insufficient' (fewer than 2 bands with n >= min_band_n) | 'degraded'.
    ``apply_gate`` is 'oos_unconfirmed' until ``oos_window.folds_possible`` is true.
    Never raises.
    """
    try:
        cal = results.get("calibration") or {}
        buckets = cal.get("buckets")
        slope = cal.get("calibration_slope")
        oos = results.get("oos_window") or {}
        apply_gate = "ready" if oos.get("folds_possible") else "oos_unconfirmed"

        if not isinstance(buckets, list):
            return {
                "observe_only": _OBSERVE_ONLY, "proposed_only": True, "advisory_only": True,
                "generated_by": _GENERATED_BY, "status": "insufficient",
                "calibration_slope": slope, "inverted": False, "bands": [],
                "apply_gate": apply_gate,
                "rationale": "no calibration buckets present; nothing to propose",
            }

        usable = [b for b in buckets if isinstance(b, dict) and (b.get("count") or 0) >= min_band_n]
        # ascending by band midpoint
        usable = sorted(usable, key=lambda b: _band_midpoint(b.get("label")) or 0.0)
        if len(usable) < 2:
            return {
                "observe_only": _OBSERVE_ONLY, "proposed_only": True, "advisory_only": True,
                "generated_by": _GENERATED_BY, "status": "insufficient",
                "calibration_slope": slope, "inverted": False, "bands": [],
                "apply_gate": apply_gate,
                "rationale": f"fewer than 2 bands with n >= {min_band_n}; insufficient to propose",
            }

        mids = [_band_midpoint(b.get("label")) or 0.0 for b in usable]
        hits = [float(b.get("hit_rate") or 0.0) for b in usable]
        inverted = (isinstance(slope, (int, float)) and slope < 0) or _spearman_decreasing(mids, hits)

        smoothed = _isotonic_nondecreasing([h / 100.0 for h in hits])
        bands = [
            {
                "band": b.get("label"),
                "n": b.get("count"),
                "empirical_hit_rate": round(hits[i], 2),
                "suggested_calibrated_conf": round(smoothed[i], 4),
            }
            for i, b in enumerate(usable)
        ]
        rationale = (
            "Confidence is inverted on the in-sample window (higher-confidence bands "
            "underperform). Proposed map isotonically remaps each band to its empirical "
            "hit-rate. PROVISIONAL — fit on in-sample data; do not apply until the OOS "
            "window matures (apply_gate=ready)."
            if inverted else
            "Calibration is monotone (well-ordered); no correction proposed."
        )
        return {
            "observe_only": _OBSERVE_ONLY, "proposed_only": True, "advisory_only": True,
            "generated_by": _GENERATED_BY, "status": "ok",
            "calibration_slope": slope, "inverted": inverted, "bands": bands,
            "apply_gate": apply_gate, "rationale": rationale,
        }
    except Exception as exc:  # observe-only: degrade, never raise
        return {
            "observe_only": _OBSERVE_ONLY, "proposed_only": True, "advisory_only": True,
            "generated_by": _GENERATED_BY, "status": "degraded", "error": str(exc),
            "inverted": False, "bands": [], "apply_gate": "oos_unconfirmed",
        }


def _markdown(payload: dict[str, Any]) -> str:
    L = ["# Calibration-Correction Proposal",
         "",
         "> Observe-only, proposes-only. PROVISIONAL review artifact for owner approval; "
         "applies nothing. No scoring change.",
         "",
         f"- Status: {payload.get('status')}  |  inverted: {payload.get('inverted')}  "
         f"|  slope: {payload.get('calibration_slope')}  |  apply_gate: {payload.get('apply_gate')}",
         "",
         payload.get("rationale", ""),
         ""]
    bands = payload.get("bands") or []
    if bands:
        L += ["## Proposed monotone recalibration map", "",
              "| Band | n | Empirical hit-rate % | Suggested calibrated conf |",
              "|---|---|---|---|"]
        for b in bands:
            L.append(f"| {b['band']} | {b['n']} | {b['empirical_hit_rate']} | "
                     f"{b['suggested_calibrated_conf']} |")
    return "\n".join(L) + "\n"


def write_calibration_proposal(payload: dict[str, Any], base_dir: str = "outputs") -> Path:
    """Write the proposal JSON+MD to OutputNamespace.POLICY; return the JSON path."""
    from portfolio_automation.data_governance import (
        OutputNamespace, safe_write_json, safe_write_text,
    )
    safe_write_text(OutputNamespace.POLICY, "calibration_correction_proposal.md",
                    _markdown(payload), base_dir=base_dir)
    return safe_write_json(OutputNamespace.POLICY, "calibration_correction_proposal.json",
                           payload, base_dir=base_dir)
