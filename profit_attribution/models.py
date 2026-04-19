"""
Profit Attribution — Data Models
=================================
All dataclasses for trade-level tracking, strategy performance,
exit classification, missed opportunities, and aggregated metrics.

Two attribution layers:
  Coverage attribution  — scanner-promoted candidates (coverage_history.jsonl)
  Execution attribution — system-recommended actions  (trade_events.jsonl)

These are pure data containers: no IO, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Trade lifecycle
# ---------------------------------------------------------------------------

@dataclass
class TradeLedgerEntry:
    """
    A single investment candidate's full lifecycle from entry to latest observation.

    Derived from CoverageOutcome; one entry = one promotion event from the
    scanner pipeline.  Symbol + entry_date form the natural key.
    """
    trade_id: str             # "{symbol}_{entry_run_id}"
    symbol: str
    strategy_type: str        # compounder | momentum | watchlist
    entry_date: str           # ISO date (YYYY-MM-DD)
    entry_price: float
    entry_score: float        # 0–100 composite score at promotion
    entry_events: List[str]   # scanner event types that fired at entry
    entry_regime: str         # drawdown regime at entry
    action_bucket: str        # portfolio_context.action_bucket (may be "")

    # Lifecycle
    observation_count: int    # subsequent price observations
    hold_days: Optional[int]  # calendar days from entry to latest observation

    # Returns at standard horizons
    return_1d: Optional[float]
    return_3d: Optional[float]
    return_5d: Optional[float]
    return_10d: Optional[float]
    latest_return: Optional[float]

    # Risk excursion metrics
    mfe: Optional[float]           # max favorable excursion (peak gain ≥ 0)
    mae: Optional[float]           # max adverse excursion (worst loss ≤ 0)
    exit_quality: Optional[float]  # latest_return / mfe — 1.0 = peak retained

    # Outcome flags
    hit: Optional[bool]    # return_5d > 0
    attributable: bool     # True iff ≥1 observation exists

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "strategy_type": self.strategy_type,
            "entry_date": self.entry_date,
            "entry_price": self.entry_price,
            "entry_score": self.entry_score,
            "entry_events": self.entry_events,
            "entry_regime": self.entry_regime,
            "action_bucket": self.action_bucket,
            "observation_count": self.observation_count,
            "hold_days": self.hold_days,
            "return_1d": self.return_1d,
            "return_3d": self.return_3d,
            "return_5d": self.return_5d,
            "return_10d": self.return_10d,
            "latest_return": self.latest_return,
            "mfe": self.mfe,
            "mae": self.mae,
            "exit_quality": self.exit_quality,
            "hit": self.hit,
            "attributable": self.attributable,
        }


# ---------------------------------------------------------------------------
# Exit classification
# ---------------------------------------------------------------------------

# Exit quality thresholds (explicit constants)
EXIT_QUALITY_PROTECTED: float = 0.70   # retained ≥70% of peak → well managed
EXIT_QUALITY_PARTIAL: float = 0.30     # retained 30–70%
# < 0.30 but > 0 → gave back most gains
# ≤ 0          → gain turned to loss / reversed
# mfe == 0     → never had a meaningful gain

EXIT_LABELS = ("protected", "partial", "gave_back", "reversed", "no_gain", "unresolved")


@dataclass
class ExitClassification:
    """How well the exit was managed for a single trade."""
    trade_id: str
    symbol: str
    exit_quality: Optional[float]
    label: str    # one of EXIT_LABELS
    detail: str   # human-readable explanation

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "exit_quality": self.exit_quality,
            "label": self.label,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Strategy performance bucket
# ---------------------------------------------------------------------------

@dataclass
class StrategyPerformance:
    """Aggregated performance metrics for a named grouping (strategy / score band / regime)."""
    name: str               # e.g. "compounder", "high", "normal"
    dimension: str          # "strategy" | "score_band" | "regime"
    total_entries: int = 0
    attributable: int = 0
    entries_with_5d: int = 0   # attributable AND have a 5d return
    hit_count: int = 0
    strong_win_count: int = 0  # return_5d ≥ +2%
    adverse_count: int = 0     # return_5d ≤ −2%
    returns_5d: List[float] = field(default_factory=list)
    gains: List[float] = field(default_factory=list)   # positive 5d returns only
    losses: List[float] = field(default_factory=list)  # negative 5d returns only
    mfe_values: List[float] = field(default_factory=list)
    mae_values: List[float] = field(default_factory=list)
    eq_values: List[float] = field(default_factory=list)
    hold_days_values: List[int] = field(default_factory=list)
    small_sample: bool = False

    @property
    def win_rate(self) -> Optional[float]:
        if not self.returns_5d:
            return None
        return round(self.hit_count / len(self.returns_5d), 4)

    @property
    def avg_gain(self) -> Optional[float]:
        if not self.gains:
            return None
        return round(sum(self.gains) / len(self.gains), 6)

    @property
    def avg_loss(self) -> Optional[float]:
        if not self.losses:
            return None
        return round(sum(self.losses) / len(self.losses), 6)

    @property
    def risk_reward(self) -> Optional[float]:
        g = self.avg_gain
        l_ = self.avg_loss
        if g is None or l_ is None or l_ == 0:
            return None
        return round(g / abs(l_), 4)

    @property
    def avg_mfe(self) -> Optional[float]:
        if not self.mfe_values:
            return None
        return round(sum(self.mfe_values) / len(self.mfe_values), 6)

    @property
    def avg_mae(self) -> Optional[float]:
        if not self.mae_values:
            return None
        return round(sum(self.mae_values) / len(self.mae_values), 6)

    @property
    def avg_exit_quality(self) -> Optional[float]:
        if not self.eq_values:
            return None
        return round(sum(self.eq_values) / len(self.eq_values), 4)

    @property
    def avg_hold_days(self) -> Optional[float]:
        if not self.hold_days_values:
            return None
        return round(sum(self.hold_days_values) / len(self.hold_days_values), 1)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "dimension": self.dimension,
            "total_entries": self.total_entries,
            "attributable": self.attributable,
            "entries_with_5d": self.entries_with_5d,
            "hit_count": self.hit_count,
            "strong_win_count": self.strong_win_count,
            "adverse_count": self.adverse_count,
            "win_rate": self.win_rate,
            "avg_gain": self.avg_gain,
            "avg_loss": self.avg_loss,
            "risk_reward": self.risk_reward,
            "avg_mfe": self.avg_mfe,
            "avg_mae": self.avg_mae,
            "avg_exit_quality": self.avg_exit_quality,
            "avg_hold_days": self.avg_hold_days,
            "small_sample": self.small_sample,
        }


# ---------------------------------------------------------------------------
# Missed opportunities
# ---------------------------------------------------------------------------

MISSED_HIGH_SCORE_THRESHOLD: float = 70.0  # candidates with score ≥ this are "notable"


@dataclass
class OpportunityRecord:
    """A high-scored candidate that the portfolio did not act on."""
    symbol: str
    entry_date: str
    strategy_type: str
    score: float
    action_bucket: str          # "watchonly" / "" / unclassified → not acted on
    forward_return_5d: Optional[float]
    mfe: Optional[float]
    outcome: str                # "would_have_won" | "would_have_lost" | "unknown"
    opportunity_cost: Optional[float]  # positive return left on the table (0 if would_have_lost)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry_date": self.entry_date,
            "strategy_type": self.strategy_type,
            "score": self.score,
            "action_bucket": self.action_bucket,
            "forward_return_5d": self.forward_return_5d,
            "mfe": self.mfe,
            "outcome": self.outcome,
            "opportunity_cost": self.opportunity_cost,
        }


# ---------------------------------------------------------------------------
# Aggregated top-level metrics
# ---------------------------------------------------------------------------

@dataclass
class AttributionMetrics:
    """Overall aggregated metrics across all attributable trades."""
    total_entries: int
    attributable_entries: int
    entries_with_5d: int
    coverage_rate: float

    win_rate: Optional[float]
    avg_gain: Optional[float]        # mean of positive 5d returns
    avg_loss: Optional[float]        # mean of negative 5d returns (negative value)
    risk_reward: Optional[float]     # avg_gain / abs(avg_loss)
    expectancy: Optional[float]      # win_rate * avg_gain + (1-win_rate) * avg_loss
    capital_efficiency: Optional[float]  # sum(positive) / sum(abs(all)) — how concentrated profits are

    avg_mfe: Optional[float]
    avg_mae: Optional[float]
    avg_exit_quality: Optional[float]
    avg_hold_days: Optional[float]

    strong_win_rate: Optional[float]  # ≥ +2%
    adverse_rate: Optional[float]     # ≤ −2%

    def to_dict(self) -> dict:
        return {
            "total_entries": self.total_entries,
            "attributable_entries": self.attributable_entries,
            "entries_with_5d": self.entries_with_5d,
            "coverage_rate": self.coverage_rate,
            "win_rate": self.win_rate,
            "avg_gain": self.avg_gain,
            "avg_loss": self.avg_loss,
            "risk_reward": self.risk_reward,
            "expectancy": self.expectancy,
            "capital_efficiency": self.capital_efficiency,
            "avg_mfe": self.avg_mfe,
            "avg_mae": self.avg_mae,
            "avg_exit_quality": self.avg_exit_quality,
            "avg_hold_days": self.avg_hold_days,
            "strong_win_rate": self.strong_win_rate,
            "adverse_rate": self.adverse_rate,
        }


# ---------------------------------------------------------------------------
# Top-level attribution result
# ---------------------------------------------------------------------------

@dataclass
class AttributionSummary:
    """Full profit attribution output. Always a valid object, even with no data."""
    generated_at: str

    # Aggregated metrics
    metrics: AttributionMetrics

    # Per-dimension performance breakdowns
    by_strategy: List[StrategyPerformance]    # compounder / momentum / watchlist
    by_score_band: List[StrategyPerformance]  # low / medium / high
    by_regime: List[StrategyPerformance]      # normal / modest_dip / etc.

    # Trade-level ledger
    trade_ledger: List[TradeLedgerEntry]

    # Exit quality
    exit_summary: Dict[str, int]          # label → count across all trades
    exit_classified: List[ExitClassification]

    # Missed opportunities
    missed_opportunities: List[OpportunityRecord]
    total_opportunity_cost: Optional[float]   # sum of positive missed returns

    # Notable items
    best_trades: List[dict]   # top-5 by 5d return
    worst_trades: List[dict]  # bottom-5 by 5d return

    # Data quality
    data_quality_notes: List[str]

    # Execution-level attribution (optional — None when trade_events.jsonl absent)
    execution: Optional["ExecutionAttributionSummary"] = None

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "metrics": self.metrics.to_dict(),
            "by_strategy": [s.to_dict() for s in self.by_strategy],
            "by_score_band": [s.to_dict() for s in self.by_score_band],
            "by_regime": [s.to_dict() for s in self.by_regime],
            "trade_ledger": [t.to_dict() for t in self.trade_ledger],
            "exit_summary": self.exit_summary,
            "exit_classified": [e.to_dict() for e in self.exit_classified],
            "missed_opportunities": [o.to_dict() for o in self.missed_opportunities],
            "total_opportunity_cost": self.total_opportunity_cost,
            "best_trades": self.best_trades,
            "worst_trades": self.worst_trades,
            "data_quality_notes": self.data_quality_notes,
            "execution": self.execution.to_dict() if self.execution else None,
        }


# ---------------------------------------------------------------------------
# Confidence calibration result
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceCalibrationResult:
    """
    Observe-only calibration assessment for execution confidence bands.

    Evaluates whether low / medium / high confidence tiers are meaningfully
    predictive of execution outcomes.  Never mutates live thresholds.
    """
    observe_only: bool          # always True — safety marker

    status: str                 # "healthy" | "weak_separation" | "insufficient_data" | "no_data"

    # Matched-event counts per band
    low_matched: int
    medium_matched: int
    high_matched: int

    # Win rate per band (primary calibration metric)
    low_win_rate: Optional[float]
    medium_win_rate: Optional[float]
    high_win_rate: Optional[float]

    # Expectancy per band  (win_rate × avg_gain + (1−win_rate) × avg_loss)
    low_expectancy: Optional[float]
    medium_expectancy: Optional[float]
    high_expectancy: Optional[float]

    # Calibration verdict
    band_order_valid: Optional[bool]  # high ≥ medium ≥ low on win_rate
    strongest_band: Optional[str]
    weakest_band: Optional[str]

    # Observe-only recommendation
    recommendation: str
    recommendation_reason: str

    def to_dict(self) -> dict:
        return {
            "observe_only": self.observe_only,
            "status": self.status,
            "sample_summary": {
                "low_matched": self.low_matched,
                "medium_matched": self.medium_matched,
                "high_matched": self.high_matched,
                "total_matched": self.low_matched + self.medium_matched + self.high_matched,
            },
            "low_win_rate": self.low_win_rate,
            "medium_win_rate": self.medium_win_rate,
            "high_win_rate": self.high_win_rate,
            "low_expectancy": self.low_expectancy,
            "medium_expectancy": self.medium_expectancy,
            "high_expectancy": self.high_expectancy,
            "band_order_valid": self.band_order_valid,
            "strongest_band": self.strongest_band,
            "weakest_band": self.weakest_band,
            "recommendation": self.recommendation,
            "recommendation_reason": self.recommendation_reason,
        }


# ---------------------------------------------------------------------------
# Execution-level attribution models
# ---------------------------------------------------------------------------

@dataclass
class ExecutionLedgerEntry:
    """
    One trade event record (BUY / SELL / TRIM / PROMOTE_TO_PORTFOLIO) enriched
    with forward-return data from the matched coverage outcome.

    Source: trade_events.jsonl (advisory execution log).
    Outcomes: linked from coverage_history.jsonl via symbol + nearest date.
    """
    event_id: str               # "{symbol}_{run_id}"
    symbol: str
    action: str                 # BUY | SELL | TRIM | PROMOTE_TO_PORTFOLIO
    run_id: str
    timestamp: str
    run_mode: str
    strategy_type: Optional[str]
    score: Optional[float]
    confidence: Optional[float]
    suggested_allocation_pct: Optional[float]
    suggested_allocation_amount: Optional[float]
    drawdown_regime: str
    degraded_mode: bool

    # Forward-return data from matched coverage outcome
    return_1d: Optional[float] = None
    return_3d: Optional[float] = None
    return_5d: Optional[float] = None
    return_10d: Optional[float] = None
    mfe: Optional[float] = None
    mae: Optional[float] = None
    exit_quality: Optional[float] = None
    hold_days: Optional[int] = None

    # Match metadata
    matched: bool = False
    match_quality: str = "none"     # "exact" | "nearest" | "none"

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "action": self.action,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "run_mode": self.run_mode,
            "strategy_type": self.strategy_type,
            "score": self.score,
            "confidence": self.confidence,
            "suggested_allocation_pct": self.suggested_allocation_pct,
            "suggested_allocation_amount": self.suggested_allocation_amount,
            "drawdown_regime": self.drawdown_regime,
            "degraded_mode": self.degraded_mode,
            "return_1d": self.return_1d,
            "return_3d": self.return_3d,
            "return_5d": self.return_5d,
            "return_10d": self.return_10d,
            "mfe": self.mfe,
            "mae": self.mae,
            "exit_quality": self.exit_quality,
            "hold_days": self.hold_days,
            "matched": self.matched,
            "match_quality": self.match_quality,
        }


@dataclass
class ExecutionActionMetrics:
    """
    Aggregated performance metrics for one action type (BUY / SELL / TRIM / PROMOTE).

    BUY/PROMOTE: win_rate, expectancy answer "did our buys work?"
    SELL/TRIM:   avg_exit_quality answers "did we exit well?"
    """
    action: str
    total_events: int
    matched_events: int         # events linked to a coverage outcome
    entries_with_5d: int

    win_rate: Optional[float]           # BUY/PROMOTE primary metric
    avg_gain: Optional[float]
    avg_loss: Optional[float]
    risk_reward: Optional[float]
    expectancy: Optional[float]
    avg_exit_quality: Optional[float]   # SELL/TRIM primary metric

    @property
    def match_rate(self) -> Optional[float]:
        if self.total_events == 0:
            return None
        return round(self.matched_events / self.total_events, 4)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "total_events": self.total_events,
            "matched_events": self.matched_events,
            "match_rate": self.match_rate,
            "entries_with_5d": self.entries_with_5d,
            "win_rate": self.win_rate,
            "avg_gain": self.avg_gain,
            "avg_loss": self.avg_loss,
            "risk_reward": self.risk_reward,
            "expectancy": self.expectancy,
            "avg_exit_quality": self.avg_exit_quality,
        }


@dataclass
class ExecutionAttributionSummary:
    """
    Full execution-level attribution result.

    Answers: 'What actions the system actually recommended made money?'
    Clearly labelled as advisory execution — not broker fill data.
    """
    generated_at: str
    total_events: int
    matched_events: int
    match_rate: float

    by_action: List[ExecutionActionMetrics]

    # Same StrategyPerformance buckets as coverage attribution,
    # but populated from execution events instead of scanner promotions.
    by_strategy: List[StrategyPerformance]
    by_score_band: List[StrategyPerformance]
    by_regime: List[StrategyPerformance]
    by_confidence_band: List[StrategyPerformance]   # low / medium / high confidence tiers
    confidence_calibration: "ConfidenceCalibrationResult"

    execution_ledger: List[ExecutionLedgerEntry]
    data_quality_notes: List[str]

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "total_events": self.total_events,
            "matched_events": self.matched_events,
            "match_rate": self.match_rate,
            "by_action": [a.to_dict() for a in self.by_action],
            "by_strategy": [s.to_dict() for s in self.by_strategy],
            "by_score_band": [s.to_dict() for s in self.by_score_band],
            "by_regime": [s.to_dict() for s in self.by_regime],
            "by_confidence_band": [s.to_dict() for s in self.by_confidence_band],
            "confidence_calibration": self.confidence_calibration.to_dict(),
            "execution_ledger": [e.to_dict() for e in self.execution_ledger],
            "data_quality_notes": self.data_quality_notes,
        }
