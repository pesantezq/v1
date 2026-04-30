"""
Outcome attribution for recommendation events.

Attribution Method: Option A — Portfolio-level proxy
====================================================
For each recommendation event in recommendation_history.jsonl we look up the
portfolio total_value from the `snapshots` SQLite table at the recommendation's
run_date and at T+1, T+3, T+5, T+10 calendar days forward.

Why Option A?
  These are portfolio-management advisory recommendations (emergency fund,
  drift, savings rate, leverage) — not stock picks.  There is no single ticker
  to evaluate against.  The portfolio total_value is the clearest measure of
  whether conditions improved or worsened after a recommendation was issued.
  It is transparent, uses only data already present in the system, and requires
  no external API calls.

Limitation / caveat
  Portfolio value is updated only when a run executes, so forward horizons are
  approximated as "nearest actual run on or after T+N calendar days."  In a
  daily-run setup this is close to true; in a weekly-run setup T+1 and T+3 may
  map to the same snapshot.  Coverage statistics are included in every report.

Forward return formula
  forward_return(H) = (value_at_(T+H) - value_at_T) / value_at_T

"T+N calendar days" resolves to the nearest actual run snapshot at or after
T+N days.  MAX_GAP_DAYS calendar days of slack are allowed before marking the
horizon as missing (null).

MFE / MAE definition (per-recommendation, over all non-null forward horizons)
  MFE = max(0,  max(all forward_returns))   # best case in the window
  MAE = min(0,  min(all forward_returns))   # worst case in the window

Outcome thresholds (all explicit, never hidden)
  POSITIVE_RETURN_THRESHOLD  =  0.00   (+0%  any gain is "favorable")
  STRONG_WIN_THRESHOLD       =  0.02   (+2%  strong favorable at primary horizon)
  ACCEPTABLE_LOSS_THRESHOLD  = -0.01   (-1%  acceptable drawdown)
  ADVERSE_THRESHOLD          = -0.02   (-2%  adverse / loss at primary horizon)
  MAX_GAP_DAYS               =  3      (slack days to find a forward snapshot)
  SMALL_SAMPLE_WARNING       =  5      (buckets < 5 records are flagged)
  PRIMARY_HORIZON            =  5      (the T+5d return is used for hit-rate)
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from policy_evaluator.history_writer import load_history
from policy_evaluator.infrastructure import parse_timestamp

logger = logging.getLogger("policy_evaluator.outcome_attributor")

# ---------------------------------------------------------------------------
# Configuration constants (all explicit, never hidden in formulas)
# ---------------------------------------------------------------------------

POSITIVE_RETURN_THRESHOLD: float = 0.00    # any gain = favorable
STRONG_WIN_THRESHOLD: float = 0.02         # +2% = strong favorable
ACCEPTABLE_LOSS_THRESHOLD: float = -0.01   # -1% = acceptable
ADVERSE_THRESHOLD: float = -0.02           # -2% = adverse
MAX_GAP_DAYS: int = 3                      # slack for finding forward snapshots
SMALL_SAMPLE_WARNING: int = 5             # buckets below this are flagged
PRIMARY_HORIZON: int = 5                   # T+5d drives the headline hit rate
HORIZONS: Tuple[int, ...] = (1, 3, 5, 10)
CONFIDENCE_TIER_ORDER: Tuple[str, ...] = ("low", "medium", "high")
PRIORITY_BUCKET_ORDER: Tuple[str, ...] = ("0-33", "34-66", "67-100")

_DEFAULT_DB_PATH = Path("data/portfolio.db")
_DEFAULT_HISTORY_PATH = Path("outputs/policy/recommendation_history.jsonl")

ATTRIBUTION_METHOD = "option_a_portfolio_proxy"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AttributedRecord:
    """One recommendation event linked to its realized portfolio outcomes."""

    # Identity
    rec_id: str
    rec_base_id: str
    run_id: str
    run_date: str            # YYYY-MM-DD parsed from run_id

    # Context (copied from history record)
    action_level: str
    confidence: int          # 0-100
    confidence_tier: str     # "low" | "medium" | "high"
    score: int               # final score after confidence penalty
    raw_score: int           # score before confidence penalty
    impact_area: str
    priority: int            # 0-100
    degraded_mode: bool
    data_mode: str           # "live" | "mixed" | "fallback"
    drawdown_regime: str

    # Outcome — all Optional; null = snapshot not available for that horizon
    portfolio_value_at_t: Optional[float]
    forward_return_1d: Optional[float]
    forward_return_3d: Optional[float]
    forward_return_5d: Optional[float]
    forward_return_10d: Optional[float]
    # MFE = max(0, max(non-null forward returns)) — best excursion in window
    # MAE = min(0, min(non-null forward returns)) — worst excursion in window
    mfe: Optional[float]
    mae: Optional[float]

    # Attribution metadata
    attributable: bool       # True iff value_at_t AND ≥1 forward return exist
    attribution_note: str    # human-readable reason when not attributable
    portfolio_snapshot_date_t: Optional[str] = None
    forward_snapshot_date_1d: Optional[str] = None
    forward_snapshot_date_3d: Optional[str] = None
    forward_snapshot_date_5d: Optional[str] = None
    forward_snapshot_date_10d: Optional[str] = None

    def forward_returns(self) -> Dict[int, Optional[float]]:
        return {
            1: self.forward_return_1d,
            3: self.forward_return_3d,
            5: self.forward_return_5d,
            10: self.forward_return_10d,
        }

    def forward_snapshot_dates(self) -> Dict[int, Optional[str]]:
        return {
            1: self.forward_snapshot_date_1d,
            3: self.forward_snapshot_date_3d,
            5: self.forward_snapshot_date_5d,
            10: self.forward_snapshot_date_10d,
        }

    def hit_at_primary_horizon(self) -> Optional[bool]:
        """True iff forward_return at PRIMARY_HORIZON > POSITIVE_RETURN_THRESHOLD."""
        r = self.forward_returns().get(PRIMARY_HORIZON)
        if r is None:
            return None
        return r > POSITIVE_RETURN_THRESHOLD


@dataclass
class BucketOutcome:
    """Aggregated outcome metrics for a bucket (regime / tier / mode / etc.)."""
    label: str
    count: int = 0
    attributable_count: int = 0
    hit_count: int = 0               # forward_return_5d > 0
    strong_win_count: int = 0        # forward_return_5d > STRONG_WIN_THRESHOLD
    adverse_count: int = 0           # forward_return_5d < ADVERSE_THRESHOLD
    sum_return_1d: float = 0.0
    sum_return_3d: float = 0.0
    sum_return_5d: float = 0.0
    sum_return_10d: float = 0.0
    sum_mfe: float = 0.0
    sum_mae: float = 0.0
    count_1d: int = 0                # attributable at each horizon
    count_3d: int = 0
    count_5d: int = 0
    count_10d: int = 0
    count_mfe: int = 0
    count_mae: int = 0
    returns_5d: List[float] = field(default_factory=list)
    small_sample: bool = False       # set post-aggregation

    def hit_rate(self) -> Optional[float]:
        if self.attributable_count < 1:
            return None
        # Hit-rate uses only recs that have a 5d return
        if self.count_5d < 1:
            return None
        return round(self.hit_count / self.count_5d, 4)

    def avg_return(self, horizon: int) -> Optional[float]:
        n = getattr(self, f"count_{horizon}d")
        s = getattr(self, f"sum_return_{horizon}d")
        return round(s / n, 6) if n > 0 else None

    def avg_mfe(self) -> Optional[float]:
        return round(self.sum_mfe / self.count_mfe, 6) if self.count_mfe > 0 else None

    def avg_mae(self) -> Optional[float]:
        return round(self.sum_mae / self.count_mae, 6) if self.count_mae > 0 else None

    def median_return_5d(self) -> Optional[float]:
        return round(float(median(self.returns_5d)), 6) if self.returns_5d else None

    def strong_win_rate(self) -> Optional[float]:
        return round(self.strong_win_count / self.count_5d, 4) if self.count_5d > 0 else None

    def adverse_rate(self) -> Optional[float]:
        return round(self.adverse_count / self.count_5d, 4) if self.count_5d > 0 else None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "count": self.count,
            "attributable_count": self.attributable_count,
            "hit_count": self.hit_count,
            "strong_win_count": self.strong_win_count,
            "adverse_count": self.adverse_count,
            "hit_rate": self.hit_rate(),
            "strong_win_rate": self.strong_win_rate(),
            "adverse_rate": self.adverse_rate(),
            "avg_forward_return_1d": self.avg_return(1),
            "avg_forward_return_3d": self.avg_return(3),
            "avg_forward_return_5d": self.avg_return(5),
            "median_forward_return_5d": self.median_return_5d(),
            "avg_forward_return_10d": self.avg_return(10),
            "avg_mfe": self.avg_mfe(),
            "avg_mae": self.avg_mae(),
            "coverage_by_horizon": {
                "count_1d": self.count_1d,
                "count_3d": self.count_3d,
                "count_5d": self.count_5d,
                "count_10d": self.count_10d,
            },
            "small_sample": self.small_sample,
        }


@dataclass
class OutcomeResult:
    """Full outcome attribution result."""

    # Provenance
    generated_at: str
    history_path: str
    db_path: str
    attribution_method: str = ATTRIBUTION_METHOD

    # Coverage
    total_records: int = 0
    attributable_records: int = 0
    unevaluable_records: int = 0
    coverage_rate: Optional[float] = None   # attributable / total
    date_range: dict = field(default_factory=dict)
    coverage_by_horizon: dict = field(default_factory=dict)
    aliasing_notes: dict = field(default_factory=dict)
    sample_quality: str = "mixed"
    outcome_data_gaps: dict = field(default_factory=dict)

    # Overall forward returns (avg across all attributed recs)
    avg_forward_return_1d: Optional[float] = None
    avg_forward_return_3d: Optional[float] = None
    avg_forward_return_5d: Optional[float] = None
    avg_forward_return_10d: Optional[float] = None

    # MFE / MAE averages
    avg_mfe: Optional[float] = None
    avg_mae: Optional[float] = None

    # Headline hit rate at PRIMARY_HORIZON (5d)
    hit_rate_overall: Optional[float] = None
    strong_win_rate_overall: Optional[float] = None
    adverse_rate_overall: Optional[float] = None

    # Breakdowns — each is a dict keyed by bucket label
    by_confidence_tier: Dict[str, dict] = field(default_factory=dict)
    by_degraded_mode: Dict[str, dict] = field(default_factory=dict)
    by_regime: Dict[str, dict] = field(default_factory=dict)
    by_drawdown_regime: Dict[str, dict] = field(default_factory=dict)
    by_action_level: Dict[str, dict] = field(default_factory=dict)
    by_impact_area: Dict[str, dict] = field(default_factory=dict)
    by_priority_bucket: Dict[str, dict] = field(default_factory=dict)
    by_score_quintile: List[dict] = field(default_factory=list)
    by_score_decile: List[dict] = field(default_factory=list)
    confidence_calibration: dict = field(default_factory=dict)

    # Notable items (advisory — top wins and misses by 5d return)
    notable_wins: List[dict] = field(default_factory=list)
    notable_misses: List[dict] = field(default_factory=list)

    # Data quality
    data_quality_notes: List[str] = field(default_factory=list)

    # Thresholds used (for auditability)
    outcome_thresholds: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "history_path": self.history_path,
            "db_path": self.db_path,
            "attribution_method": self.attribution_method,
            "coverage": {
                "total_records": self.total_records,
                "attributable_records": self.attributable_records,
                "unevaluable_records": self.unevaluable_records,
                "coverage_rate": self.coverage_rate,
                "date_range": self.date_range,
            },
            "coverage_by_horizon": self.coverage_by_horizon,
            "aliasing_notes": self.aliasing_notes,
            "sample_quality": self.sample_quality,
            "outcome_data_gaps": self.outcome_data_gaps,
            "overall": {
                "hit_rate": self.hit_rate_overall,
                "strong_win_rate": self.strong_win_rate_overall,
                "adverse_rate": self.adverse_rate_overall,
                "avg_forward_return_1d": self.avg_forward_return_1d,
                "avg_forward_return_3d": self.avg_forward_return_3d,
                "avg_forward_return_5d": self.avg_forward_return_5d,
                "avg_forward_return_10d": self.avg_forward_return_10d,
                "avg_mfe": self.avg_mfe,
                "avg_mae": self.avg_mae,
            },
            "by_confidence_tier": self.by_confidence_tier,
            "confidence_calibration": self.confidence_calibration,
            "by_degraded_mode": self.by_degraded_mode,
            "by_regime": self.by_regime,
            "by_drawdown_regime": self.by_drawdown_regime,
            "by_action_level": self.by_action_level,
            "by_impact_area": self.by_impact_area,
            "by_priority_bucket": self.by_priority_bucket,
            "by_score_quintile": self.by_score_quintile,
            "by_score_decile": self.by_score_decile,
            "notable_wins": self.notable_wins,
            "notable_misses": self.notable_misses,
            "data_quality_notes": self.data_quality_notes,
            "outcome_thresholds": self.outcome_thresholds,
        }


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def load_portfolio_snapshots(
    db_path: Optional[Path] = None,
) -> List[Tuple[date, float]]:
    """
    Load portfolio snapshots from SQLite and return a list of (run_date, total_value)
    pairs sorted by date ascending.

    Groups by calendar date.  When multiple runs occurred on the same day (e.g.
    daily + weekly), we take the LAST recorded_at snapshot to capture the most
    complete state.

    Returns an empty list if the database does not exist or the table is empty.
    """
    path = db_path or _DEFAULT_DB_PATH
    if not Path(path).exists():
        logger.debug("outcome_attributor: DB not found at %s — no snapshots", path)
        return []

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            # TODO(v2-user-scope): review aggregate query behavior for multi-user support
            # This aggregates all snapshots across users; add WHERE user_id=? when
            # multi-user attribution is needed.
            rows = conn.execute(
                """
                SELECT run_id, total_value, recorded_at
                FROM snapshots
                WHERE total_value IS NOT NULL AND total_value > 0
                ORDER BY recorded_at ASC
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("outcome_attributor: could not read snapshots — %s", exc)
        return []

    # Group by calendar date; keep the last value per date
    by_date: Dict[date, float] = {}
    for row in rows:
        run_date = _parse_date_from_run_id(str(row["run_id"]))
        if run_date is None:
            # Fallback: parse from recorded_at
            try:
                run_date = datetime.fromisoformat(str(row["recorded_at"])).date()
            except (ValueError, TypeError):
                continue
        by_date[run_date] = float(row["total_value"])

    return sorted(by_date.items())


