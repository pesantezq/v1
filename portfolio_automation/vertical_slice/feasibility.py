"""VS-002 feasibility probe: can a credible risk adjustment be computed at all?

WHY THIS IS A MODULE AND NOT A PARAGRAPH.

"There is not enough data" is the kind of claim that decays. Someone backfills a
price archive six months from now, the sentence in the report stays true-looking,
and the blocker is never revisited. So the blocker is computed from the repository
on every test run instead of asserted once: the day the data arrives, these
numbers move and the tests that pin them fail, which is the notification.

WHAT IT DELIBERATELY DOES NOT DO.

It computes no return, no excess, no beta, no outcome statistic of any kind. It
measures only whether the INPUTS for a credible risk adjustment exist. Computing
a VS-002 result before the VS-002 question is frozen is exactly the contamination
this mission exists to avoid, so the capability is simply absent from this file.

``experimental_noncanonical``.
"""
from __future__ import annotations

import csv
import datetime as dt
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "engineering.vertical_slice.feasibility.v0"
SCHEMA_KIND = "experimental_noncanonical"

EVIDENCE_REL = "outputs/performance/signal_outcomes.csv"
BENCHMARK = "SPY"
HORIZON_DAYS = 7

#: The repository's own gates, reused rather than invented. A risk adjustment
#: that cannot clear the thresholds this codebase already applies to correlation
#: and volatility has no business being published as a benchmark.
MIN_OBS_CORRELATION = 30   # correlation_risk_advisor._MIN_OBSERVATIONS
MIN_OBS_VOLATILITY = 15    # vol_regime_advisor._MIN_OBSERVATIONS

#: A t-interval over fewer clusters than this is not evidence. Two clusters give
#: df=1 and a critical value of 12.7, which produces intervals wider than any
#: effect being measured.
MIN_INDEPENDENT_COHORTS = 10

#: Where a real daily price archive would live, per portfolio_sim/prices.py.
PRICE_ARCHIVE_REL = "outputs/backtest/historical"
FACTOR_FILE_REL = "data/factors/ff_monthly.csv"


@dataclass(frozen=True)
class Feasibility:
    """Measured inputs for a risk adjustment. Every field is counted, not judged."""

    scored_rows: int
    scored_scans: int
    scored_span_days: float
    scored_calendar_dates: int
    full_file_span_days: float
    same_day_scan_clusters: dict[str, Any]
    non_overlapping_cohorts: int
    cohort_timestamps: tuple[str, ...]
    price_points_per_ticker: int
    distinct_price_levels_median: int
    joint_return_observations_min: int
    joint_return_observations_median: int
    scan_gap_hours_min: float
    scan_gap_hours_median: float
    scan_gap_hours_max: float
    sub_two_hour_gaps: int
    price_archive_present: bool
    factor_file_present: bool
    blockers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def risk_adjustment_credible(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
            "risk_adjustment_credible": self.risk_adjustment_credible,
            "blockers": list(self.blockers),
            "scored_population": {
                "rows": self.scored_rows, "scans": self.scored_scans,
                "span_days": round(self.scored_span_days, 2),
                "distinct_calendar_dates": self.scored_calendar_dates,
                "full_file_span_days": round(self.full_file_span_days, 2),
                "same_day_scan_clusters": self.same_day_scan_clusters,
            },
            "independent_sampling": {
                "non_overlapping_cohorts": self.non_overlapping_cohorts,
                "cohort_timestamps": list(self.cohort_timestamps),
                "minimum_required": MIN_INDEPENDENT_COHORTS,
            },
            "risk_estimation_inputs": {
                "price_points_per_ticker": self.price_points_per_ticker,
                "distinct_price_levels_median": self.distinct_price_levels_median,
                "joint_return_observations_min": self.joint_return_observations_min,
                "joint_return_observations_median": self.joint_return_observations_median,
                "repo_gate_correlation": MIN_OBS_CORRELATION,
                "repo_gate_volatility": MIN_OBS_VOLATILITY,
                "scan_gap_hours": {
                    "min": round(self.scan_gap_hours_min, 3),
                    "median": round(self.scan_gap_hours_median, 2),
                    "max": round(self.scan_gap_hours_max, 2),
                    "under_two_hours": self.sub_two_hour_gaps,
                },
            },
            "durable_price_data": {
                "price_archive_present": self.price_archive_present,
                "price_archive_path": PRICE_ARCHIVE_REL,
                "factor_file_present": self.factor_file_present,
                "factor_file_path": FACTOR_FILE_REL,
            },
        }


