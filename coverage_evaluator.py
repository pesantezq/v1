"""
Coverage Evaluator
==================
Computes outcome metrics for promoted market-coverage candidates by tracking
their symbol-level price performance across subsequent scan runs.

Attribution method: symbol-level price proxy
============================================
For each symbol's FIRST appearance in coverage_history.jsonl (the "entry"):
  entry_price = price at first promotion

For each SUBSEQUENT appearance of the same symbol (an "observation"):
  forward_return = (observation_price - entry_price) / entry_price

Observations are included up to MAX_TRACK_DAYS (default 30) calendar days
after entry.  The observation CLOSEST to each horizon (1d, 3d, 5d, 10d) is
used for horizon-specific return reporting.

Why symbol-level (not portfolio proxy)?
  Promoted candidates are specific stock picks from the broad-universe scan.
  Unlike advisory recommendations (emergency fund, drift), these have a clear
  ticker to evaluate against.  Using the stock's own price is more honest than
  using portfolio total_value.

Limitation
  Price observations only exist for days when the symbol was again promoted and
  scanned.  Gaps in coverage will leave some horizons unattributable.
  Coverage statistics are included in every report.

Exit quality
  exit_quality = latest_return / mfe  (if mfe > 0 and latest_return is not None)
  Represents how much of the peak gain was retained.
  Values near 1.0 are good (peak preserved).  Negative values mean a gain
  turned into a loss.

Score bands (analogous to confidence tiers in policy_evaluator)
  low    0 – 40
  medium 41 – 70
  high   71 – 100

Outcome thresholds (all explicit constants)
  HIT_THRESHOLD      =  0.00   (any gain = hit)
  STRONG_WIN         =  0.02   (+2% = strong win)
  ADVERSE            = -0.02   (-2% = adverse)
  SMALL_SAMPLE       =  5      (buckets below this are flagged)
  MAX_TRACK_DAYS     = 30      (days after entry to track)
  HORIZONS           = (1, 3, 5, 10)
  PRIMARY_HORIZON    = 5
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import mean
from typing import Dict, List, Optional, Tuple

from coverage_tracker import load_coverage_history, _parse_date_from_run_id

logger = logging.getLogger("portfolio_automation.coverage_evaluator")

# ---------------------------------------------------------------------------
# Constants (all explicit)
# ---------------------------------------------------------------------------

HIT_THRESHOLD: float = 0.00         # any gain = hit at primary horizon
STRONG_WIN_THRESHOLD: float = 0.02  # +2%
ADVERSE_THRESHOLD: float = -0.02    # -2%
SMALL_SAMPLE: int = 5
MAX_TRACK_DAYS: int = 30
HORIZONS: Tuple[int, ...] = (1, 3, 5, 10)
PRIMARY_HORIZON: int = 5
SCORE_BANDS: Tuple[Tuple[str, int, int], ...] = (
    ("low", 0, 40),
    ("medium", 41, 70),
    ("high", 71, 100),
)

_DEFAULT_HISTORY_PATH_STR = "outputs/policy/coverage_history.jsonl"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PriceObservation:
    """A single price observation for a symbol after its entry."""
    run_id: str
    obs_date: date
    price: float
    forward_return: float   # (price - entry_price) / entry_price
    hold_days: int          # calendar days since entry_date


@dataclass
class CoverageOutcome:
    """
    Outcome metrics for a single promoted candidate entry.

    One entry = one promotion event (first time the symbol appeared in the
    current continuous tracking window).
    """
    symbol: str
    entry_run_id: str
    entry_date: date
    entry_price: float
    label: str              # compounder | momentum | watchlist
    score: float            # 0–100 composite score at entry
    events: List[str]       # EventType values that fired at entry
    drawdown_regime: str    # portfolio regime at entry
    action_bucket: str      # portfolio_context.action_bucket (may be "")
    observations: List[PriceObservation] = field(default_factory=list)

    # Computed (set by _compute_derived)
    forward_return_1d: Optional[float] = None
    forward_return_3d: Optional[float] = None
    forward_return_5d: Optional[float] = None
    forward_return_10d: Optional[float] = None
    mfe: Optional[float] = None           # max favorable excursion (>= 0)
    mae: Optional[float] = None           # max adverse excursion (<= 0)
    latest_return: Optional[float] = None # return at most-recent observation
    exit_quality: Optional[float] = None  # latest_return / mfe if mfe > 0
    hit: Optional[bool] = None            # forward_return_5d > HIT_THRESHOLD
    attributable: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry_run_id": self.entry_run_id,
            "entry_date": self.entry_date.isoformat(),
            "entry_price": self.entry_price,
            "label": self.label,
            "score": self.score,
            "events": self.events,
            "drawdown_regime": self.drawdown_regime,
            "action_bucket": self.action_bucket,
            "forward_return_1d": self.forward_return_1d,
            "forward_return_3d": self.forward_return_3d,
            "forward_return_5d": self.forward_return_5d,
            "forward_return_10d": self.forward_return_10d,
            "mfe": self.mfe,
            "mae": self.mae,
            "latest_return": self.latest_return,
            "exit_quality": self.exit_quality,
            "hit": self.hit,
            "attributable": self.attributable,
            "observation_count": len(self.observations),
        }


@dataclass
class Bucket:
    """Aggregated outcome metrics for a grouping dimension."""
    name: str
    count: int = 0              # total entries in this bucket
    attributable: int = 0       # entries with at least one observation
    hit_count: int = 0          # attributable hits (return > 0)
    strong_win_count: int = 0   # return >= STRONG_WIN_THRESHOLD
    adverse_count: int = 0      # return <= ADVERSE_THRESHOLD
    returns: List[float] = field(default_factory=list)    # 5d returns
    mfe_values: List[float] = field(default_factory=list)
    mae_values: List[float] = field(default_factory=list)
    eq_values: List[float] = field(default_factory=list)  # exit quality
    small_sample: bool = False

    @property
    def hit_rate(self) -> Optional[float]:
        if not self.returns:
            return None
        return round(self.hit_count / len(self.returns), 4)

    @property
    def avg_return(self) -> Optional[float]:
        if not self.returns:
            return None
        return round(mean(self.returns), 6)

    @property
    def avg_mfe(self) -> Optional[float]:
        if not self.mfe_values:
            return None
        return round(mean(self.mfe_values), 6)

    @property
    def avg_mae(self) -> Optional[float]:
        if not self.mae_values:
            return None
        return round(mean(self.mae_values), 6)

    @property
    def avg_exit_quality(self) -> Optional[float]:
        if not self.eq_values:
            return None
        return round(mean(self.eq_values), 4)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "attributable": self.attributable,
            "hit_count": self.hit_count,
            "strong_win_count": self.strong_win_count,
            "adverse_count": self.adverse_count,
            "hit_rate": self.hit_rate,
            "avg_return_5d": self.avg_return,
            "avg_mfe": self.avg_mfe,
            "avg_mae": self.avg_mae,
            "avg_exit_quality": self.avg_exit_quality,
            "small_sample": self.small_sample,
        }


@dataclass
class HorizonStats:
    """Average return at a specific hold horizon across all attributable entries."""
    horizon_days: int
    count: int = 0
    returns: List[float] = field(default_factory=list)

    @property
    def avg_return(self) -> Optional[float]:
        if not self.returns:
            return None
        return round(mean(self.returns), 6)

    @property
    def hit_rate(self) -> Optional[float]:
        if not self.returns:
            return None
        hits = sum(1 for r in self.returns if r > HIT_THRESHOLD)
        return round(hits / len(self.returns), 4)

    def to_dict(self) -> dict:
        return {
            "horizon_days": self.horizon_days,
            "count": self.count,
            "avg_return": self.avg_return,
            "hit_rate": self.hit_rate,
        }


@dataclass
class CoverageEvalResult:
    """
    Full evaluation output for all promoted market-coverage candidates.
    """
    total_entries: int
    attributable_entries: int
    coverage_rate: float                    # attributable / total
    by_label: List[Bucket]
    by_score_band: List[Bucket]
    by_event_type: List[Bucket]
    by_regime: List[Bucket]
    by_action_bucket: List[Bucket]
    by_hold_duration: List[HorizonStats]
    notable_wins: List[dict]                # top 5 by forward_return_5d
    notable_misses: List[dict]              # worst 5 by forward_return_5d
    data_quality_notes: List[str]
    generated_at: str

    def to_dict(self) -> dict:
        return {
            "total_entries": self.total_entries,
            "attributable_entries": self.attributable_entries,
            "coverage_rate": self.coverage_rate,
            "by_label": [b.to_dict() for b in self.by_label],
            "by_score_band": [b.to_dict() for b in self.by_score_band],
            "by_event_type": [b.to_dict() for b in self.by_event_type],
            "by_regime": [b.to_dict() for b in self.by_regime],
            "by_action_bucket": [b.to_dict() for b in self.by_action_bucket],
            "by_hold_duration": [h.to_dict() for h in self.by_hold_duration],
            "notable_wins": self.notable_wins,
            "notable_misses": self.notable_misses,
            "data_quality_notes": self.data_quality_notes,
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_coverage(
    history_path=None,
) -> CoverageEvalResult:
    """
    Evaluate promoted candidate outcomes from coverage_history.jsonl.

    Args:
        history_path: Override the default history file path.

    Returns:
        CoverageEvalResult — always returns a valid object; never raises.
        Includes data_quality_notes when data is missing or coverage is low.
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    records = load_coverage_history(history_path)

    if not records:
        return _empty_result(
            now_iso,
            notes=["No coverage history records found.  Run market coverage scans first."],
        )

    # Group records into CoverageOutcome entries
    outcomes = _build_outcomes(records)

    if not outcomes:
        return _empty_result(
            now_iso,
            notes=["Coverage history loaded but produced no trackable entries."],
        )

    total = len(outcomes)
    attributable = sum(1 for o in outcomes if o.attributable)
    coverage_rate = round(attributable / total, 4) if total > 0 else 0.0

    notes: List[str] = []
    if coverage_rate < 0.3:
        notes.append(
            f"Low coverage: only {attributable}/{total} entries have observations "
            f"({coverage_rate*100:.0f}%).  More scan runs needed to populate returns."
        )

    by_label = _aggregate_by_dimension(outcomes, lambda o: o.label)
    by_score_band = _aggregate_by_score_band(outcomes)
    by_event_type = _aggregate_by_event_type(outcomes)
    by_regime = _aggregate_by_dimension(outcomes, lambda o: o.drawdown_regime or "unknown")
    by_action_bucket = _aggregate_by_dimension(
        outcomes,
        lambda o: o.action_bucket if o.action_bucket else "unclassified",
    )
    by_hold_duration = _aggregate_by_hold_duration(outcomes)
    notable_wins, notable_misses = _notable_items(outcomes, n=5)

    return CoverageEvalResult(
        total_entries=total,
        attributable_entries=attributable,
        coverage_rate=coverage_rate,
        by_label=by_label,
        by_score_band=by_score_band,
        by_event_type=by_event_type,
        by_regime=by_regime,
        by_action_bucket=by_action_bucket,
        by_hold_duration=by_hold_duration,
        notable_wins=notable_wins,
        notable_misses=notable_misses,
        data_quality_notes=notes,
        generated_at=now_iso,
    )


