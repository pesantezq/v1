"""
Profit Attribution and Learning Layer
=======================================
Read-only evaluation module.  Never modifies core decision logic.

Public API:
  run_profit_attribution(history_path, events_path)  → AttributionSummary
  write_attribution_reports(summary, policy_dir, dry_run)  → bool
  build_attribution_memo(summary)  → str

Data flow — Coverage attribution (scanner-level):
  coverage_history.jsonl
       ↓  build_coverage_outcomes()      [coverage_evaluator.py]
  CoverageOutcome[]
       ↓  build_trade_ledger()           [trade_ledger.py]
  TradeLedgerEntry[]
       ↓  analyze_by_*()                 [strategy_analyzer.py]
       ↓  classify_exits()               [exit_analyzer.py]
       ↓  find_missed_opportunities()    [opportunity_tracker.py]
       ↓  compute_metrics()              [metrics_engine.py]
  AttributionSummary.metrics / by_strategy / ...

Data flow — Execution attribution (action-level):
  trade_events.jsonl + coverage_history.jsonl
       ↓  build_execution_ledger()       [execution_ledger.py]
  ExecutionLedgerEntry[]
       ↓  compute_execution_attribution() [execution_metrics.py]
  AttributionSummary.execution (ExecutionAttributionSummary)

       ↓  write_attribution_reports()    [report_writer.py]
  profit_attribution.json + profit_attribution.md
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from profit_attribution.models import AttributionSummary, AttributionMetrics
from profit_attribution.trade_ledger import build_trade_ledger
from profit_attribution.strategy_analyzer import (
    analyze_by_strategy,
    analyze_by_score_band,
    analyze_by_regime,
)
from profit_attribution.exit_analyzer import classify_exits
from profit_attribution.opportunity_tracker import find_missed_opportunities
from profit_attribution.metrics_engine import compute_metrics, notable_trades
from profit_attribution.report_writer import write_attribution_reports, build_attribution_memo
from profit_attribution.execution_ledger import build_execution_ledger
from profit_attribution.execution_metrics import compute_execution_attribution

logger = logging.getLogger("profit_attribution")

__all__ = [
    "run_profit_attribution",
    "write_attribution_reports",
    "build_attribution_memo",
]


def run_profit_attribution(
    history_path=None,
    events_path=None,
) -> AttributionSummary:
    """
    Run the full profit attribution pipeline and return an AttributionSummary.

    Read-only: loads coverage_history.jsonl and trade_events.jsonl.
    Always returns a valid object — degrades gracefully on missing data.

    Args:
        history_path: Optional path override for coverage_history.jsonl.
        events_path:  Optional path override for trade_events.jsonl.

    Returns:
        AttributionSummary with:
          - Coverage attribution  (scanner-level, .metrics / .by_strategy / ...)
          - Execution attribution (action-level,  .execution)
    """
    now_str = datetime.now().isoformat()
    notes: List[str] = []

    # 1. Build trade ledger
    try:
        ledger = build_trade_ledger(history_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profit_attribution: build_trade_ledger failed — %s", exc)
        ledger = []

    if not ledger:
        notes.append(
            "No coverage history records found.  "
            "Enable market_universe scanning and run at least one scan first."
        )
        return _empty_summary(now_str, notes)

    # 2. Compute metrics
    try:
        metrics = compute_metrics(ledger)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profit_attribution: compute_metrics failed — %s", exc)
        metrics = _empty_metrics(len(ledger))
        notes.append(f"Metrics computation failed: {exc}")

    if metrics.entries_with_5d == 0:
        notes.append(
            "No 5-day return data yet.  More scan observations needed "
            "before win-rate and gain/loss metrics can be computed."
        )

    # 3. Strategy / band / regime breakdowns
    try:
        by_strategy = analyze_by_strategy(ledger)
        by_score_band = analyze_by_score_band(ledger)
        by_regime = analyze_by_regime(ledger)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profit_attribution: strategy analysis failed — %s", exc)
        by_strategy, by_score_band, by_regime = [], [], []
        notes.append(f"Strategy breakdown failed: {exc}")

    # 4. Exit quality
    try:
        exit_classified, exit_summary = classify_exits(ledger)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profit_attribution: exit analysis failed — %s", exc)
        exit_classified, exit_summary = [], {}
        notes.append(f"Exit analysis failed: {exc}")

    # 5. Missed opportunities
    try:
        missed, total_opp_cost = find_missed_opportunities(ledger)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profit_attribution: opportunity tracking failed — %s", exc)
        missed, total_opp_cost = [], None
        notes.append(f"Opportunity tracking failed: {exc}")

    # 6. Notable trades
    try:
        best, worst = notable_trades(ledger)
    except Exception as exc:  # noqa: BLE001
        logger.warning("profit_attribution: notable_trades failed — %s", exc)
        best, worst = [], []

    # 7. Execution-level attribution (additive — does not affect coverage metrics)
    execution_summary = None
    try:
        exec_ledger = build_execution_ledger(
            events_path=events_path,
            history_path=history_path,
        )
        if exec_ledger:
            execution_summary = compute_execution_attribution(exec_ledger)
            logger.info(
                "profit_attribution: execution — %d events, %d matched (%.0f%%)",
                execution_summary.total_events,
                execution_summary.matched_events,
                execution_summary.match_rate * 100,
            )
        else:
            logger.debug("profit_attribution: no execution events found — skipping execution attribution")
    except Exception as exc:  # noqa: BLE001
        logger.warning("profit_attribution: execution attribution failed — %s", exc)

    # 8. Data quality notes
    if metrics.coverage_rate < 0.3 and metrics.total_entries > 0:
        notes.append(
            f"Low coverage: only {metrics.attributable_entries}/{metrics.total_entries} "
            f"entries have observations ({metrics.coverage_rate * 100:.0f}%).  "
            "Run more scans to populate returns."
        )

    logger.info(
        "profit_attribution: %d trades, %d attr, win=%.0f%%, rr=%s, %d missed opps",
        metrics.total_entries,
        metrics.attributable_entries,
        (metrics.win_rate or 0) * 100,
        f"{metrics.risk_reward:.2f}" if metrics.risk_reward is not None else "—",
        len(missed),
    )

    return AttributionSummary(
        generated_at=now_str,
        metrics=metrics,
        by_strategy=by_strategy,
        by_score_band=by_score_band,
        by_regime=by_regime,
        trade_ledger=ledger,
        exit_summary=exit_summary,
        exit_classified=exit_classified,
        missed_opportunities=missed,
        total_opportunity_cost=total_opp_cost,
        best_trades=best,
        worst_trades=worst,
        data_quality_notes=notes,
        execution=execution_summary,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_summary(now_str: str, notes: List[str]) -> AttributionSummary:
    return AttributionSummary(
        generated_at=now_str,
        metrics=_empty_metrics(0),
        by_strategy=[],
        by_score_band=[],
        by_regime=[],
        trade_ledger=[],
        exit_summary={},
        exit_classified=[],
        missed_opportunities=[],
        total_opportunity_cost=None,
        best_trades=[],
        worst_trades=[],
        data_quality_notes=notes,
    )


def _empty_metrics(total: int) -> AttributionMetrics:
    return AttributionMetrics(
        total_entries=total,
        attributable_entries=0,
        entries_with_5d=0,
        coverage_rate=0.0,
        win_rate=None,
        avg_gain=None,
        avg_loss=None,
        risk_reward=None,
        expectancy=None,
        capital_efficiency=None,
        avg_mfe=None,
        avg_mae=None,
        avg_exit_quality=None,
        avg_hold_days=None,
        strong_win_rate=None,
        adverse_rate=None,
    )
