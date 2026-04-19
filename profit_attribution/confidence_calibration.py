"""
Profit Attribution — Confidence Calibration
=============================================
Observe-only analysis of whether confidence bands (low / medium / high) are
meaningfully predictive of execution outcomes.

Emits a ConfidenceCalibrationResult with:
  - A status label  (healthy / weak_separation / insufficient_data / no_data)
  - Per-band win rate and expectancy
  - Band order validity check
  - A plain-language, non-binding recommendation

Pure computation — no IO, no live config mutations, observe_only=True always.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from profit_attribution.models import ConfidenceCalibrationResult, StrategyPerformance

logger = logging.getLogger("profit_attribution.confidence_calibration")

# ---------------------------------------------------------------------------
# Calibration thresholds (observe-only — not used in live decision logic)
# ---------------------------------------------------------------------------

MIN_TOTAL_MATCHED: int = 10          # below this: no recommendation
MIN_BAND_MATCHED: int = 5            # minimum per-band for comparison
SEPARATION_THRESHOLD: float = 0.05   # 5pp win-rate gap = "meaningful" separation
STRONG_SEPARATION: float = 0.10      # 10pp = materially differentiated

_BAND_ORDER = ("low", "medium", "high")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calibrate_confidence_bands(
    by_confidence_band: List[StrategyPerformance],
) -> ConfidenceCalibrationResult:
    """
    Evaluate confidence-band separation and emit an observe-only result.

    Args:
        by_confidence_band: Ordered list [low, medium, high] from
                            execution_metrics._analyze_by_confidence_band().

    Returns:
        ConfidenceCalibrationResult — never raises; degrades gracefully.
    """
    band_map: Dict[str, StrategyPerformance] = {b.name: b for b in by_confidence_band}
    low = band_map.get("low")
    med = band_map.get("medium")
    high = band_map.get("high")

    low_m  = low.attributable  if low  else 0
    med_m  = med.attributable  if med  else 0
    high_m = high.attributable if high else 0
    total  = low_m + med_m + high_m

    low_wr  = low.win_rate  if low  else None
    med_wr  = med.win_rate  if med  else None
    high_wr = high.win_rate if high else None

    low_exp  = _expectancy(low)  if low  else None
    med_exp  = _expectancy(med)  if med  else None
    high_exp = _expectancy(high) if high else None

    # --- No data at all ---
    if total == 0:
        return _build(
            status="no_data",
            low_m=0, med_m=0, high_m=0,
            low_wr=None, med_wr=None, high_wr=None,
            low_exp=None, med_exp=None, high_exp=None,
            band_order_valid=None,
            strongest=None, weakest=None,
            recommendation="No matched execution events. Confidence calibration requires execution data.",
            reason="total_matched=0",
        )

    # --- Insufficient sample size ---
    if total < MIN_TOTAL_MATCHED or high_m < MIN_BAND_MATCHED or med_m < MIN_BAND_MATCHED:
        return _build(
            status="insufficient_data",
            low_m=low_m, med_m=med_m, high_m=high_m,
            low_wr=low_wr, med_wr=med_wr, high_wr=high_wr,
            low_exp=low_exp, med_exp=med_exp, high_exp=high_exp,
            band_order_valid=None,
            strongest=None, weakest=None,
            recommendation=(
                f"Insufficient matched samples for calibration. "
                f"Need ≥{MIN_TOTAL_MATCHED} total and ≥{MIN_BAND_MATCHED} per band "
                f"(have: low={low_m}, medium={med_m}, high={high_m}). "
                "Collect more execution data before acting on this analysis."
            ),
            reason=f"insufficient_samples: total={total}, high={high_m}, medium={med_m}",
        )

    # --- Samples sufficient but no win-rate data yet (5d returns missing) ---
    if high_wr is None or med_wr is None:
        return _build(
            status="insufficient_data",
            low_m=low_m, med_m=med_m, high_m=high_m,
            low_wr=low_wr, med_wr=med_wr, high_wr=high_wr,
            low_exp=low_exp, med_exp=med_exp, high_exp=high_exp,
            band_order_valid=None,
            strongest=None, weakest=None,
            recommendation=(
                "Matched events exist but 5-day forward returns are not yet available "
                "for one or more bands. Run additional scans to populate return data."
            ),
            reason="win_rate unavailable: 5d return data missing",
        )

    # --- Full analysis ---
    _w = lambda v: v if v is not None else 0.0

    band_order_valid = _w(high_wr) >= _w(med_wr) and _w(med_wr) >= _w(low_wr)

    rated = [(name, w) for name, w in [("low", low_wr), ("medium", med_wr), ("high", high_wr)] if w is not None]
    strongest = max(rated, key=lambda x: x[1])[0] if rated else None
    weakest  = min(rated, key=lambda x: x[1])[0] if rated else None

    high_med_gap = _w(high_wr) - _w(med_wr)
    med_low_gap  = _w(med_wr)  - _w(low_wr)

    # Collect specific issues for the recommendation
    issues: list[str] = []
    if not band_order_valid:
        issues.append("band_order_inverted")
    if high_med_gap < SEPARATION_THRESHOLD:
        issues.append(f"high_medium_gap_small({high_med_gap * 100:.1f}pp)")
    if med_low_gap < SEPARATION_THRESHOLD:
        issues.append(f"medium_low_gap_small({med_low_gap * 100:.1f}pp)")

    if not issues:
        status = "healthy"
        if high_med_gap >= STRONG_SEPARATION:
            recommendation = (
                "Confidence thresholds appear well-calibrated. "
                "High-confidence events materially outperform medium and low."
            )
            reason = (
                f"high–medium gap {high_med_gap * 100:.1f}pp (≥{STRONG_SEPARATION * 100:.0f}pp), "
                f"medium–low gap {med_low_gap * 100:.1f}pp, band order valid"
            )
        else:
            recommendation = (
                "Confidence thresholds appear reasonably calibrated. "
                "Band order is correct and separation is adequate."
            )
            reason = (
                f"high–medium gap {high_med_gap * 100:.1f}pp, "
                f"medium–low gap {med_low_gap * 100:.1f}pp, band order valid"
            )
    else:
        status = "weak_separation"
        parts: list[str] = []

        if not band_order_valid:
            parts.append(
                "Band order is inverted — high-confidence events are not outperforming "
                "lower-confidence events. Confidence scoring may need review."
            )
        else:
            if high_med_gap < SEPARATION_THRESHOLD:
                parts.append(
                    f"High-confidence band does not meaningfully outperform medium-confidence events "
                    f"(gap {high_med_gap * 100:.1f}pp < {SEPARATION_THRESHOLD * 100:.0f}pp threshold). "
                    "Consider raising the high-confidence threshold (currently > 0.80)."
                )
            if med_low_gap < SEPARATION_THRESHOLD:
                parts.append(
                    f"Medium and low bands show weak separation "
                    f"(gap {med_low_gap * 100:.1f}pp < {SEPARATION_THRESHOLD * 100:.0f}pp threshold). "
                    "The 0.65 split may not meaningfully distinguish these tiers."
                )

        recommendation = " ".join(parts) if parts else "Weak separation detected. No specific recommendation at this time."
        reason = "; ".join(issues)

    return _build(
        status=status,
        low_m=low_m, med_m=med_m, high_m=high_m,
        low_wr=low_wr, med_wr=med_wr, high_wr=high_wr,
        low_exp=low_exp, med_exp=med_exp, high_exp=high_exp,
        band_order_valid=band_order_valid,
        strongest=strongest, weakest=weakest,
        recommendation=recommendation,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build(
    status: str,
    low_m: int, med_m: int, high_m: int,
    low_wr: Optional[float], med_wr: Optional[float], high_wr: Optional[float],
    low_exp: Optional[float], med_exp: Optional[float], high_exp: Optional[float],
    band_order_valid: Optional[bool],
    strongest: Optional[str], weakest: Optional[str],
    recommendation: str, reason: str,
) -> ConfidenceCalibrationResult:
    return ConfidenceCalibrationResult(
        observe_only=True,
        status=status,
        low_matched=low_m,
        medium_matched=med_m,
        high_matched=high_m,
        low_win_rate=low_wr,
        medium_win_rate=med_wr,
        high_win_rate=high_wr,
        low_expectancy=low_exp,
        medium_expectancy=med_exp,
        high_expectancy=high_exp,
        band_order_valid=band_order_valid,
        strongest_band=strongest,
        weakest_band=weakest,
        recommendation=recommendation,
        recommendation_reason=reason,
    )


def _expectancy(perf: StrategyPerformance) -> Optional[float]:
    """Compute expectancy from a StrategyPerformance bucket."""
    wr = perf.win_rate
    ag = perf.avg_gain
    al = perf.avg_loss
    if wr is None or ag is None or al is None:
        return None
    return round(wr * ag + (1 - wr) * al, 6)