def _parse_date_from_run_id(run_id: str) -> Optional[date]:
    """
    Extract the YYYY-MM-DD date from a run_id like '2026-04-16_daily'.
    Returns None if the format does not match.
    """
    parts = run_id.split("_")
    if len(parts) >= 1:
        try:
            return date.fromisoformat(parts[0])
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Forward-return calculation helpers
# ---------------------------------------------------------------------------

def _find_value_at_or_after(
    sorted_snapshots: List[Tuple[date, float]],
    target_date: date,
    max_gap_days: int = MAX_GAP_DAYS,
) -> Tuple[Optional[float], Optional[date]]:
    """
    Find the portfolio value for the nearest snapshot on or after target_date.
    Returns (None, None) if no snapshot falls within max_gap_days.

    Parameters
    ----------
    sorted_snapshots : [(run_date, total_value)] ascending
    target_date      : the date we're looking for
    max_gap_days     : how many calendar days of slack to allow

    Returns
    -------
    (value, actual_date) or (None, None)
    """
    deadline = target_date + timedelta(days=max_gap_days)
    for snap_date, snap_val in sorted_snapshots:
        if snap_date >= target_date:
            if snap_date <= deadline:
                return snap_val, snap_date
            break  # sorted, so nothing closer will follow
    return None, None