def _ts(raw: str) -> dt.datetime:
    return dt.datetime.fromisoformat(raw.strip())


def assess(repo_root: Path) -> Feasibility:
    root = Path(repo_root)
    with (root / EVIDENCE_REL).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    matured = [r for r in rows if r["outcome_return_7d"].strip()]
    scans = sorted({r["signal_time"] for r in matured})
    all_scans = sorted({r["signal_time"] for r in rows})
    span = (_ts(scans[-1]) - _ts(scans[0])).total_seconds() / 86400.0
    full_span = (_ts(all_scans[-1]) - _ts(all_scans[0])).total_seconds() / 86400.0

    by_day: dict[dt.date, list[dt.datetime]] = defaultdict(list)
    for s in scans:
        by_day[_ts(s).date()].append(_ts(s))
    clusters = {
        str(day): {"scans": len(times),
                   "spread_minutes": round(
                       (max(times) - min(times)).total_seconds() / 60.0, 1)}
        for day, times in sorted(by_day.items()) if len(times) > 1}

    # Greedy non-overlapping selection: a cohort may start only once the previous
    # cohort's 7-day outcome window has closed.
    cohorts: list[str] = []
    for s in scans:
        if not cohorts or (_ts(s) - _ts(cohorts[-1])).days >= HORIZON_DAYS:
            cohorts.append(s)

    series: dict[str, list[tuple[dt.datetime, float]]] = defaultdict(list)
    for r in rows:
        series[r["ticker"]].append((_ts(r["signal_time"]),
                                    float(r["price_at_signal"])))
    for ticker in series:
        series[ticker].sort()

    def returns(points):
        return [(points[i][1] - points[i - 1][1]) / points[i - 1][1]
                for i in range(1, len(points))]

    bench = series[BENCHMARK]
    bench_returns = returns(bench)
    joint = {}
    for ticker, points in series.items():
        if ticker == BENCHMARK:
            continue
        joint[ticker] = sum(
            1 for a, b in zip(returns(points), bench_returns)
            if a != 0.0 and b != 0.0)

    gaps = [(bench[i][0] - bench[i - 1][0]).total_seconds() / 3600.0
            for i in range(1, len(bench))]
    levels = sorted(len({p for _, p in v}) for v in series.values())

    blockers = []
    if len(cohorts) < MIN_INDEPENDENT_COHORTS:
        blockers.append(
            f"only {len(cohorts)} non-overlapping {HORIZON_DAYS}-day cohort(s) "
            f"exist in a {span:.1f}-day scored window; {MIN_INDEPENDENT_COHORTS} "
            "is the minimum for an interval estimate that is not wider than the "
            "effect")
    if min(joint.values()) < MIN_OBS_VOLATILITY:
        blockers.append(
            f"risk estimation has {min(joint.values())}-{max(joint.values())} "
            f"usable joint return observations per ticker, below this "
            f"repository's own volatility gate of {MIN_OBS_VOLATILITY} and far "
            f"below its correlation gate of {MIN_OBS_CORRELATION}")
    if not (root / PRICE_ARCHIVE_REL).is_dir():
        blockers.append(
            f"no durable price history: {PRICE_ARCHIVE_REL} does not exist, so "
            "no pre-signal estimation window can be constructed")
    if max(gaps) / max(min(gaps), 1e-9) > 100:
        blockers.append(
            f"observation intervals are not comparable: gaps range "
            f"{min(gaps):.2f}h to {max(gaps):.2f}h, so consecutive 'returns' are "
            "not identically scaled and a beta over them is biased toward zero")

    return Feasibility(
        scored_rows=len(matured), scored_scans=len(scans),
        scored_span_days=span, scored_calendar_dates=len(by_day),
        full_file_span_days=full_span, same_day_scan_clusters=clusters,
        non_overlapping_cohorts=len(cohorts), cohort_timestamps=tuple(cohorts),
        price_points_per_ticker=len(bench),
        distinct_price_levels_median=int(statistics.median(levels)),
        joint_return_observations_min=min(joint.values()),
        joint_return_observations_median=int(statistics.median(joint.values())),
        scan_gap_hours_min=min(gaps),
        scan_gap_hours_median=statistics.median(gaps),
        scan_gap_hours_max=max(gaps),
        sub_two_hour_gaps=sum(1 for g in gaps if g < 2.0),
        price_archive_present=(root / PRICE_ARCHIVE_REL).is_dir(),
        factor_file_present=(root / FACTOR_FILE_REL).is_file(),
        blockers=tuple(blockers),
    )
