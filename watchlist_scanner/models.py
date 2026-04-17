from __future__ import annotations

from typing import Any, TypedDict


class ScoreBreakdown(TypedDict, total=False):
    theme_news_score: float
    technical_score: float
    fundamental_context_score: float


class FundamentalsSnapshot(TypedDict, total=False):
    sector: str
    market_cap: float
    pe_ratio: float
    profit_margin: float


class NewsSnapshot(TypedDict, total=False):
    headline_count: int
    avg_sentiment: float
    themes: list[str]
    theme_scores: dict[str, float]
    top_headlines: list[str]


class TechnicalSnapshot(TypedDict, total=False):
    price: float
    price_change_1d: float
    price_change_5d: float | None
    sma20: float | None
    sma50: float | None
    above_sma20: bool
    above_sma50: bool
    volume_today: int
    volume_avg20: int | None
    volume_spike: bool
    data_days: int


class AlertDecision(TypedDict, total=False):
    priority: str | None
    basis: list[str]
    basis_summary: str
    reason: str
    code: str
    confirmation_signals: list[str]
    confirmation_summary: str
    evidence_categories: list[str]
    evidence_breadth: int
    alert_quality_tier: str
    trusted_signal_score: float
    composite_support: bool


class PortfolioContext(TypedDict, total=False):
    holdings: list[dict[str, Any]]
    cash_available: float
    target_cash_weight: float
    held_theme_counts: dict[str, int]
    held_sector_counts: dict[str, int]


class PortfolioContextSummary(TypedDict, total=False):
    holding_count: int
    held_theme_counts: dict[str, int]
    held_sector_counts: dict[str, int]
    available_cash: float | None


class ScanSummary(TypedDict, total=False):
    scan_status: str
    symbols_fresh: int
    symbols_cached: int
    symbols_partial: int
    symbols_budget_skipped: int
    alerts_watch_level: int
    signals_conf_suppressed: int
    alerts_cooldown_suppressed: int
    alerts_action_suppressed: int
    signals_suppressed: int
    cooldown_hits: int
    performance_tracked_signals: int
    performance_resolved_signals: int
    conviction_band_counts: dict[str, int]
    conviction_summary_line: str
    portfolio_construction_summary_line: str
    portfolio_construction_label: str
    market_regime_summary_line: str
    degraded_mode: bool
    degraded_reason: str | None
    data_sources_used: list[str]
    data_mode: str
    degraded_confidence_penalty: float


class ExtendedWatchlistMeta(TypedDict, total=False):
    extended_tickers: list[str]
    skipped_for_budget: list[str]


class WatchlistRow(TypedDict, total=False):
    # Base scanner output
    ticker: str
    scan_time: str
    data_quality: str
    confidence_score: float
    confidence_band: str
    confidence_reasons: list[str]
    price: float
    price_change_pct: float
    above_sma20: bool
    above_sma50: bool
    volume_spike: bool
    themes: list[str]
    headline_examples: list[str]
    signal_score: float
    sma20: float | None
    sma50: float | None
    volume_today: int
    volume_avg20: int | None
    theme_scores: dict[str, float]
    news_count: int
    avg_sentiment: float
    fundamentals: FundamentalsSnapshot | dict[str, Any]
    news: NewsSnapshot | dict[str, Any]
    technicals: TechnicalSnapshot | dict[str, Any]
    score_breakdown: ScoreBreakdown | dict[str, float]

    # Routing / ranking enrichment
    alert_priority: str | None
    routed_alert_priority: str | None
    alert_basis: list[str]
    alert_basis_summary: str
    alert_decision_reason: str
    alert_decision_code: str
    alert_confirmation_signals: list[str]
    alert_confirmation_summary: str
    confirmation_count: int
    evidence_categories: list[str]
    evidence_breadth: int
    alert_quality_tier: str
    trusted_signal_score: float
    watchlist_source: str
    notification_status: str
    notification_reason: str
    filter_allowed: bool
    filter_reason: str
    filter_reason_code: str
    filtered_reason: str
    alert_tier: str | None
    cooldown_applied_hours: int | None
    cooldown_override_reason: str
    evidence_count: int
    priority_score: float
    priority_explanation: str
    portfolio_priority: float
    overlap_penalty: float
    diversification_bonus: float
    existing_position_relevance_bonus: float
    budget_fit: str
    budget_fit_score: float
    exposure_context: str
    final_operator_rank_reason: str
    operator_rank: int
    data_mode: str
    degraded_confidence_score: float
    degraded_confidence_penalty: float
    confidence_weight: float
    effective_score: float
    cooldown_active: bool
    cooldown_reason: str
    actionable_signal: bool
    action_suppressed: bool
    action_suppression_reason: str
    last_alert_timestamp: str | None
    last_action_taken: str
    recent_signal_strength: float
    historical_performance_score: float | None
    signal_reliability: str
    conviction_score: float
    conviction_band: str
    sizing_recommendation: str
    sizing_reason: str
    target_allocation_band: str
    sizing_multiplier: float
    capital_sizing_note: str
    conviction_inputs: dict[str, Any]
    conviction_caps_applied: list[str]
    portfolio_sector: str
    portfolio_themes: list[str]
    market_cap_bucket: str
    suggested_allocation: float
    normalized_allocation: float
    allocation_capped: bool
    allocation_cap_reason: str

    # Outcome lifecycle
    alert_event_id: int
    surfaced_at: str
    baseline_price: float
    evaluation_window: str
    outcome_status: str
    outcome_pending: bool


class WatchlistScanResult(TypedDict, total=False):
    run_date: str
    generated_at: str
    calls_used: int
    scan_summary: ScanSummary
    results: list[WatchlistRow]
    alerts: list[WatchlistRow]
    extended_watchlist_meta: ExtendedWatchlistMeta
    portfolio_context_summary: PortfolioContextSummary
    degraded_mode: bool
    degraded_reason: str | None
    data_sources_used: list[str]
    data_mode: str
    data_fallback_triggered: bool
    performance_feedback: dict[str, Any]
    portfolio_construction: dict[str, Any]
    market_regime: dict[str, Any]