def _compute_forward_return_details(
    value_at_t: float,
    sorted_snapshots: List[Tuple[date, float]],
    run_date: date,
    horizons: Tuple[int, ...] = HORIZONS,
    max_gap_days: int = MAX_GAP_DAYS,
) -> Tuple[
    Dict[int, Optional[float]],
    Dict[int, Optional[date]],
    Optional[float],
    Optional[float],
]:
    """Compute forward returns plus the actual snapshot date used for each horizon."""
    forward_returns: Dict[int, Optional[float]] = {}
    forward_dates: Dict[int, Optional[date]] = {}
    non_null_returns: List[float] = []

    for h in horizons:
        target = run_date + timedelta(days=h)
        val, actual_date = _find_value_at_or_after(sorted_snapshots, target, max_gap_days)
        if val is not None and value_at_t > 0:
            ret = (val - value_at_t) / value_at_t
            forward_returns[h] = round(ret, 6)
            forward_dates[h] = actual_date
            non_null_returns.append(ret)
        else:
            forward_returns[h] = None
            forward_dates[h] = None

    mfe: Optional[float] = None
    mae: Optional[float] = None
    if non_null_returns:
        mfe = round(max(0.0, max(non_null_returns)), 6)
        mae = round(min(0.0, min(non_null_returns)), 6)

    return forward_returns, forward_dates, mfe, mae


