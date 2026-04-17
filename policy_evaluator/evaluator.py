"""
Recommendation quality evaluator.

Reads recommendation_history.jsonl and computes five metric families:

1. hit_rate_by_regime      — resolution rate grouped by drawdown_regime
2. hit_rate_by_mode        — resolution rate in normal vs degraded data mode
3. confidence_calibration  — do high-confidence recs resolve faster?
4. recommendation_stability — churn / persistence run-over-run
5. best_vs_recommended_gap — score headroom and confidence discounting

"Resolution" definition
-----------------------
A recommendation (identified by its stable rec_base_id) is considered
**resolved** at run T if it does NOT appear in run T+1.  This is a proxy
for "the underlying condition improved or the investor acted."  The metric
is meaningful only when runs are reasonably frequent (daily / weekly).

Sparse-history handling
-----------------------
All metrics degrade gracefully:
  - 0 records  → empty result with total_records=0
  - 1 run      → stability / hit-rate metrics are null (need ≥ 2 runs)
  - 1 regime   → by-regime dict has a single key (still valid)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from policy_evaluator.infrastructure import (
    DEFAULT_HISTORY_PATH,
    load_recommendation_history,
    parse_timestamp,
)

logger = logging.getLogger("policy_evaluator.evaluator")

_DEFAULT_HISTORY_PATH = DEFAULT_HISTORY_PATH

# Action levels in ascending urgency — used for gap analysis.
_ACTION_LEVEL_ORDER = ["FYI", "Monitor", "Recommended", "Action Required"]
_RECOMMENDED_THRESHOLD_SCORE = 50
_ACTION_REQUIRED_THRESHOLD_SCORE = 75


# ---------------------------------------------------------------------------
# Dataclasses for structured output
# ---------------------------------------------------------------------------

@dataclass
class RegimeBucket:
    regime: str
    total: int = 0
    resolved: int = 0

    @property
    def hit_rate(self) -> Optional[float]:
        return round(self.resolved / self.total, 4) if self.total > 0 else None

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "total": self.total,
            "resolved": self.resolved,
            "hit_rate": self.hit_rate,
        }


@dataclass
class ModeBucket:
    mode: str          # "normal" or "degraded"
    total: int = 0
    resolved: int = 0

    @property
    def hit_rate(self) -> Optional[float]:
        return round(self.resolved / self.total, 4) if self.total > 0 else None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "total": self.total,
            "resolved": self.resolved,
            "hit_rate": self.hit_rate,
        }


@dataclass
class ConfidenceTier:
    tier: str          # "low", "medium", "high"
    range_label: str   # e.g. "0-50"
    count: int = 0
    avg_score: float = 0.0
    avg_raw_score: float = 0.0
    resolution_rate: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "range": self.range_label,
            "count": self.count,
            "avg_score": round(self.avg_score, 2),
            "avg_raw_score": round(self.avg_raw_score, 2),
            "resolution_rate": round(self.resolution_rate, 4) if self.resolution_rate is not None else None,
        }


@dataclass
class RunStabilityRow:
    run_id: str
    total: int
    new_count: int
    carried_over: int
    churn_rate: Optional[float]   # null for first run

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "total": self.total,
            "new_count": self.new_count,
            "carried_over": self.carried_over,
            "churn_rate": round(self.churn_rate, 4) if self.churn_rate is not None else None,
        }


@dataclass
class RunGapRow:
    run_id: str
    best_score: int
    best_raw_score: int
    max_confidence_discount: int     # best_raw_score - best_score
    has_action_required: bool
    min_action_required_score: Optional[int]
    min_recommended_score: Optional[int]
    gap_best_vs_action_required_threshold: int  # best_score - 75; negative = best not urgent

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "best_score": self.best_score,
            "best_raw_score": self.best_raw_score,
            "max_confidence_discount": self.max_confidence_discount,
            "has_action_required": self.has_action_required,
            "min_action_required_score": self.min_action_required_score,
            "min_recommended_score": self.min_recommended_score,
            "gap_best_vs_action_required_threshold": self.gap_best_vs_action_required_threshold,
        }


@dataclass
class EvaluationResult:
    generated_at: str
    history_path: str
    total_records: int
    total_runs: int
    date_range: Dict[str, Optional[str]]

    # 1. Hit rate by regime
    hit_rate_by_regime: Dict[str, dict] = field(default_factory=dict)

    # 2. Hit rate by data mode
    hit_rate_by_mode: Dict[str, dict] = field(default_factory=dict)

    # 3. Confidence calibration
    confidence_calibration: Dict[str, object] = field(default_factory=dict)

    # 4. Recommendation stability
    recommendation_stability: Dict[str, object] = field(default_factory=dict)

    # 5. Best-vs-recommended gap
    best_vs_recommended_gap: Dict[str, object] = field(default_factory=dict)

    # Aggregates for the memo
    action_level_distribution: Dict[str, int] = field(default_factory=dict)
    impact_area_breakdown: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "history_path": self.history_path,
            "total_records": self.total_records,
            "total_runs": self.total_runs,
            "date_range": self.date_range,
            "hit_rate_by_regime": self.hit_rate_by_regime,
            "hit_rate_by_mode": self.hit_rate_by_mode,
            "confidence_calibration": self.confidence_calibration,
            "recommendation_stability": self.recommendation_stability,
            "best_vs_recommended_gap": self.best_vs_recommended_gap,
            "action_level_distribution": self.action_level_distribution,
            "impact_area_breakdown": self.impact_area_breakdown,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _group_by_run(records: List[dict]) -> List[Tuple[str, List[dict]]]:
    """
    Return [(run_id, [records]), ...] ordered by the earliest timestamp
    within each run_id.

    Uses run_id as the stable key so that records written in the same
    execution are grouped even if the append was split across midnight.
    """
    by_run: Dict[str, List[dict]] = defaultdict(list)
    for rec in records:
        rid = rec.get("run_id", "unknown")
        by_run[rid].append(rec)

    def _run_sort_key(item: Tuple[str, List[dict]]) -> tuple[datetime, str]:
        run_id, run_records = item
        timestamps = [
            parsed for parsed in (parse_timestamp(rec.get("timestamp")) for rec in run_records)
            if parsed is not None
        ]
        first_seen = min(timestamps) if timestamps else datetime.max
        return first_seen, run_id

    ordered_runs = sorted(by_run.items(), key=_run_sort_key)
    return [(run_id, run_records) for run_id, run_records in ordered_runs]


def _base_ids(run_records: List[dict]) -> set:
    return {r.get("rec_base_id", r.get("rec_id", "")) for r in run_records}


def _confidence_tier(confidence: int) -> Tuple[str, str]:
    """Map confidence 0-100 to (tier_name, range_label)."""
    if confidence <= 50:
        return "low", "0-50"
    elif confidence <= 80:
        return "medium", "51-80"
    return "high", "81-100"


def _tier_order(tier: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(tier, 0)


# ---------------------------------------------------------------------------
# Metric computers
# ---------------------------------------------------------------------------

def _compute_hit_rate_by_regime(
    runs: List[Tuple[str, List[dict]]],
) -> Dict[str, dict]:
    """
    For each consecutive pair of runs, tag each recommendation in run[i]
    as resolved (base_id absent from run[i+1]) and bucket by regime.
    """
    buckets: Dict[str, RegimeBucket] = {}

    for i in range(len(runs) - 1):
        _, curr_recs = runs[i]
        _, next_recs = runs[i + 1]
        next_bases = _base_ids(next_recs)

        for rec in curr_recs:
            regime = rec.get("regime") or rec.get("drawdown_regime") or "unknown"
            if regime not in buckets:
                buckets[regime] = RegimeBucket(regime=regime)
            b = buckets[regime]
            b.total += 1
            base = rec.get("rec_base_id", rec.get("rec_id", ""))
            if base not in next_bases:
                b.resolved += 1

    return {k: v.to_dict() for k, v in buckets.items()}


def _compute_hit_rate_by_mode(
    runs: List[Tuple[str, List[dict]]],
) -> Dict[str, dict]:
    """Same resolution logic, grouped by degraded_mode."""
    normal = ModeBucket(mode="normal")
    degraded = ModeBucket(mode="degraded")

    for i in range(len(runs) - 1):
        _, curr_recs = runs[i]
        _, next_recs = runs[i + 1]
        next_bases = _base_ids(next_recs)

        for rec in curr_recs:
            is_degraded = bool(rec.get("degraded_mode", False))
            b = degraded if is_degraded else normal
            b.total += 1
            base = rec.get("rec_base_id", rec.get("rec_id", ""))
            if base not in next_bases:
                b.resolved += 1

    result = {}
    if normal.total > 0:
        result["normal"] = normal.to_dict()
    if degraded.total > 0:
        result["degraded"] = degraded.to_dict()
    return result


def _compute_confidence_calibration(
    records: List[dict],
    runs: List[Tuple[str, List[dict]]],
) -> dict:
    """
    Group records by confidence tier and compute:
      - count, avg_score, avg_raw_score per tier
      - resolution_rate per tier (using consecutive-run logic)
    Then derive a calibration_score = 1.0 if tiers are monotonically ordered
    by resolution_rate (high > medium > low), 0.0 if reversed, 0.5 if flat.
    """
    tier_counts: Dict[str, int] = defaultdict(int)
    tier_scores: Dict[str, List[int]] = defaultdict(list)
    tier_raw: Dict[str, List[int]] = defaultdict(list)

    for rec in records:
        conf = int(rec.get("confidence", 100))
        tier, _ = _confidence_tier(conf)
        tier_counts[tier] += 1
        tier_scores[tier].append(int(rec.get("score", 0)))
        tier_raw[tier].append(int(rec.get("raw_score", rec.get("score", 0))))

    # Resolution rates per tier using consecutive-run analysis
    tier_resolved: Dict[str, int] = defaultdict(int)
    tier_total_res: Dict[str, int] = defaultdict(int)

    for i in range(len(runs) - 1):
        _, curr_recs = runs[i]
        _, next_recs = runs[i + 1]
        next_bases = _base_ids(next_recs)

        for rec in curr_recs:
            conf = int(rec.get("confidence", 100))
            tier, _ = _confidence_tier(conf)
            tier_total_res[tier] += 1
            base = rec.get("rec_base_id", rec.get("rec_id", ""))
            if base not in next_bases:
                tier_resolved[tier] += 1

    tiers_out = {}
    all_tier_names = {"low", "medium", "high"} | set(tier_counts.keys())
    for tier in ("low", "medium", "high"):
        if tier not in all_tier_names:
            continue
        rng = {"low": "0-50", "medium": "51-80", "high": "81-100"}[tier]
        count = tier_counts.get(tier, 0)
        scores = tier_scores.get(tier, [])
        raws = tier_raw.get(tier, [])
        res_total = tier_total_res.get(tier, 0)
        res_count = tier_resolved.get(tier, 0)
        obj = ConfidenceTier(
            tier=tier,
            range_label=rng,
            count=count,
            avg_score=sum(scores) / len(scores) if scores else 0.0,
            avg_raw_score=sum(raws) / len(raws) if raws else 0.0,
            resolution_rate=(res_count / res_total) if res_total > 0 else None,
        )
        tiers_out[tier] = obj

    # Calibration score: monotonicity test on resolution rates
    calibration_score: Optional[float] = None
    rates = [
        tiers_out[t].resolution_rate
        for t in ("low", "medium", "high")
        if t in tiers_out and tiers_out[t].resolution_rate is not None
    ]
    if len(rates) >= 2:
        # Count how many adjacent pairs are in the "right" direction (higher tier → higher rate)
        correct = sum(1 for a, b in zip(rates, rates[1:]) if b >= a)
        total_pairs = len(rates) - 1
        calibration_score = round(correct / total_pairs, 4)

    return {
        "tiers": {t: v.to_dict() for t, v in tiers_out.items()},
        "calibration_score": calibration_score,
        "note": (
            "calibration_score=1.0 means higher-confidence recs resolve faster (well-calibrated); "
            "0.0 means the opposite; null means insufficient data."
        ),
    }


def _compute_stability(runs: List[Tuple[str, List[dict]]]) -> dict:
    """
    For each run, report how many recommendations are new vs carried over
    from the previous run (using rec_base_id).
    """
    rows: List[RunStabilityRow] = []
    prev_bases: Optional[set] = None

    for run_id, recs in runs:
        curr_bases = _base_ids(recs)
        total = len(recs)

        if prev_bases is None:
            row = RunStabilityRow(
                run_id=run_id,
                total=total,
                new_count=total,
                carried_over=0,
                churn_rate=None,  # first run — no prior baseline
            )
        else:
            carried = len(curr_bases & prev_bases)
            new_c = total - carried
            churn = round(new_c / total, 4) if total > 0 else 0.0
            row = RunStabilityRow(
                run_id=run_id,
                total=total,
                new_count=new_c,
                carried_over=carried,
                churn_rate=churn,
            )
        rows.append(row)
        prev_bases = curr_bases

    churn_rates = [r.churn_rate for r in rows if r.churn_rate is not None]
    avg_churn = round(sum(churn_rates) / len(churn_rates), 4) if churn_rates else None
    avg_stability = round(1.0 - avg_churn, 4) if avg_churn is not None else None

    return {
        "avg_churn_rate": avg_churn,
        "avg_stability": avg_stability,
        "per_run": [r.to_dict() for r in rows],
        "note": (
            "churn_rate = fraction of recommendations in this run that did not "
            "appear in the previous run.  Low churn = stable, persistent issues."
        ),
    }


def _compute_gap(runs: List[Tuple[str, List[dict]]]) -> dict:
    """
    For each run, compute:
      - best_score             : highest final_score in the run
      - best_raw_score         : highest raw_score (before confidence penalty)
      - max_confidence_discount: difference (raw - final) on the best item
      - gap_best_vs_action_required_threshold: best_score - 75
        positive = run has urgent items
        zero / negative = no urgent items
    """
    rows: List[RunGapRow] = []

    for run_id, recs in runs:
        if not recs:
            continue
        scores = [int(r.get("score", 0)) for r in recs]
        raw_scores = [int(r.get("raw_score", r.get("score", 0))) for r in recs]
        action_levels = [r.get("action_level", "") for r in recs]

        best_score = max(scores)
        best_raw = max(raw_scores)

        ar_scores = [s for s, lvl in zip(scores, action_levels) if lvl == "Action Required"]
        rec_scores = [s for s, lvl in zip(scores, action_levels) if lvl in ("Action Required", "Recommended")]

        row = RunGapRow(
            run_id=run_id,
            best_score=best_score,
            best_raw_score=best_raw,
            max_confidence_discount=best_raw - best_score,
            has_action_required=bool(ar_scores),
            min_action_required_score=min(ar_scores) if ar_scores else None,
            min_recommended_score=min(rec_scores) if rec_scores else None,
            gap_best_vs_action_required_threshold=best_score - _ACTION_REQUIRED_THRESHOLD_SCORE,
        )
        rows.append(row)

    gaps = [r.gap_best_vs_action_required_threshold for r in rows]
    discounts = [r.max_confidence_discount for r in rows]

    return {
        "avg_gap_vs_action_required_threshold": round(sum(gaps) / len(gaps), 2) if gaps else None,
        "max_gap_vs_action_required_threshold": max(gaps) if gaps else None,
        "avg_confidence_discount": round(sum(discounts) / len(discounts), 2) if discounts else None,
        "max_confidence_discount": max(discounts) if discounts else None,
        "per_run": [r.to_dict() for r in rows],
        "note": (
            "gap_best_vs_action_required_threshold > 0 means the run had at least one ACTION_REQUIRED item. "
            "confidence_discount = raw_score - final_score on the best item; large values indicate "
            "data-quality penalties suppressing urgency."
        ),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate_history(
    history_path: Optional[Path] = None,
) -> EvaluationResult:
    """
    Load recommendation_history.jsonl and compute all metric families.

    Parameters
    ----------
    history_path : optional override (used in tests)

    Returns
    -------
    EvaluationResult with all metric dicts populated.
    Gracefully handles 0 records, 1 run, or missing file.
    """
    from datetime import datetime

    path = history_path or _DEFAULT_HISTORY_PATH
    records = load_recommendation_history(path)

    now_str = datetime.now().isoformat()

    if not records:
        logger.info("policy_evaluator: history file empty or absent — returning empty result")
        return EvaluationResult(
            generated_at=now_str,
            history_path=str(path),
            total_records=0,
            total_runs=0,
            date_range={"first": None, "last": None},
        )

    runs = _group_by_run(records)
    timestamps = [parse_timestamp(r.get("timestamp")) for r in records if r.get("timestamp")]
    timestamps = [ts for ts in timestamps if ts is not None]
    date_first = min(timestamps).date().isoformat() if timestamps else None
    date_last = max(timestamps).date().isoformat() if timestamps else None

    # Action-level distribution (all records)
    action_dist: Dict[str, int] = defaultdict(int)
    for r in records:
        action_dist[r.get("action_level", "unknown")] += 1

    # Impact-area distribution
    area_dist: Dict[str, int] = defaultdict(int)
    for r in records:
        area_dist[r.get("impact_area", "unknown")] += 1

    result = EvaluationResult(
        generated_at=now_str,
        history_path=str(path),
        total_records=len(records),
        total_runs=len(runs),
        date_range={"first": date_first, "last": date_last},
        action_level_distribution=dict(action_dist),
        impact_area_breakdown=dict(area_dist),
    )

    if len(runs) >= 2:
        result.hit_rate_by_regime = _compute_hit_rate_by_regime(runs)
        result.hit_rate_by_mode = _compute_hit_rate_by_mode(runs)
        result.confidence_calibration = _compute_confidence_calibration(records, runs)
    else:
        logger.info(
            "policy_evaluator: only %d run(s) in history — "
            "hit-rate and calibration require ≥ 2 runs",
            len(runs),
        )
        result.confidence_calibration = _compute_confidence_calibration(records, runs)

    result.recommendation_stability = _compute_stability(runs)
    result.best_vs_recommended_gap = _compute_gap(runs)

    logger.info(
        "policy_evaluator: evaluated %d records across %d runs (%s → %s)",
        len(records), len(runs), date_first, date_last,
    )
    return result