# ---------------------------------------------------------------------------
# Internal: build outcomes from raw JSONL records
# ---------------------------------------------------------------------------

def _build_outcomes(records: List[dict]) -> List[CoverageOutcome]:
    """
    Group records by symbol and build CoverageOutcome objects.

    Strategy: treat each symbol's first appearance as the entry.  All later
    appearances within MAX_TRACK_DAYS are observations.  A symbol that
    re-appears after a gap > MAX_TRACK_DAYS starts a NEW entry.
    """
    # Sort all records by date ascending (stable)
    valid_records: List[dict] = []
    for rec in records:
        d = _parse_date_safe(rec.get("date", ""))
        if d is not None:
            valid_records.append({**rec, "_date": d})

    valid_records.sort(key=lambda r: r["_date"])

    outcomes: List[CoverageOutcome] = []
    # Track the most recent entry per symbol
    # Key: symbol → CoverageOutcome (current open entry)
    open_entries: Dict[str, CoverageOutcome] = {}

    for rec in valid_records:
        sym = str(rec.get("symbol", "")).strip()
        if not sym:
            continue
        rec_date: date = rec["_date"]
        price = _safe_float(rec.get("price"))
        label = str(rec.get("label", "watchlist"))
        score = _safe_float(rec.get("score")) or 0.0
        events = list(rec.get("events") or [])
        regime = str(rec.get("drawdown_regime", "normal") or "normal")
        action_bucket = str(rec.get("action_bucket", "") or "")
        run_id = str(rec.get("run_id", ""))

        existing = open_entries.get(sym)

        if existing is None:
            # First appearance ever for this symbol
            if price is not None and price > 0:
                entry = _make_entry(sym, run_id, rec_date, price, label, score, events, regime, action_bucket)
                open_entries[sym] = entry
                outcomes.append(entry)
        else:
            gap = (rec_date - existing.entry_date).days
            if gap > MAX_TRACK_DAYS:
                # Old entry is expired — start a fresh one if we have a price
                if price is not None and price > 0:
                    new_entry = _make_entry(
                        sym, run_id, rec_date, price, label, score, events, regime, action_bucket
                    )
                    open_entries[sym] = new_entry
                    outcomes.append(new_entry)
            elif price is not None and price > 0:
                # Add as a price observation to the existing entry
                fwd_return = (price - existing.entry_price) / existing.entry_price
                obs = PriceObservation(
                    run_id=run_id,
                    obs_date=rec_date,
                    price=price,
                    forward_return=round(fwd_return, 6),
                    hold_days=gap,
                )
                existing.observations.append(obs)

    # Compute derived metrics for each outcome
    for outcome in outcomes:
        _compute_derived(outcome)

    return outcomes