def _compute_forward_returns(
    value_at_t: float,
    sorted_snapshots: List[Tuple[date, float]],
    run_date: date,
    horizons: Tuple[int, ...] = HORIZONS,
    max_gap_days: int = MAX_GAP_DAYS,
) -> Tuple[Dict[int, Optional[float]], Optional[float], Optional[float]]:
    """
    Compute forward returns for each horizon day.

    Formula for each horizon H:
        forward_return(H) = (value_at_(T+H) − value_at_T) / value_at_T

    MFE = max(0, max(non-null returns))  — best excursion in window
    MAE = min(0, min(non-null returns))  — worst excursion in window

    Parameters
    ----------
    value_at_t      : portfolio value at recommendation date
    sorted_snapshots: [(run_date, total_value)] ascending
    run_date        : the recommendation date (T)
    horizons        : forward horizon days (default 1, 3, 5, 10)
    max_gap_days    : slack for finding a matching snapshot

    Returns
    -------
    (forward_returns_dict, mfe, mae)
    """
    forward_returns, _, mfe, mae = _compute_forward_return_details(
        value_at_t,
        sorted_snapshots,
        run_date,
        horizons=horizons,
        max_gap_days=max_gap_days,
    )
    return forward_returns, mfe, mae


def _confidence_tier(confidence: int) -> str:
    """Map confidence 0-100 to tier label (matches evaluator.py boundaries)."""
    if confidence <= 50:
        return "low"
    elif confidence <= 80:
        return "medium"
    return "high"


def _score_quintile_label(score: int) -> str:
    """Map score 0-100 to quintile label."""
    if score <= 20:
        return "0-20"
    elif score <= 40:
        return "21-40"
    elif score <= 60:
        return "41-60"
    elif score <= 80:
        return "61-80"
    return "81-100"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off", ""}:
            return False
    return bool(value)


def _score_decile_label(score: int) -> str:
    """Map score 0-100 to decile label."""
    bounded = max(0, min(100, _safe_int(score, 0)))
    if bounded <= 10:
        return "0-10"
    lower = ((bounded - 1) // 10) * 10 + 1
    upper = lower + 9
    if upper >= 100:
        return "91-100"
    return f"{lower}-{upper}"


def _priority_bucket_label(priority: int) -> str:
    """Bucket priority into coarse, explainable bands."""
    bounded = max(0, min(100, _safe_int(priority, 0)))
    if bounded <= 33:
        return "0-33"
    if bounded <= 66:
        return "34-66"
    return "67-100"


def _history_date_range(records: List[dict]) -> dict:
    """Return first/last recommendation date with tolerant timestamp parsing."""
    dates: List[str] = []
    for rec in records:
        parsed = parse_timestamp(rec.get("timestamp"))
        if parsed is not None:
            dates.append(parsed.date().isoformat())
            continue
        run_date = _parse_date_from_run_id(str(rec.get("run_id", "")))
        if run_date is not None:
            dates.append(run_date.isoformat())
    if not dates:
        return {"first": None, "last": None}
    return {"first": min(dates), "last": max(dates)}


def _safe_rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _summarize_aliasing(attributed: List[AttributedRecord]) -> dict:
    pairs_1d_3d = [
        ar for ar in attributed
        if ar.forward_snapshot_date_1d and ar.forward_snapshot_date_3d
    ]
    pairs_3d_5d = [
        ar for ar in attributed
        if ar.forward_snapshot_date_3d and ar.forward_snapshot_date_5d
    ]
    count_1d_3d_same = sum(
        1 for ar in pairs_1d_3d
        if ar.forward_snapshot_date_1d == ar.forward_snapshot_date_3d
    )
    count_3d_5d_same = sum(
        1 for ar in pairs_3d_5d
        if ar.forward_snapshot_date_3d == ar.forward_snapshot_date_5d
    )
    return {
        "count_1d_3d_same_snapshot": count_1d_3d_same,
        "count_3d_5d_same_snapshot": count_3d_5d_same,
        "comparison_count_1d_3d": len(pairs_1d_3d),
        "comparison_count_3d_5d": len(pairs_3d_5d),
        "rate_1d_3d_same_snapshot": _safe_rate(count_1d_3d_same, len(pairs_1d_3d)),
        "rate_3d_5d_same_snapshot": _safe_rate(count_3d_5d_same, len(pairs_3d_5d)),
    }


def _classify_sample_quality(
    sorted_snapshots: List[Tuple[date, float]],
    aliasing_notes: dict,
) -> str:
    """Classify cadence quality from snapshot spacing and horizon aliasing."""
    if len(sorted_snapshots) < 2:
        return "mixed"

    day_gaps = [
        (current[0] - previous[0]).days
        for previous, current in zip(sorted_snapshots, sorted_snapshots[1:])
    ]
    avg_gap = sum(day_gaps) / len(day_gaps) if day_gaps else None
    alias_1d_3d = aliasing_notes.get("rate_1d_3d_same_snapshot") or 0.0
    alias_3d_5d = aliasing_notes.get("rate_3d_5d_same_snapshot") or 0.0

    if avg_gap is not None and avg_gap <= 2.0 and alias_1d_3d < 0.25 and alias_3d_5d < 0.25:
        return "dense_daily"
    if avg_gap is not None and (avg_gap >= 5.0 or alias_1d_3d >= 0.5 or alias_3d_5d >= 0.5):
        return "sparse_weekly"
    return "mixed"


def _summarize_outcome_gaps(attributed: List[AttributedRecord]) -> dict:
    with_base = [ar for ar in attributed if ar.portfolio_value_at_t is not None]
    return {
        "missing_run_date_count": sum("cannot parse run_date" in ar.attribution_note for ar in attributed),
        "missing_value_at_t_count": sum("no snapshot within" in ar.attribution_note for ar in attributed),
        "missing_all_forward_horizons_count": sum(
            "no forward snapshots after run_date" in ar.attribution_note for ar in attributed
        ),
        "records_with_base_snapshot": len(with_base),
        "missing_1d_count": sum(ar.forward_return_1d is None for ar in with_base),
        "missing_3d_count": sum(ar.forward_return_3d is None for ar in with_base),
        "missing_5d_count": sum(ar.forward_return_5d is None for ar in with_base),
        "missing_10d_count": sum(ar.forward_return_10d is None for ar in with_base),
    }


def _confidence_calibration_summary(by_confidence_tier: Dict[str, dict]) -> dict:
    notes: List[str] = []
    small_sample_tiers = [
        tier for tier in CONFIDENCE_TIER_ORDER
        if by_confidence_tier.get(tier, {}).get("small_sample")
    ]
    if small_sample_tiers:
        notes.append(
            "Confidence buckets with small samples: " + ", ".join(small_sample_tiers) + "."
        )

    hit_checks: List[dict] = []
    avg_checks: List[dict] = []
    for lower, higher in zip(CONFIDENCE_TIER_ORDER, CONFIDENCE_TIER_ORDER[1:]):
        lower_bucket = by_confidence_tier.get(lower, {})
        higher_bucket = by_confidence_tier.get(higher, {})
        lower_hit = lower_bucket.get("hit_rate")
        higher_hit = higher_bucket.get("hit_rate")
        if lower_hit is not None and higher_hit is not None:
            hit_checks.append({
                "pair": f"{lower}->{higher}",
                "monotonic": higher_hit >= lower_hit,
                "lower": lower_hit,
                "higher": higher_hit,
            })
        lower_avg = lower_bucket.get("avg_forward_return_5d")
        higher_avg = higher_bucket.get("avg_forward_return_5d")
        if lower_avg is not None and higher_avg is not None:
            avg_checks.append({
                "pair": f"{lower}->{higher}",
                "monotonic": higher_avg >= lower_avg,
                "lower": lower_avg,
                "higher": higher_avg,
            })

    hit_monotonic = all(check["monotonic"] for check in hit_checks) if hit_checks else None
    avg_monotonic = all(check["monotonic"] for check in avg_checks) if avg_checks else None

    if hit_monotonic is None or avg_monotonic is None:
        notes.append("Confidence monotonicity is only partially evaluable because some tiers lack 5d outcomes.")
    else:
        if hit_monotonic and avg_monotonic:
            notes.append("Higher confidence tiers outperform lower tiers on both hit rate and average 5d return.")
        else:
            if not hit_monotonic:
                notes.append("Higher confidence tiers do not improve monotonically on hit rate.")
            if not avg_monotonic:
                notes.append("Higher confidence tiers do not improve monotonically on average 5d return.")

    return {
        "tiers": {
            tier: by_confidence_tier.get(tier, {})
            for tier in CONFIDENCE_TIER_ORDER
            if by_confidence_tier.get(tier)
        },
        "monotonicity": {
            "hit_rate_monotonic": hit_monotonic,
            "avg_return_5d_monotonic": avg_monotonic,
            "overall": (
                bool(hit_monotonic) and bool(avg_monotonic)
                if hit_monotonic is not None and avg_monotonic is not None
                else None
            ),
            "hit_rate_checks": hit_checks,
            "avg_return_5d_checks": avg_checks,
        },
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Core attribution
# ---------------------------------------------------------------------------

def _attribute_single(
    rec: dict,
    sorted_snapshots: List[Tuple[date, float]],
) -> AttributedRecord:
    """
    Link one recommendation history record to forward portfolio outcomes.

    A record is "attributable" when:
      1. We can parse a run_date from its run_id, AND
      2. A portfolio snapshot exists within MAX_GAP_DAYS of the run_date, AND
      3. At least one forward snapshot exists.
    """
    run_id: str = rec.get("run_id", "unknown")
    run_date_obj = _parse_date_from_run_id(run_id)

    base_rec = AttributedRecord(
        rec_id=rec.get("rec_id", ""),
        rec_base_id=rec.get("rec_base_id", rec.get("rec_id", "")),
        run_id=run_id,
        run_date=run_date_obj.isoformat() if run_date_obj else "unknown",
        action_level=rec.get("action_level", ""),
        confidence=_safe_int(rec.get("confidence", 100), 100),
        confidence_tier=_confidence_tier(_safe_int(rec.get("confidence", 100), 100)),
        score=_safe_int(rec.get("score", 0), 0),
        raw_score=_safe_int(rec.get("raw_score", rec.get("score", 0)), _safe_int(rec.get("score", 0), 0)),
        impact_area=rec.get("impact_area", ""),
        priority=_safe_int(rec.get("priority", 0), 0),
        degraded_mode=_safe_bool(rec.get("degraded_mode", False), False),
        data_mode=str(rec.get("data_mode", "live")),
        drawdown_regime=str(rec.get("drawdown_regime") or rec.get("regime") or "unknown"),
        portfolio_value_at_t=None,
        forward_return_1d=None,
        forward_return_3d=None,
        forward_return_5d=None,
        forward_return_10d=None,
        mfe=None,
        mae=None,
        attributable=False,
        attribution_note="",
    )

    if run_date_obj is None:
        base_rec.attribution_note = f"cannot parse run_date from run_id={run_id!r}"
        return base_rec

    # Find portfolio value at T
    value_at_t, snap_date = _find_value_at_or_after(
        sorted_snapshots, run_date_obj, max_gap_days=MAX_GAP_DAYS
    )
    if value_at_t is None:
        base_rec.attribution_note = (
            f"no snapshot within {MAX_GAP_DAYS}d of run_date={run_date_obj}"
        )
        return base_rec

    base_rec.portfolio_value_at_t = value_at_t
    base_rec.portfolio_snapshot_date_t = snap_date.isoformat() if snap_date else None

    # Compute forward returns
    forward_returns, forward_dates, mfe, mae = _compute_forward_return_details(
        value_at_t,
        sorted_snapshots,
        run_date_obj,
    )
    base_rec.forward_return_1d = forward_returns.get(1)
    base_rec.forward_return_3d = forward_returns.get(3)
    base_rec.forward_return_5d = forward_returns.get(5)
    base_rec.forward_return_10d = forward_returns.get(10)
    base_rec.forward_snapshot_date_1d = forward_dates.get(1).isoformat() if forward_dates.get(1) else None
    base_rec.forward_snapshot_date_3d = forward_dates.get(3).isoformat() if forward_dates.get(3) else None
    base_rec.forward_snapshot_date_5d = forward_dates.get(5).isoformat() if forward_dates.get(5) else None
    base_rec.forward_snapshot_date_10d = forward_dates.get(10).isoformat() if forward_dates.get(10) else None
    base_rec.mfe = mfe
    base_rec.mae = mae

    # Attributable iff at least one forward horizon has data
    has_any_forward = any(v is not None for v in forward_returns.values())
    if has_any_forward:
        base_rec.attributable = True
        base_rec.attribution_note = "ok"
    else:
        base_rec.attribution_note = (
            f"no forward snapshots after run_date={run_date_obj} "
            f"within {MAX_GAP_DAYS}d of each horizon"
        )

    return base_rec


def attribute_outcomes(
    records: List[dict],
    sorted_snapshots: List[Tuple[date, float]],
) -> List[AttributedRecord]:
    """
    Link each recommendation history record to realized forward portfolio outcomes.

    Parameters
    ----------
    records          : loaded from recommendation_history.jsonl
    sorted_snapshots : [(run_date, total_value)] from load_portfolio_snapshots()

    Returns
    -------
    List of AttributedRecord (one per input record; attributable=False for gaps)
    """
    result = []
    for rec in records:
        try:
            attributed = _attribute_single(rec, sorted_snapshots)
            result.append(attributed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome_attributor: failed to attribute rec %r — %s", rec.get("rec_id"), exc)
    return result


# ---------------------------------------------------------------------------
# Bucket aggregation
# ---------------------------------------------------------------------------

def _accumulate_bucket(bucket: BucketOutcome, ar: AttributedRecord) -> None:
    """Add one attributed record into an aggregation bucket (in-place)."""
    bucket.count += 1
    if not ar.attributable:
        return
    bucket.attributable_count += 1

    # Horizon returns
    for h, attr_name in [(1, "forward_return_1d"), (3, "forward_return_3d"),
                         (5, "forward_return_5d"), (10, "forward_return_10d")]:
        val: Optional[float] = getattr(ar, attr_name)
        if val is not None:
            setattr(bucket, f"sum_return_{h}d", getattr(bucket, f"sum_return_{h}d") + val)
            setattr(bucket, f"count_{h}d", getattr(bucket, f"count_{h}d") + 1)

    # Hit / win / adverse (based on 5d return)
    r5 = ar.forward_return_5d
    if r5 is not None:
        bucket.returns_5d.append(r5)
        if r5 > POSITIVE_RETURN_THRESHOLD:
            bucket.hit_count += 1
        if r5 > STRONG_WIN_THRESHOLD:
            bucket.strong_win_count += 1
        if r5 < ADVERSE_THRESHOLD:
            bucket.adverse_count += 1

    # MFE / MAE
    if ar.mfe is not None:
        bucket.sum_mfe += ar.mfe
        bucket.count_mfe += 1
    if ar.mae is not None:
        bucket.sum_mae += ar.mae
        bucket.count_mae += 1


def _finalize_bucket(bucket: BucketOutcome) -> None:
    """Flag small-sample buckets after aggregation."""
    bucket.small_sample = bucket.attributable_count < SMALL_SAMPLE_WARNING


def _aggregate_by(
    attributed: List[AttributedRecord],
    key_fn,
    label_prefix: str = "",
) -> Dict[str, dict]:
    """
    Aggregate attributed records by an arbitrary key function.

    Returns a dict {key_label -> BucketOutcome.to_dict()}.
    """
    buckets: Dict[str, BucketOutcome] = {}
    for ar in attributed:
        key = key_fn(ar) or "unknown"
        if key not in buckets:
            buckets[key] = BucketOutcome(label=f"{label_prefix}{key}")
        _accumulate_bucket(buckets[key], ar)
    for b in buckets.values():
        _finalize_bucket(b)
    return {k: v.to_dict() for k, v in sorted(buckets.items())}


# ---------------------------------------------------------------------------
# Score quintile analysis (ordered list, not dict)
# ---------------------------------------------------------------------------

_QUINTILE_ORDER = ["0-20", "21-40", "41-60", "61-80", "81-100"]
_DECILE_ORDER = [
    "0-10",
    "11-20",
    "21-30",
    "31-40",
    "41-50",
    "51-60",
    "61-70",
    "71-80",
    "81-90",
    "91-100",
]


def _aggregate_by_score_quintile(
    attributed: List[AttributedRecord],
) -> List[dict]:
    """
    Bucket attributed records into 5 score quintiles (0-20, 21-40, …, 81-100).
    Returns a list ordered from lowest to highest score bucket.
    """
    buckets: Dict[str, BucketOutcome] = {
        q: BucketOutcome(label=q) for q in _QUINTILE_ORDER
    }
    for ar in attributed:
        q = _score_quintile_label(ar.score)
        _accumulate_bucket(buckets[q], ar)
    for b in buckets.values():
        _finalize_bucket(b)
    return [buckets[q].to_dict() for q in _QUINTILE_ORDER if buckets[q].count > 0]


def _aggregate_by_score_decile(
    attributed: List[AttributedRecord],
) -> List[dict]:
    """Bucket attributed records into 10 score deciles."""
    buckets: Dict[str, BucketOutcome] = {
        label: BucketOutcome(label=label) for label in _DECILE_ORDER
    }
    for ar in attributed:
        label = _score_decile_label(ar.score)
        _accumulate_bucket(buckets[label], ar)
    for bucket in buckets.values():
        _finalize_bucket(bucket)
    return [buckets[label].to_dict() for label in _DECILE_ORDER if buckets[label].count > 0]


# ---------------------------------------------------------------------------
# Notable items
# ---------------------------------------------------------------------------

def _notable_items(
    attributed: List[AttributedRecord],
    n: int = 5,
) -> Tuple[List[dict], List[dict]]:
    """
    Return the top-n wins and top-n misses (by 5d forward return) among
    records that have a 5d return.  Advisory only — no ordering of live logic.
    """
    with_5d = [ar for ar in attributed if ar.forward_return_5d is not None]
    if not with_5d:
        return [], []

    sorted_by_return = sorted(with_5d, key=lambda ar: ar.forward_return_5d or 0.0)

    def _fmt(ar: AttributedRecord) -> dict:
        return {
            "run_id": ar.run_id,
            "rec_base_id": ar.rec_base_id,
            "action_level": ar.action_level,
            "confidence": ar.confidence,
            "score": ar.score,
            "impact_area": ar.impact_area,
            "drawdown_regime": ar.drawdown_regime,
            "degraded_mode": ar.degraded_mode,
            "forward_return_5d": ar.forward_return_5d,
            "forward_return_10d": ar.forward_return_10d,
            "mfe": ar.mfe,
            "mae": ar.mae,
        }

    misses = [_fmt(ar) for ar in sorted_by_return[:n]]
    wins = [_fmt(ar) for ar in sorted_by_return[-n:][::-1]]
    return wins, misses


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_outcome_attribution(
    history_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> OutcomeResult:
    """
    Load recommendation history and portfolio snapshots, then attribute
    each recommendation event to its realized forward portfolio outcomes.

    Parameters
    ----------
    history_path : optional path override for recommendation_history.jsonl
    db_path      : optional path override for portfolio.db

    Returns
    -------
    OutcomeResult — always returns a valid object, even with no data.
    Degrades gracefully: missing file / empty history / no snapshots all
    result in an OutcomeResult with coverage_rate=0 and data_quality_notes.
    """
    now_str = datetime.now().isoformat()
    hist_path = history_path or _DEFAULT_HISTORY_PATH
    db = db_path or _DEFAULT_DB_PATH

    thresholds = {
        "positive_return_threshold": POSITIVE_RETURN_THRESHOLD,
        "strong_win_threshold": STRONG_WIN_THRESHOLD,
        "acceptable_loss_threshold": ACCEPTABLE_LOSS_THRESHOLD,
        "adverse_threshold": ADVERSE_THRESHOLD,
        "max_gap_days": MAX_GAP_DAYS,
        "small_sample_warning": SMALL_SAMPLE_WARNING,
        "primary_horizon_days": PRIMARY_HORIZON,
        "note": (
            "hit_rate = fraction of attributed recs where forward_return_5d > "
            f"{POSITIVE_RETURN_THRESHOLD}.  strong_win = > {STRONG_WIN_THRESHOLD}.  "
            f"adverse = < {ADVERSE_THRESHOLD}."
        ),
    }

    base_result = OutcomeResult(
        generated_at=now_str,
        history_path=str(hist_path),
        db_path=str(db),
        outcome_thresholds=thresholds,
    )

    # --- Load history ---
    records = load_history(hist_path)
    if not records:
        base_result.data_quality_notes.append(
            "recommendation_history.jsonl is absent or empty — no records to attribute"
        )
        logger.info("outcome_attributor: history absent or empty")
        return base_result

    # --- Load snapshots ---
    sorted_snapshots = load_portfolio_snapshots(db)
    if not sorted_snapshots:
        base_result.total_records = len(records)
        base_result.unevaluable_records = len(records)
        base_result.data_quality_notes.append(
            f"portfolio.db absent or snapshots table empty — "
            f"{len(records)} records cannot be attributed (no portfolio value time series)"
        )
        logger.info("outcome_attributor: no snapshots available — all records unevaluable")
        return base_result

    logger.info(
        "outcome_attributor: %d history records, %d snapshot dates",
        len(records), len(sorted_snapshots),
    )

    # --- Date range ---
    date_range = _history_date_range(records)

    # --- Attribute ---
    attributed = attribute_outcomes(records, sorted_snapshots)

    # Coverage stats
    n_attr = sum(1 for ar in attributed if ar.attributable)
    n_uneval = len(attributed) - n_attr
    coverage = round(n_attr / len(attributed), 4) if attributed else None

    # Data quality notes
    notes: List[str] = []
    if coverage is not None and coverage < 0.5:
        notes.append(
            f"Low coverage: only {n_attr}/{len(attributed)} ({coverage*100:.0f}%) "
            "records are attributable.  Ensure the portfolio runs frequently enough "
            "that snapshots exist within MAX_GAP_DAYS of each recommendation event."
        )
    if n_attr < SMALL_SAMPLE_WARNING:
        notes.append(
            f"Very few attributable records ({n_attr}).  "
            "Metrics should be interpreted with caution."
        )

    # --- Overall bucket ---
    overall = BucketOutcome(label="overall")
    for ar in attributed:
        _accumulate_bucket(overall, ar)
    _finalize_bucket(overall)
    coverage_by_horizon = {
        "count_1d": overall.count_1d,
        "count_3d": overall.count_3d,
        "count_5d": overall.count_5d,
        "count_10d": overall.count_10d,
    }

    # --- Breakdowns ---
    by_tier = _aggregate_by(attributed, lambda ar: ar.confidence_tier)
    by_mode = _aggregate_by(attributed, lambda ar: "degraded" if ar.degraded_mode else "normal")
    by_regime = _aggregate_by(attributed, lambda ar: ar.drawdown_regime)
    by_action = _aggregate_by(attributed, lambda ar: ar.action_level if ar.action_level else "unknown")
    by_impact_area = _aggregate_by(attributed, lambda ar: ar.impact_area if ar.impact_area else "unknown")
    priority_available = any(
        rec.get("priority") not in (None, "", "n/a") for rec in records if "priority" in rec
    )
    by_priority = (
        _aggregate_by(attributed, lambda ar: _priority_bucket_label(ar.priority))
        if priority_available else {}
    )
    by_quintile = _aggregate_by_score_quintile(attributed)
    by_decile = _aggregate_by_score_decile(attributed)
    aliasing_notes = _summarize_aliasing(attributed)
    sample_quality = _classify_sample_quality(sorted_snapshots, aliasing_notes)
    outcome_data_gaps = _summarize_outcome_gaps(attributed)
    confidence_calibration = _confidence_calibration_summary(by_tier)

    # Notable items
    wins, misses = _notable_items(attributed)

    # Snapshot coverage note
    snap_start = sorted_snapshots[0][0].isoformat()
    snap_end = sorted_snapshots[-1][0].isoformat()
    notes.append(
        f"Portfolio snapshot time series spans {snap_start} → {snap_end} "
        f"({len(sorted_snapshots)} run-dates).  Forward horizons beyond the last "
        "snapshot date will be null."
    )

    if sample_quality == "sparse_weekly":
        notes.append(
            "Sample cadence appears sparse: short horizons often resolve to the same underlying snapshot."
        )
    if any(outcome_data_gaps[f"missing_{h}d_count"] > 0 for h in (1, 3, 5, 10)):
        notes.append(
            "Some forward horizons are unresolved because no snapshot was available within the max 3-day gap."
        )
    notes.extend(note for note in confidence_calibration.get("notes", []) if note not in notes)

    result = OutcomeResult(
        generated_at=now_str,
        history_path=str(hist_path),
        db_path=str(db),
        total_records=len(records),
        attributable_records=n_attr,
        unevaluable_records=n_uneval,
        coverage_rate=coverage,
        date_range=date_range,
        coverage_by_horizon=coverage_by_horizon,
        aliasing_notes=aliasing_notes,
        sample_quality=sample_quality,
        outcome_data_gaps=outcome_data_gaps,
        avg_forward_return_1d=overall.avg_return(1),
        avg_forward_return_3d=overall.avg_return(3),
        avg_forward_return_5d=overall.avg_return(5),
        avg_forward_return_10d=overall.avg_return(10),
        avg_mfe=overall.avg_mfe(),
        avg_mae=overall.avg_mae(),
        hit_rate_overall=overall.hit_rate(),
        strong_win_rate_overall=overall.strong_win_rate(),
        adverse_rate_overall=overall.adverse_rate(),
        by_confidence_tier=by_tier,
        confidence_calibration=confidence_calibration,
        by_degraded_mode=by_mode,
        by_regime=by_regime,
        by_drawdown_regime=by_regime,
        by_action_level=by_action,
        by_impact_area=by_impact_area,
        by_priority_bucket=by_priority,
        by_score_quintile=by_quintile,
        by_score_decile=by_decile,
        notable_wins=wins,
        notable_misses=misses,
        data_quality_notes=notes,
        outcome_thresholds=thresholds,
    )

    logger.info(
        "outcome_attributor: attributed %d/%d records (coverage %.0f%%)",
        n_attr, len(records), (coverage or 0) * 100,
    )
    return result