def _make_entry(
    symbol: str,
    run_id: str,
    entry_date: date,
    entry_price: float,
    label: str,
    score: float,
    events: List[str],
    regime: str,
    action_bucket: str,
) -> CoverageOutcome:
    return CoverageOutcome(
        symbol=symbol,
        entry_run_id=run_id,
        entry_date=entry_date,
        entry_price=entry_price,
        label=label,
        score=score,
        events=events,
        drawdown_regime=regime,
        action_bucket=action_bucket,
    )


def _compute_derived(o: CoverageOutcome) -> None:
    """Populate forward returns, MFE, MAE, exit_quality, hit in-place."""
    if not o.observations:
        o.attributable = False
        return

    o.attributable = True
    all_returns = [obs.forward_return for obs in o.observations]

    # Horizon-specific returns: find obs closest to each horizon
    for h in HORIZONS:
        target = o.entry_date + timedelta(days=h)
        best_obs = None
        best_gap = float("inf")
        for obs in o.observations:
            gap = abs((obs.obs_date - target).days)
            if gap < best_gap:
                best_gap = gap
                best_obs = obs
        # Only accept if within MAX_TRACK_DAYS / 2 of the target horizon
        if best_obs is not None and best_gap <= max(3, h // 2):
            val = round(best_obs.forward_return, 6)
        else:
            val = None
        setattr(o, f"forward_return_{h}d", val)

    o.mfe = round(max(0.0, max(all_returns)), 6)
    o.mae = round(min(0.0, min(all_returns)), 6)
    o.latest_return = round(all_returns[-1], 6)  # most recent obs

    if o.mfe > 0 and o.latest_return is not None:
        o.exit_quality = round(o.latest_return / o.mfe, 4)
    else:
        o.exit_quality = None

    r5 = o.forward_return_5d
    o.hit = (r5 is not None and r5 > HIT_THRESHOLD)


# ---------------------------------------------------------------------------
# Internal: aggregation helpers
# ---------------------------------------------------------------------------

def _aggregate_by_dimension(
    outcomes: List[CoverageOutcome],
    key_fn,
) -> List[Bucket]:
    """Generic aggregation by a single-valued dimension."""
    buckets: Dict[str, Bucket] = {}

    for o in outcomes:
        k = key_fn(o)
        if k not in buckets:
            buckets[k] = Bucket(name=k)
        b = buckets[k]
        _accumulate(b, o)

    result = sorted(buckets.values(), key=lambda b: -b.count)
    for b in result:
        b.small_sample = b.attributable < SMALL_SAMPLE
    return result


def _aggregate_by_score_band(outcomes: List[CoverageOutcome]) -> List[Bucket]:
    """Aggregate by score band: low / medium / high."""
    buckets = {name: Bucket(name=name) for name, _, _ in SCORE_BANDS}

    for o in outcomes:
        band = _score_band(o.score)
        if band in buckets:
            _accumulate(buckets[band], o)

    result = [buckets[name] for name, _, _ in SCORE_BANDS]
    for b in result:
        b.small_sample = b.attributable < SMALL_SAMPLE
    return result


def _aggregate_by_event_type(outcomes: List[CoverageOutcome]) -> List[Bucket]:
    """
    Each outcome can contribute to multiple event-type buckets.
    An outcome with no events goes into 'none'.
    """
    buckets: Dict[str, Bucket] = {}

    for o in outcomes:
        event_keys = list(o.events) if o.events else ["none"]
        for etype in event_keys:
            if etype not in buckets:
                buckets[etype] = Bucket(name=etype)
            _accumulate(buckets[etype], o)

    result = sorted(buckets.values(), key=lambda b: -b.count)
    for b in result:
        b.small_sample = b.attributable < SMALL_SAMPLE
    return result


def _aggregate_by_hold_duration(outcomes: List[CoverageOutcome]) -> List[HorizonStats]:
    """Average return at each standard horizon across all attributable entries."""
    stats = {h: HorizonStats(horizon_days=h) for h in HORIZONS}

    for o in outcomes:
        if not o.attributable:
            continue
        for h in HORIZONS:
            val = getattr(o, f"forward_return_{h}d", None)
            if val is not None:
                stats[h].count += 1
                stats[h].returns.append(val)

    return [stats[h] for h in HORIZONS]


def _accumulate(b: Bucket, o: CoverageOutcome) -> None:
    """Add one CoverageOutcome to a Bucket."""
    b.count += 1
    if not o.attributable:
        return
    b.attributable += 1

    r5 = o.forward_return_5d
    if r5 is not None:
        b.returns.append(r5)
        if r5 > HIT_THRESHOLD:
            b.hit_count += 1
        if r5 >= STRONG_WIN_THRESHOLD:
            b.strong_win_count += 1
        if r5 <= ADVERSE_THRESHOLD:
            b.adverse_count += 1

    if o.mfe is not None:
        b.mfe_values.append(o.mfe)
    if o.mae is not None:
        b.mae_values.append(o.mae)
    if o.exit_quality is not None:
        b.eq_values.append(o.exit_quality)


def _notable_items(
    outcomes: List[CoverageOutcome], n: int = 5
) -> Tuple[List[dict], List[dict]]:
    """Top-N wins and worst-N misses by forward_return_5d."""
    attributed = [o for o in outcomes if o.attributable and o.forward_return_5d is not None]
    by_return = sorted(attributed, key=lambda o: o.forward_return_5d or 0.0)

    wins = [
        {"symbol": o.symbol, "label": o.label, "forward_return_5d": o.forward_return_5d,
         "mfe": o.mfe, "entry_date": o.entry_date.isoformat(), "score": o.score}
        for o in reversed(by_return[-n:])
    ]
    misses = [
        {"symbol": o.symbol, "label": o.label, "forward_return_5d": o.forward_return_5d,
         "mae": o.mae, "entry_date": o.entry_date.isoformat(), "score": o.score}
        for o in by_return[:n]
    ]
    return wins, misses


def _empty_result(now_iso: str, notes: List[str]) -> CoverageEvalResult:
    return CoverageEvalResult(
        total_entries=0,
        attributable_entries=0,
        coverage_rate=0.0,
        by_label=[],
        by_score_band=[],
        by_event_type=[],
        by_regime=[],
        by_action_bucket=[],
        by_hold_duration=[HorizonStats(horizon_days=h) for h in HORIZONS],
        notable_wins=[],
        notable_misses=[],
        data_quality_notes=notes,
        generated_at=now_iso,
    )


def _score_band(score: float) -> str:
    for name, lo, hi in SCORE_BANDS:
        if lo <= score <= hi:
            return name
    return "unknown"


def _parse_date_safe(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (f != f or f == float("inf") or f == float("-inf")) else f
    except (TypeError, ValueError):
        return None
