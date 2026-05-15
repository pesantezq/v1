#!/usr/bin/env python3
"""
Portfolio Automation System - Main Entry Point

A production-ready, rules-based portfolio tracking and rebalancing tool.
Supports dynamic configuration, multiple investors, and scheduled execution.

Usage:
    python main.py [--config CONFIG_PATH] [--env ENV_PATH] [--debug] [--dry-run]
    
Options:
    --config    Path to configuration JSON file (default: config.json)
    --env       Path to .env file (default: .env)
    --debug     Enable debug logging
    --dry-run   Run without sending emails or modifying files

Author: Portfolio Automation System
License: MIT
"""

import argparse
import csv
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

# Local imports
from utils import (
    setup_logging, load_env, load_config, validate_config,
    get_timestamp, Config
)
from market_data import (
    create_market_client, update_holdings_with_prices
)
from portfolio import (
    calculate_portfolio_values, calculate_allocations,
    generate_portfolio_summary, analyze_holdings,
    format_summary_text, format_holdings_table
)
from recommendations import (
    generate_recommendations, format_recommendations_text
)
from retirement import load_retirement_data, format_retirement_summary
from file_output import (
    write_csv_snapshot, create_excel_workbook, export_recommendations_csv,
    write_contribution_plan_csv, write_compounding_dashboard_txt,
)
from email_reporter import (
    create_email_reporter, should_send_report, EmailError
)
from scoring import ActionLevel
from finance_analyzer import (
    FinanceAnalyzer, FinanceConfig, FinanceHistoryStore,
    export_recommendations_csv as export_scored_recommendations_csv
)
from email_digest import (
    FinanceEmailDigest, build_top_summary,
    compute_digest_hash, compute_monthly_memo_hash,
)
from adjustment import (
    generate_portfolio_adjustments,
    format_adjustments_for_email_view,
    get_email_prompt,
    ActionLevel as AdjActionLevel,
    CashAnalysis
)
from ml_history import MLHistoryStore, create_record_from_adjustment, update_record_resolution
from ml_advisor import MLAdvisor, get_historical_analysis_prompt
from drawdown import DrawdownTracker
from contribution_engine import ContributionEngine
from projections import compute_portfolio_cagr, compute_compounding_dashboard, format_dashboard_text
from run_lock import acquire_run_lock, release_run_lock
from state_store import PortfolioStateStore
from guardrails import run_guardrail_checks
from degraded_mode import (
    DEFAULT_STALE_DAYS,
    MIN_TRUSTED_DATASET_SIZE,
    build_data_health_context,
    stale_cache_days_for_path,
    summarize_data_health,
)
import json as _json

try:
    from portfolio_automation.decision_explainer import (
        generate_decision_explanations,
    )
except ImportError:
    generate_decision_explanations = None  # type: ignore[assignment]

try:
    from portfolio_automation.ai_decision_validator import (
        run_ai_validation as _run_ai_validation,
    )
except ImportError:
    _run_ai_validation = None  # type: ignore[assignment]

try:
    from portfolio_automation.decision_outcome_tracker import (
        run_outcome_tracker as _run_outcome_tracker,
    )
except ImportError:
    _run_outcome_tracker = None  # type: ignore[assignment]

try:
    from portfolio_automation.decision_triage import (
        run_triage as _run_triage,
    )
except ImportError:
    _run_triage = None  # type: ignore[assignment]

try:
    from portfolio_automation.confidence_calibration import (
        run_calibration as _run_calibration,
    )
except ImportError:
    _run_calibration = None  # type: ignore[assignment]

try:
    from portfolio_automation.decision_performance_attribution import (
        run_performance_attribution as _run_performance_attribution,
    )
except ImportError:
    _run_performance_attribution = None  # type: ignore[assignment]

try:
    from portfolio_automation.memo_email_sender import (
        run_memo_email_delivery as _run_memo_email_delivery,
    )
except ImportError:
    _run_memo_email_delivery = None  # type: ignore[assignment]

try:
    from api_budget import AVDailyBudget as _AVDailyBudget
except ImportError:
    _AVDailyBudget = None  # type: ignore[assignment,misc]


def _persist_guardrail_violations(
    store: PortfolioStateStore,
    violations: list[dict],
    logger_obj,
) -> None:
    """Sync serialized guardrail violations into the state store."""
    active_keys: set[str] = set()
    for violation in violations:
        violation_type = str(
            violation.get("violation_type")
            or violation.get("type")
            or "unknown"
        )
        symbol = str(violation.get("symbol", ""))
        rule = str(violation.get("rule") or violation_type)
        vkey = f"{violation_type}|{symbol}|{rule}"
        vrow = store.upsert_structural_violation(vkey)
        violation["days_active"] = vrow.get("days_active", 0)
        violation["escalation_level"] = vrow.get("escalation_level", 0)
        active_keys.add(vkey)

    for old in store.get_all_structural_violations():
        if old["violation_key"] not in active_keys:
            store.clear_structural_violation(old["violation_key"])
            logger_obj.info("Structural violation resolved: %s", old["violation_key"])


def _mark_failed_run(
    store: Optional[PortfolioStateStore],
    run_id: Optional[str],
    dry_run: bool,
    logger_obj,
) -> None:
    """Best-effort cleanup for a run that started but failed before finalization."""
    if dry_run or store is None or not run_id:
        return
    try:
        store.fail_run(run_id)
    except Exception as exc:
        logger_obj.warning("State store failure cleanup failed (non-fatal): %s", exc)


def _clear_conditional_output_artifacts(output_dir: Path, logger_obj) -> None:
    """Remove data-dependent files that are not always rewritten every run."""
    for name in (
        "email_view.csv",
        "ml_advisor_outputs.csv",
        "candidates_top20.csv",
        "candidates_debug.csv",
        "spec_sleeve_plan.csv",
        "contribution_plan.csv",
        "compounding_dashboard.txt",
    ):
        path = output_dir / name
        try:
            if path.exists():
                path.unlink()
                logger_obj.debug("Cleared stale conditional artifact: %s", path)
        except Exception as exc:
            logger_obj.warning("Failed to clear stale artifact %s (non-fatal): %s", path, exc)


def _decision_explainer_root_from_output_dir(output_dir: Path) -> Path:
    """Infer project root for additive explanation outputs from outputs/latest."""
    if output_dir.name == "latest" and output_dir.parent.name == "outputs":
        return output_dir.parent.parent
    return Path(".")


def _write_decision_engine_outputs(
    output_dir: Path,
    result: dict[str, Any],
    run_mode: str,
    logger_obj,
    *,
    explainer_root: Optional[Path] = None,
) -> None:
    """
    Write additive decision-plan artifacts, then trigger the additive explainer.

    All failures are intentionally non-fatal so explanation output can never
    block the main advisory pipeline.
    """
    try:
        _dp_list = result.get('decision_plan') or []
        _dp_summary = result.get('decision_plan_summary') or ''
        _dp_json_path = output_dir / 'decision_plan.json'
        _dp_json_path.write_text(
            _json.dumps(
                {
                    'generated_at': datetime.now().isoformat(),
                    'run_mode': run_mode,
                    'observe_only': True,
                    'total_decisions': len(_dp_list),
                    'decisions': _dp_list,
                },
                indent=2,
                default=str,
            ),
            encoding='utf-8',
        )
        (output_dir / 'decision_plan.md').write_text(
            _dp_summary, encoding='utf-8'
        )
        logger_obj.info(
            "DECISION ENGINE: decision_plan.json + decision_plan.md written"
            " (%d decisions)", len(_dp_list),
        )
    except Exception as _dp_write_err:
        logger_obj.warning(
            "DECISION ENGINE: output write failed (non-fatal): %s",
            _dp_write_err,
        )
        return

    if generate_decision_explanations is None:
        logger_obj.warning(
            "DECISION EXPLAINER: module unavailable; skipping additive explanations"
            " (non-fatal)"
        )
        return

    try:
        _explainer_root = explainer_root or _decision_explainer_root_from_output_dir(output_dir)
        _explanation_payload, _ = generate_decision_explanations(_explainer_root)
        logger_obj.info(
            "DECISION EXPLAINER: decision_explanations.json +"
            " decision_explanations.md written (%d explanations)",
            len((_explanation_payload or {}).get('explanations') or []),
        )
    except Exception as _explainer_err:
        logger_obj.warning(
            "DECISION EXPLAINER: output write failed (non-fatal): %s",
            _explainer_err,
        )

    if _run_ai_validation is None:
        logger_obj.warning(
            "AI VALIDATOR: module unavailable; skipping ai_decision_validation (non-fatal)"
        )
        return

    try:
        _validator_root = explainer_root or _decision_explainer_root_from_output_dir(output_dir)
        _use_llm = bool(int(os.environ.get("AI_VALIDATOR_USE_LLM", "0")))
        _validation_payload, _ = _run_ai_validation(_validator_root, use_llm=_use_llm)
        logger_obj.info(
            "AI VALIDATOR: ai_decision_validation.json + .md written"
            " (%d validated, aligned=%d caution=%d contradiction=%d insufficient=%d)",
            _validation_payload.get('total_validated', 0),
            _validation_payload.get('aligned_count', 0),
            _validation_payload.get('caution_count', 0),
            _validation_payload.get('contradiction_count', 0),
            _validation_payload.get('insufficient_context_count', 0),
        )
    except Exception as _validator_err:
        logger_obj.warning(
            "AI VALIDATOR: output write failed (non-fatal): %s",
            _validator_err,
        )


def _annotate_scanner_candidates_for_data_mode(
    candidates: list[dict],
    data_health: dict,
) -> list[dict]:
    """Attach non-ranking data-health metadata to scanner rows."""
    for row in candidates:
        row["data_mode"] = data_health.get("data_mode", "live")
        row["degraded_mode"] = bool(data_health.get("degraded_mode", False))
        row["degraded_reason"] = data_health.get("degraded_reason")
        row["degraded_confidence_penalty"] = data_health.get("degraded_confidence_penalty", 0.0)
    return candidates


def _adj_to_de_dict(adj: Any) -> dict:
    """Convert a PortfolioAdjustment to a plain dict for the decision engine."""
    try:
        return {
            'symbol': adj.symbol,
            'title': adj.title,
            'recommendation_type': (
                adj.recommendation_type.value if adj.recommendation_type else 'hold'
            ),
            'adjustment_mode': (
                adj.adjustment_mode.value if adj.adjustment_mode else 'NO_ACTION'
            ),
            'action_level': (
                adj.action_level.value if adj.action_level else 'MONITOR'
            ),
            'is_leveraged': bool(adj.is_leveraged),
            'amount': adj.amount,
            'drift': adj.drift,
            'do': adj.do,
            'why': adj.why,
        }
    except Exception:
        return {}


def _finance_rec_to_de_dict(rec: Any) -> dict:
    """Convert a FinanceRecommendation to a plain dict for the decision engine."""
    try:
        return {
            'id': rec.id,
            'title': rec.title,
            'action': rec.action,
            'action_level': (
                rec.action_level.value if rec.action_level else 'MONITOR'
            ),
            'impact_area': (
                rec.impact_area.value if rec.impact_area else ''
            ),
            'trigger': rec.trigger,
        }
    except Exception:
        return {}


def _market_opps_from_coverage(market_coverage: dict) -> list:
    """Extract advisory market opportunities from a market_coverage result dict."""
    opps: list[dict] = []

    for candidate in (market_coverage.get('promoted') or []):
        if not isinstance(candidate, dict):
            continue
        symbol = candidate.get('symbol') or ''
        if not symbol:
            continue
        reasons = candidate.get('reasons') or []
        opps.append({
            'symbol': symbol,
            'opportunity_type': 'rebalance_target',
            'suggested_pct': None,
            'suggested_amount': None,
            'reason': (', '.join(reasons) if reasons else
                       f"Promoted {candidate.get('label', '')} candidate."),
        })

    for action in ((market_coverage.get('decision_layer') or {}).get('actions') or []):
        if not isinstance(action, dict):
            continue
        symbol = action.get('symbol') or ''
        if not symbol:
            continue
        opp_type = (
            'underweight_target'
            if str(action.get('action_type', '')).lower() == 'buy'
            else 'rebalance_target'
        )
        opps.append({
            'symbol': symbol,
            'opportunity_type': opp_type,
            'suggested_pct': action.get('suggested_pct'),
            'suggested_amount': action.get('amount'),
            'reason': action.get('reason') or f"Market action: {symbol}.",
        })

    return opps


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Portfolio Automation System',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python main.py                          # Run with defaults
    python main.py --config custom.json     # Use custom config
    python main.py --debug --dry-run        # Debug mode, no side effects
        """
    )
    
    parser.add_argument(
        '--config', '-c',
        type=str,
        default='config.json',
        help='Path to configuration JSON file'
    )
    
    parser.add_argument(
        '--env', '-e',
        type=str,
        default=None,
        help='Path to .env file'
    )

    parser.add_argument(
        '--profile',
        type=str,
        default=None,
        help='Optional structured config profile name (for config/ directory loads)'
    )
    
    parser.add_argument(
        '--debug', '-d',
        action='store_true',
        help='Enable debug logging'
    )
    
    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Run without side effects (no emails, no file writes)'
    )
    
    parser.add_argument(
        '--force-email',
        action='store_true',
        help='Force email send regardless of schedule'
    )
    
    parser.add_argument(
        '--skip-email',
        action='store_true',
        help='Skip email send even if scheduled'
    )

    parser.add_argument(
        '--run-mode',
        choices=['daily', 'weekly', 'monthly'],
        default='daily',
        help=(
            'Execution mode: '
            'daily = quiet, email only on ACTION_REQUIRED; '
            'weekly = always send digest; '
            'monthly = send Capital Deployment Memo'
        )
    )

    parser.add_argument(
        '--llm-provider',
        choices=['ollama', 'anthropic', 'openai'],
        default=None,
        help='Optional provider override for theme-engine LLM tasks during this run'
    )

    return parser.parse_args()


def _build_market_opportunities_md(result: dict) -> str:
    """Build a Markdown report from the market_coverage result dict."""
    lines = [
        "# Market Opportunities",
        "",
        f"**Symbols scanned:** {result.get('symbols_scanned', 0)}  ",
        f"**Symbols with price:** {result.get('symbols_with_price', 0)}",
        "",
    ]
    portfolio_review = result.get("portfolio_review", {})
    decision_layer = result.get("decision_layer", {})
    event_summary = result.get("event_summary", {})
    if event_summary:
        lines += ["## Event Summary", ""]
        for etype, count in sorted(event_summary.items()):
            lines.append(f"- **{etype}**: {count}")
        lines.append("")
    if portfolio_review.get("available"):
        lines += [
            "## Portfolio Review",
            "",
            f"- {portfolio_review.get('summary_line', 'Portfolio review available.')}",
            f"- Existing holding confirmations: {portfolio_review.get('existing_holding_confirmations', 0)}",
            f"- Scanner-confirmed ideas: {portfolio_review.get('scanner_confirmation_count', 0)}",
            f"- New rotation candidates: {portfolio_review.get('new_rotation_candidates', 0)}",
            "",
        ]
    if decision_layer.get("available"):
        lines += [
            "## Portfolio Actions",
            "",
            f"- {decision_layer.get('summary_line', 'Portfolio decision layer available.')}",
            "",
        ]
        for action in decision_layer.get("actions", [])[:8]:
            amount = action.get("suggested_allocation_amount")
            amount_suffix = f" ({amount:,.0f} suggested)" if isinstance(amount, (int, float)) and amount > 0 else ""
            related = f" vs {action.get('related_symbol')}" if action.get("related_symbol") else ""
            lines.append(
                f"- **{action.get('action', 'HOLD')}** `{action.get('symbol', '?')}`"
                f"{related} [{action.get('strategy_type', 'n/a')}]"
                f"{amount_suffix}"
            )
            for reason in action.get("rationale", [])[:3]:
                lines.append(f"  - {reason}")
        lines.append("")
    promoted = result.get("promoted", [])
    if promoted:
        lines += [
            "## Promoted Candidates",
            "",
            "| Rank | Symbol | Score | Label | Events |",
            "| ---- | ------ | ----- | ----- | ------ |",
        ]
        for p in promoted:
            events_str = ", ".join(p.get("events", [])) or "—"
            lines.append(
                f"| {p.get('rank', '?')} "
                f"| {p.get('symbol', '?')} "
                f"| {p.get('score', 0):.1f} "
                f"| {p.get('label', '?')} "
                f"| {events_str} |"
            )
        lines.append("")
        lines += ["## Factor Details", ""]
        for p in promoted:
            sym = p.get("symbol", "?")
            reasons = p.get("reasons", [])
            lines.append(f"**{sym}** (score {p.get('score', 0):.1f}, {p.get('label', '?')})")
            if p.get("portfolio_context", {}).get("action_hint"):
                lines.append(f"  - portfolio fit: {p['portfolio_context']['action_hint']}")
            for r in reasons:
                lines.append(f"  - {r}")
            lines.append("")
    else:
        lines.append("*No candidates promoted in this run.*")
        lines.append("")
    return "\n".join(lines)


def run_portfolio_update(
    config: Config,
    dry_run: bool = False,
    force_email: bool = False,
    skip_email: bool = False,
    run_mode: str = 'daily',
    output_dir: Optional[Path] = None,
    logger=None,
    store=None,
    llm_provider_override: Optional[str] = None,
) -> dict:
    """
    Execute the full portfolio update workflow.

    Args:
        run_mode: 'daily' (alert-only), 'weekly' (digest), or 'monthly' (capital memo).
        output_dir: Where to write output files. Defaults to the path derived from config.

    Returns a result dictionary with status and data.
    """
    result = {
        'success': False,
        'timestamp': get_timestamp(),
        'errors': [],
        'warnings': [],
        'summary': None,
        'recommendations': None,
        'scored_recommendations': None,
        'portfolio_adjustments': None,
        'ml_outputs': None,
        'drawdown_regime': 'normal',
        'contribution_plan': [],
        'compounding_dashboard': None,
        'guardrails': None,
        'scanner': {'candidates': [], 'sleeve_plan': []},
        'theme_engine': {},
        'market_coverage': {
            'enabled': False,
            'promoted': [],
            'event_summary': {},
            'symbols_scanned': 0,
            'symbols_with_price': 0,
            'decision_layer': {'available': False, 'actions': []},
        },
        'data_health': {},
        'degraded_mode': False,
        'degraded_reason': None,
        'data_mode': 'live',
        'decision_plan': [],
        'decision_plan_summary': '',
    }
    # Local variables initialised here so they're always defined
    contribution_plan = []
    dashboard = None
    portfolio_cagr = 0.0

    # Resolve output directory (caller may override via output_dir param)
    if output_dir is None:
        output_dir = Path(config.output.get('csv_path', 'output/snapshot.csv')).parent

    # Wall-clock timer for the official-lane status artifact (non-blocking).
    _run_status_start = time.monotonic()

    try:
        # =====================
        # 1. VALIDATE CONFIG
        # =====================
        logger.info("Validating configuration...")
        config_issues = validate_config(config)
        if config_issues:
            for issue in config_issues:
                logger.warning(f"Config issue: {issue}")
                result['warnings'].append(issue)
        
        # =====================
        # 1b. CASH LEDGER SYNC
        # =====================
        if store is not None:
            try:
                ledger_balance = store.get_cash_balance()
                if ledger_balance is None:
                    store.add_cash_entry("seed", config.cash_available, "initial seed from config")
                    logger.info("Cash ledger seeded: %.2f", config.cash_available)
                else:
                    config.cash_available = ledger_balance
                    logger.info("Cash balance from ledger: %.2f", ledger_balance)
            except Exception as _ledger_err:
                logger.warning("Cash ledger sync failed (non-fatal): %s", _ledger_err)

        # =====================
        # 2. FETCH MARKET DATA
        # =====================
        logger.info("Fetching market data...")
        _av_budget = None
        if _AVDailyBudget is not None:
            try:
                _av_budget = _AVDailyBudget()
                logger.info("AV budget: %s", _av_budget.status_line())
            except Exception as _budget_err:
                logger.warning("AV budget init failed (non-fatal): %s", _budget_err)
        market_client = create_market_client(config.market_data, budget=_av_budget)
        
        holdings, failed_symbols = update_holdings_with_prices(
            config.holdings, market_client
        )
        
        if failed_symbols:
            msg = f"Failed to fetch prices for: {', '.join(failed_symbols)}"
            logger.warning(msg)
            result['warnings'].append(msg)
        
        # Check if we have enough data to proceed
        valid_holdings = [h for h in holdings if h.current_price is not None]
        if not valid_holdings:
            raise RuntimeError("No valid price data available for any holdings")
        
        # =====================
        # 3. LOAD 401(K) DATA
        # =====================
        logger.info("Loading retirement account data...")
        retirement_summary = load_retirement_data(config.retirement_401k)
        
        # Update config retirement balance from loaded data
        config.retirement_401k.balance = retirement_summary.total_balance
        
        # =====================
        # 4. CALCULATE PORTFOLIO
        # =====================
        logger.info("Calculating portfolio metrics...")
        
        # Generate summary
        summary = generate_portfolio_summary(
            holdings=holdings,
            cash_available=config.cash_available,
            target_cash_weight=config.target_cash_weight,
            retirement_401k=config.retirement_401k,
            band_threshold=config.rebalance_rules.band_threshold,
            timestamp=result['timestamp']
        )
        result['summary'] = summary
        
        # Calculate allocations (updates holdings in place)
        holdings, cash_weight, cash_drift = calculate_allocations(
            holdings,
            summary.total_portfolio_value,
            config.cash_available,
            config.target_cash_weight
        )
        
        # Analyze each holding
        analyses = analyze_holdings(
            holdings,
            summary.total_portfolio_value,
            config.rebalance_rules.band_threshold
        )
        
        # =====================
        # 4b. DRAWDOWN TRACKING (Growth Mode)
        # =====================
        growth_cfg = config.growth_mode
        growth_mode_active = growth_cfg.get('mode') == 'accumulation_aggressive'

        drawdown_tracker = DrawdownTracker("data/drawdown_state.json")
        drawdown_state = drawdown_tracker.update(summary.total_portfolio_value)
        drawdown_regime = drawdown_tracker.get_regime(config.drawdown_thresholds)
        suppress_sells = drawdown_tracker.should_suppress_sells()

        logger.info(drawdown_tracker.format_summary(config.drawdown_thresholds))
        result['drawdown_regime'] = drawdown_regime

        # Sync peaks to SQLite state store
        if store is not None and not dry_run:
            try:
                store.upsert_peak('all_time_high', drawdown_state.all_time_high)
                store.upsert_peak('rolling_12m_high', drawdown_state.rolling_12m_high)
            except Exception as _peak_err:
                logger.warning(f"Peak sync to state store failed (non-fatal): {_peak_err}")

        # =====================
        # 4c. GUARDRAILS PRE-FLIGHT
        # =====================
        logger.info("Running pre-flight guardrail checks...")
        try:
            guardrails_result = run_guardrail_checks(
                holdings=holdings,
                total_portfolio=summary.total_portfolio_value,
                concentration_cap=config.concentration_cap,
                leverage_cap=config.leverage_cap,
            )
            result['guardrails'] = guardrails_result.to_dict()
            if guardrails_result.has_violations:
                logger.warning(f"GUARDRAILS: {guardrails_result.summary}")

            # Persist violation age in state store
            if store is not None and not dry_run:
                try:
                    _persist_guardrail_violations(
                        store,
                        result['guardrails'].get('violations', []),
                        logger,
                    )
                except Exception as _viol_err:
                    logger.warning("Violation persistence failed (non-fatal): %s", _viol_err)
        except Exception as _gr_err:
            logger.warning(f"Guardrail check failed (non-fatal): {_gr_err}")

        # =====================
        # 4d. S&P 500 CANDIDATE SCANNER  (gated by config.scanner_enabled)
        # =====================
        scanner_candidates: list = []
        scanner_debug_rows: list = []
        scanner_sleeve_plan: list = []
        _scanner_safe_mode = False
        _scanner_safe_mode_reasons: list[str] = []
        _scanner_stale_cache_days: int | None = None
        _scanner_latency_ms: int | None = None

        # Tracks FMP + fallback state for the run-summary artifact.
        _scanner_meta: dict = {
            'fmp_attempted':  False,
            'fmp_succeeded':  False,
            'fmp_error':      None,
            'fallback_used':  False,
            'watchlist_source': 'none',
        }
        _scanner_sp500_symbols: list[str] = []

        if config.scanner_enabled:
            logger.info(f"SCANNER: Starting {run_mode} scan (FMP budget: {config.fmp_daily_calls_budget} calls/day)...")
            _scanner_started = time.perf_counter()
            try:
                from fmp_client import FMPClient, CallBudgetExceeded, FMPError
                from universe.sp500 import SP500Universe
                from scanner.candidate_scanner import CandidateScanner

                # ── Circuit breaker: skip if FMP had 3+ consecutive auth failures ──
                if store is not None and store.is_subsystem_disabled("fmp"):
                    _sh = store.get_subsystem_health("fmp") or {}
                    raise RuntimeError(
                        f"FMP circuit breaker open (disabled until {_sh.get('disabled_until', '?')}, "
                        f"last error: {_sh.get('last_error', '?')}). "
                        "Skipping scanner. To reset: delete subsystem_health row for 'fmp'."
                    )
                _scanner_meta['fmp_attempted'] = True

                fmp = FMPClient(daily_budget=config.fmp_daily_calls_budget)
                sp500_universe = SP500Universe(fmp)
                candidate_scanner = CandidateScanner(
                    min_mkt_cap=float(config.scanner.get('min_mkt_cap', 5e9)),
                    min_rev_growth=float(config.scanner.get('min_rev_growth', 0.15)),
                    trend_filter_200dma=bool(config.scanner.get('trend_filter_200dma', True)),
                    top_k=int(config.scanner.get('top_k_watchlist', 100)),
                )

                _use_premium = bool(config.scanner.get('use_premium_endpoints', True))

                if run_mode == 'monthly':
                    # Full scan: fetch profiles + metrics + quotes for all S&P 500 symbols
                    logger.info("SCANNER: Monthly full S&P 500 scan — fetching bulk data...")
                    sp500_symbols = sp500_universe.get_symbols()
                    _scanner_sp500_symbols = list(sp500_symbols)
                    if _use_premium:
                        # v4 bulk endpoints (1 call each — requires paid FMP plan)
                        bulk_profiles = fmp.get_bulk_profiles()
                        bulk_metrics = fmp.get_bulk_key_metrics()
                    else:
                        # v3 free-tier: batch profiles + per-ticker fundamentals for top-N
                        _v3_max = int(config.scanner.get('v3_max_symbols', 100))
                        logger.info("SCANNER: v3 free-tier mode (v3_max_symbols=%d)...", _v3_max)
                        bulk_profiles = fmp.get_batch_profiles_v3(sp500_symbols)
                        _min_mkt = float(config.scanner.get('min_mkt_cap', 5e9))
                        _prof_map = {p['symbol']: p for p in bulk_profiles if p.get('symbol')}
                        _qualifying = sorted(
                            [s for s in sp500_symbols
                             if float(_prof_map.get(s, {}).get('mktCap', 0) or 0) >= _min_mkt],
                            key=lambda s: float(_prof_map.get(s, {}).get('mktCap', 0) or 0),
                            reverse=True,
                        )
                        _syms_for_metrics = _qualifying[:_v3_max]
                        logger.info(
                            "SCANNER: %d symbols pass mktCap filter; fetching fundamentals for top %d...",
                            len(_qualifying), len(_syms_for_metrics),
                        )
                        bulk_metrics = fmp.get_fundamentals_v3(_syms_for_metrics)
                    batch_quotes = fmp.get_batch_quotes(sp500_symbols)
                    scanner_candidates, scanner_debug_rows = candidate_scanner.full_scan(
                        sp500_symbols, bulk_profiles, bulk_metrics, batch_quotes
                    )
                    if not dry_run:
                        candidate_scanner.save_watchlist(scanner_candidates)
                    _scanner_meta['fmp_succeeded'] = True
                    _scanner_meta['watchlist_source'] = 'fmp'
                    logger.info(
                        f"SCANNER: {len(scanner_candidates)} candidates "
                        f"({fmp.calls_today} API calls used today)"
                    )

                elif run_mode == 'weekly':
                    # Refresh top-k: reload watchlist + fresh metrics + fresh quotes
                    watchlist = candidate_scanner.load_watchlist()
                    if not watchlist:
                        logger.warning(
                            "SCANNER: No watchlist cached — running full scan "
                            "(run monthly mode first to pre-warm cache)"
                        )
                        sp500_symbols = sp500_universe.get_symbols()
                        _scanner_sp500_symbols = list(sp500_symbols)
                        if _use_premium:
                            bulk_profiles = fmp.get_bulk_profiles()
                            bulk_metrics = fmp.get_bulk_key_metrics()
                        else:
                            _v3_max = int(config.scanner.get('v3_max_symbols', 100))
                            bulk_profiles = fmp.get_batch_profiles_v3(sp500_symbols)
                            _min_mkt = float(config.scanner.get('min_mkt_cap', 5e9))
                            _prof_map = {p['symbol']: p for p in bulk_profiles if p.get('symbol')}
                            _qualifying = sorted(
                                [s for s in sp500_symbols
                                 if float(_prof_map.get(s, {}).get('mktCap', 0) or 0) >= _min_mkt],
                                key=lambda s: float(_prof_map.get(s, {}).get('mktCap', 0) or 0),
                                reverse=True,
                            )
                            bulk_metrics = fmp.get_fundamentals_v3(_qualifying[:_v3_max])
                        batch_quotes = fmp.get_batch_quotes(sp500_symbols)
                        scanner_candidates, scanner_debug_rows = candidate_scanner.full_scan(
                            sp500_symbols, bulk_profiles, bulk_metrics, batch_quotes
                        )
                    else:
                        logger.info(f"SCANNER: Weekly refresh of {len(watchlist)} cached candidates...")
                        top100_symbols = [c['symbol'] for c in watchlist]
                        if _use_premium:
                            bulk_metrics = fmp.get_bulk_key_metrics()
                        else:
                            bulk_metrics = fmp.get_fundamentals_v3(top100_symbols)
                        batch_quotes = fmp.get_batch_quotes(top100_symbols)
                        scanner_candidates, scanner_debug_rows = candidate_scanner.weekly_refresh(
                            watchlist, bulk_metrics, batch_quotes
                        )
                    if not dry_run:
                        candidate_scanner.save_watchlist(scanner_candidates)
                    _scanner_meta['fmp_succeeded'] = True
                    _scanner_meta['watchlist_source'] = 'fmp'
                    logger.info(
                        f"SCANNER: {len(scanner_candidates)} candidates after weekly refresh "
                        f"({fmp.calls_today} API calls used today)"
                    )

                else:  # daily
                    # Quote refresh only: load watchlist + batch quotes for top-k
                    # API calls: batch quotes for top100 (≈1–2) = ~2 total
                    watchlist = candidate_scanner.load_watchlist()
                    if not watchlist:
                        logger.warning(
                            "SCANNER: No watchlist found — attempting fallback watchlist. "
                            "Run weekly or monthly mode to build a scored FMP watchlist."
                        )
                        # ── Fallback: build and persist a default symbol list ────────
                        try:
                            from scanner.fallback_watchlist import FallbackWatchlist as _FBW
                            _fb = _FBW(config.scanner)
                            if _fb.enabled:
                                _theme_candidates_path = str(
                                    Path(config.theme_engine.get("output_dir", "outputs/latest"))
                                    / "watch_candidates.json"
                                )
                                watchlist = _fb.build(
                                    theme_candidates_path=_theme_candidates_path,
                                )
                                if not dry_run:
                                    _fb.save(watchlist)
                                _has_themes = any(
                                    c.get("watchlist_source") == "fallback+themes"
                                    for c in watchlist
                                )
                                _scanner_meta['fallback_used'] = True
                                _scanner_meta['watchlist_source'] = (
                                    'fallback+themes' if _has_themes else 'fallback'
                                )
                                logger.warning(
                                    "SCANNER: fallback watchlist activated — "
                                    "%d symbols (source: %s)",
                                    len(watchlist),
                                    _scanner_meta['watchlist_source'],
                                )
                        except Exception as _fb_err:
                            logger.warning(
                                "SCANNER: fallback watchlist failed (non-fatal): %s", _fb_err
                            )
                    else:
                        _scanner_meta['watchlist_source'] = (
                            watchlist[0].get('watchlist_source', 'fmp_cached')
                            if watchlist else 'fmp_cached'
                        )

                    if watchlist:
                        logger.info(
                            "SCANNER: Daily quote refresh for %d watchlist candidates...",
                            len(watchlist),
                        )
                        top100_symbols = [c['symbol'] for c in watchlist]
                        try:
                            batch_quotes = fmp.get_batch_quotes(top100_symbols)
                        except Exception as _q_err:
                            logger.warning(
                                "SCANNER: quote fetch failed (%s) — using stale prices", _q_err
                            )
                            batch_quotes = {}
                        scanner_candidates, _ = candidate_scanner.daily_refresh(
                            watchlist, batch_quotes
                        )
                        if not _scanner_meta['fallback_used']:
                            _scanner_meta['fmp_succeeded'] = True
                    logger.info(
                        f"SCANNER: {len(scanner_candidates)} candidates "
                        f"({fmp.calls_today} API calls used today)"
                    )

                result['scanner']['candidates'] = scanner_candidates

                # Sleeve allocation is handled after degraded-mode evaluation.
                if False and config.sleeve_enabled and scanner_candidates:
                    allocator = SpecSleeveAllocator(
                        sleeve_total_max=float(config.speculative_sleeve.get('max_total', 0.10)),
                        max_per_stock=float(config.speculative_sleeve.get('max_per_position', 0.05)),
                        max_new_positions_per_month=int(
                            config.speculative_sleeve.get('max_new_positions_per_month', 1)
                        ),
                    )
                    scanner_sleeve_plan = allocator.allocate(
                        candidates=scanner_candidates[:20],
                        holdings=holdings,
                        total_portfolio=summary.total_portfolio_value,
                        available_cash=config.cash_available,
                        drawdown_regime=drawdown_regime,
                    )
                    result['scanner']['sleeve_plan'] = scanner_sleeve_plan
                    logger.info(f"SCANNER: Sleeve plan — {len(scanner_sleeve_plan)} recommendations")

                if store is not None:
                    try:
                        store.record_subsystem_success("fmp")
                    except Exception:
                        pass

            except CallBudgetExceeded as _budget_err:
                msg = f"SCANNER: FMP daily budget exceeded — {_budget_err}"
                logger.warning(msg)
                result['warnings'].append(msg)
                _scanner_meta['fmp_error'] = str(_budget_err)
                # ── Fallback on budget exhaustion ──────────────────────────
                try:
                    from scanner.fallback_watchlist import FallbackWatchlist as _FBW
                    _fb = _FBW(config.scanner)
                    if _fb.enabled:
                        _theme_candidates_path = str(
                            Path(config.theme_engine.get("output_dir", "outputs/latest"))
                            / "watch_candidates.json"
                        )
                        _fb_candidates = _fb.build(theme_candidates_path=_theme_candidates_path)
                        if not dry_run:
                            _fb.save(_fb_candidates)
                        _has_themes = any(
                            c.get("watchlist_source") == "fallback+themes"
                            for c in _fb_candidates
                        )
                        _scanner_meta['fallback_used'] = True
                        _scanner_meta['watchlist_source'] = (
                            'fallback+themes' if _has_themes else 'fallback'
                        )
                        scanner_candidates = _fb_candidates
                        result['scanner']['candidates'] = scanner_candidates
                        logger.warning(
                            "SCANNER: fallback watchlist activated after budget exhaustion "
                            "— %d symbols", len(scanner_candidates)
                        )
                except Exception as _fb_err:
                    logger.warning(
                        "SCANNER: fallback watchlist failed (non-fatal): %s", _fb_err
                    )
            except FMPError as _fmp_err:
                msg = f"SCANNER: FMP API error — {_fmp_err}"
                logger.warning(msg)
                result['warnings'].append(msg)
                _scanner_meta['fmp_error'] = str(_fmp_err)
                if store is not None:
                    try:
                        store.record_subsystem_failure("fmp", str(_fmp_err))
                    except Exception:
                        pass
                # ── Fallback on FMP auth / API error ──────────────────────
                try:
                    from scanner.fallback_watchlist import FallbackWatchlist as _FBW
                    _fb = _FBW(config.scanner)
                    if _fb.enabled:
                        _theme_candidates_path = str(
                            Path(config.theme_engine.get("output_dir", "outputs/latest"))
                            / "watch_candidates.json"
                        )
                        _fb_candidates = _fb.build(theme_candidates_path=_theme_candidates_path)
                        if not dry_run:
                            _fb.save(_fb_candidates)
                        _has_themes = any(
                            c.get("watchlist_source") == "fallback+themes"
                            for c in _fb_candidates
                        )
                        _scanner_meta['fallback_used'] = True
                        _scanner_meta['watchlist_source'] = (
                            'fallback+themes' if _has_themes else 'fallback'
                        )
                        scanner_candidates = _fb_candidates
                        result['scanner']['candidates'] = scanner_candidates
                        logger.warning(
                            "SCANNER: fallback watchlist activated after FMP error "
                            "— %d symbols (source: %s)",
                            len(scanner_candidates),
                            _scanner_meta['watchlist_source'],
                        )
                except Exception as _fb_err:
                    logger.warning(
                        "SCANNER: fallback watchlist failed (non-fatal): %s", _fb_err
                    )
            except Exception as _scan_err:
                msg = f"SCANNER: Non-fatal error — {_scan_err}"
                logger.warning(msg)
                result['warnings'].append(msg)
                if not _scanner_meta['fmp_error']:
                    _scanner_meta['fmp_error'] = str(_scan_err)
                # ── Fallback on unexpected errors (incl. circuit breaker) ──
                try:
                    from scanner.fallback_watchlist import FallbackWatchlist as _FBW
                    _fb = _FBW(config.scanner)
                    if _fb.enabled:
                        _theme_candidates_path = str(
                            Path(config.theme_engine.get("output_dir", "outputs/latest"))
                            / "watch_candidates.json"
                        )
                        _fb_candidates = _fb.build(theme_candidates_path=_theme_candidates_path)
                        if not dry_run:
                            _fb.save(_fb_candidates)
                        _has_themes = any(
                            c.get("watchlist_source") == "fallback+themes"
                            for c in _fb_candidates
                        )
                        _scanner_meta['fallback_used'] = True
                        _scanner_meta['watchlist_source'] = (
                            'fallback+themes' if _has_themes else 'fallback'
                        )
                        scanner_candidates = _fb_candidates
                        result['scanner']['candidates'] = scanner_candidates
                        logger.warning(
                            "SCANNER: fallback watchlist activated — %d symbols (source: %s)",
                            len(scanner_candidates),
                            _scanner_meta['watchlist_source'],
                        )
                except Exception as _fb_err:
                    logger.warning(
                        "SCANNER: fallback watchlist failed (non-fatal): %s", _fb_err
                    )

            _scanner_latency_ms = int((time.perf_counter() - _scanner_started) * 1000)
            _scanner_stale_cache_days = stale_cache_days_for_path("data/fmp_cache/top100_watchlist.json")
            _scanner_data_health = build_data_health_context(
                fmp_attempted=_scanner_meta.get('fmp_attempted', False),
                fmp_succeeded=_scanner_meta.get('fmp_succeeded', False),
                fmp_error=_scanner_meta.get('fmp_error'),
                fallback_used=_scanner_meta.get('fallback_used', False),
                watchlist_source=_scanner_meta.get('watchlist_source', 'none'),
                data_latency_ms=_scanner_latency_ms,
                stale_cache_days=_scanner_stale_cache_days,
            )
            scanner_candidates = _annotate_scanner_candidates_for_data_mode(
                scanner_candidates,
                _scanner_data_health,
            )
            result['scanner']['candidates'] = scanner_candidates

            if _scanner_data_health.get("data_fallback_triggered"):
                logger.warning(
                    "DATA fallback: FMP unavailable -> %s (reason=%s)",
                    _scanner_meta.get("watchlist_source", "fallback"),
                    _scanner_data_health.get("degraded_reason") or "unknown",
                )

            if _scanner_data_health.get("degraded_mode"):
                if not scanner_candidates:
                    _scanner_safe_mode_reasons.append("empty_dataset")
                    msg = (
                        "SCANNER SAFE MODE: degraded data produced an empty candidate set - "
                        "speculative downstream allocation suppressed"
                    )
                    logger.warning(msg)
                    result['warnings'].append(msg)
                elif len(scanner_candidates) < MIN_TRUSTED_DATASET_SIZE:
                    _scanner_safe_mode_reasons.append("small_dataset")
                    msg = (
                        f"SCANNER SAFE MODE: degraded data produced only {len(scanner_candidates)} "
                        "candidates - speculative downstream allocation suppressed"
                    )
                    logger.warning(msg)
                    result['warnings'].append(msg)
                if (
                    _scanner_stale_cache_days is not None
                    and _scanner_stale_cache_days > DEFAULT_STALE_DAYS
                ):
                    _scanner_safe_mode_reasons.append("stale_cache")
                    msg = (
                        f"SCANNER SAFE MODE: fallback cache is {_scanner_stale_cache_days} days old - "
                        "speculative downstream allocation suppressed"
                    )
                    logger.warning(msg)
                    result['warnings'].append(msg)
            _scanner_safe_mode = bool(_scanner_safe_mode_reasons)

            if config.sleeve_enabled and scanner_candidates and not _scanner_safe_mode:
                from sleeve.spec_sleeve_allocator import SpecSleeveAllocator

                allocator = SpecSleeveAllocator(
                    sleeve_total_max=float(config.speculative_sleeve.get('max_total', 0.10)),
                    max_per_stock=float(config.speculative_sleeve.get('max_per_position', 0.05)),
                    max_new_positions_per_month=int(
                        config.speculative_sleeve.get('max_new_positions_per_month', 1)
                    ),
                )
                scanner_sleeve_plan = allocator.allocate(
                    candidates=scanner_candidates[:20],
                    holdings=holdings,
                    total_portfolio=summary.total_portfolio_value,
                    available_cash=config.cash_available,
                    drawdown_regime=drawdown_regime,
                )
                result['scanner']['sleeve_plan'] = scanner_sleeve_plan
                logger.info("SCANNER: Sleeve plan - %d recommendations", len(scanner_sleeve_plan))
            elif config.sleeve_enabled and _scanner_safe_mode:
                logger.info(
                    "SCANNER: sleeve allocation skipped due to degraded safe mode (%s)",
                    ", ".join(_scanner_safe_mode_reasons),
                )

            _scanner_meta.update(_scanner_data_health)
            _scanner_meta["safe_mode"] = _scanner_safe_mode
            _scanner_meta["safe_mode_reasons"] = list(_scanner_safe_mode_reasons)
            result['scanner']['safe_mode'] = _scanner_safe_mode
            result['scanner']['safe_mode_reasons'] = list(_scanner_safe_mode_reasons)
            result['scanner']['meta'] = _scanner_meta

        else:
            logger.debug("SCANNER: disabled in config (scanner.enabled=false)")

        # =====================
        # 4d_ext. MARKET COVERAGE (gated by config.market_universe_enabled)
        # Broad universe shallow scan → event detection → ranking → promotion.
        # Runs AFTER the FMP scanner so batch quotes may be partially cached.
        # =====================
        _market_coverage_result: dict = {
            "enabled": False,
            "promoted": [],
            "event_summary": {},
            "symbols_scanned": 0,
            "symbols_with_price": 0,
            "decision_layer": {"available": False, "actions": []},
        }
        if config.market_universe_enabled:
            try:
                from market_universe import get_all_symbols
                from universal_scanner import UniversalScanner
                from event_detection import detect_events
                from opportunity_ranker import rank_opportunities
                from portfolio_decision_engine import generate_portfolio_actions
                from promotion_engine import build_portfolio_review, promote_candidates
                from fmp_client import FMPClient, CallBudgetExceeded, FMPError

                _mu_cfg = config.market_universe
                _us_cfg = config.universal_scanner_cfg
                _or_cfg = config.opportunity_ranker_cfg
                _pe_cfg = config.promotion_engine_cfg

                logger.info(
                    "MARKET COVERAGE: building universe (groups=%s)...",
                    _mu_cfg.get("groups", ["nasdaq100", "sector_etfs"]),
                )

                # Portfolio symbols for optional 'portfolio' group
                _portfolio_symbols = [h.symbol for h in holdings]

                # S&P 500 symbols: reuse if scanner already has them, else skip
                _sp500_for_universe = list(_scanner_sp500_symbols)
                if "sp500" in _mu_cfg.get("groups", []):
                    if not _sp500_for_universe:
                        try:
                            _fmp_uni = FMPClient(daily_budget=config.fmp_daily_calls_budget)
                            _sp500_for_universe = [
                                c["symbol"]
                                for c in _fmp_uni.get_sp500_constituents()
                                if c.get("symbol")
                            ]
                        except Exception as _sp_err:
                            logger.warning(
                                "MARKET COVERAGE: sp500 fetch failed (%s) — skipping sp500 group",
                                _sp_err,
                            )

                universe_symbols = get_all_symbols(
                    {"market_universe": _mu_cfg},
                    sp500_symbols=_sp500_for_universe,
                    portfolio_symbols=_portfolio_symbols,
                )
                logger.info(
                    "MARKET COVERAGE: %d unique symbols in universe", len(universe_symbols)
                )

                # Fetch batch quotes for the universe symbols via FMP
                # Most S&P 500 symbols will be cache-hits from step 4d.
                _mc_batch_quotes: dict = {}
                try:
                    _fmp_mc = FMPClient(daily_budget=config.fmp_daily_calls_budget)
                    _mc_batch_quotes = _fmp_mc.get_batch_quotes(universe_symbols)
                    logger.info(
                        "MARKET COVERAGE: %d quotes fetched (%d budget used today)",
                        len(_mc_batch_quotes),
                        _fmp_mc.calls_today,
                    )
                except CallBudgetExceeded as _mc_budget_err:
                    logger.warning(
                        "MARKET COVERAGE: FMP budget exceeded for quote fetch — "
                        "proceeding with empty quotes: %s", _mc_budget_err
                    )
                except FMPError as _mc_fmp_err:
                    logger.warning(
                        "MARKET COVERAGE: FMP error during quote fetch — "
                        "proceeding with empty quotes: %s", _mc_fmp_err
                    )

                # Run the pipeline
                scanner_inst = UniversalScanner(_us_cfg)
                scan_results = scanner_inst.scan(_mc_batch_quotes, symbols=universe_symbols)

                _et_cfg = _us_cfg.get("event_thresholds", {})
                market_events = detect_events(scan_results, config=_et_cfg)

                ranked_opps = rank_opportunities(scan_results, market_events, config=_or_cfg)
                promoted_candidates = promote_candidates(ranked_opps, config=_pe_cfg)
                portfolio_review = build_portfolio_review(
                    promoted_candidates,
                    holdings=holdings,
                    scanner_candidates=scanner_candidates,
                    cash_available=config.cash_available,
                )
                decision_layer = generate_portfolio_actions(
                    current_holdings=holdings,
                    opportunities=promoted_candidates,
                    portfolio_value=summary.total_portfolio_value,
                    cash_available=config.cash_available,
                    context={
                        "drawdown_regime": drawdown_regime,
                        "regime_label": drawdown_regime,
                        "degraded_mode": bool(_scanner_meta.get("fallback_used")),
                        "degraded_reason": _scanner_meta.get("fmp_error"),
                    },
                )

                # Event summary: count per type
                _event_summary: dict = {}
                for _ev in market_events:
                    _et_val = _ev.event_type.value
                    _event_summary[_et_val] = _event_summary.get(_et_val, 0) + 1

                _market_coverage_result = {
                    "enabled": True,
                    "promoted": [p.to_dict() for p in promoted_candidates],
                    "event_summary": _event_summary,
                    "symbols_scanned": len(scan_results),
                    "symbols_with_price": sum(1 for sr in scan_results if sr.has_price),
                    "portfolio_review": portfolio_review,
                    "decision_layer": decision_layer,
                }
                result["market_coverage"] = _market_coverage_result

                logger.info(
                    "MARKET COVERAGE: %d promoted candidates, %d events (%s)",
                    len(promoted_candidates),
                    len(market_events),
                    ", ".join(f"{k}:{v}" for k, v in _event_summary.items()),
                )

                # Record promoted candidates to coverage history for evaluation
                if not dry_run:
                    try:
                        from coverage_tracker import append_coverage_run as _append_cov
                        _cov_written = _append_cov(
                            run_id=f"{date.today().isoformat()}_{run_mode}",
                            promoted=promoted_candidates,
                            scan_by_symbol=scan_results,
                            drawdown_regime=drawdown_regime,
                        )
                        logger.info(
                            "MARKET COVERAGE: recorded %d candidates to coverage history",
                            _cov_written,
                        )
                    except Exception as _cov_err:
                        logger.warning(
                            "MARKET COVERAGE: coverage_tracker write failed (non-fatal): %s",
                            _cov_err,
                        )

                # Log finalized portfolio actions to trade_events.jsonl
                if not dry_run and decision_layer.get("actions"):
                    try:
                        from trade_event_logger import append_trade_events as _append_tevents
                        _te_written = _append_tevents(
                            actions=decision_layer["actions"],
                            run_id=f"{date.today().isoformat()}_{run_mode}",
                            run_mode=run_mode,
                            portfolio_value=summary.total_portfolio_value,
                            cash_available=config.cash_available,
                            drawdown_regime=drawdown_regime,
                            degraded_mode=bool(_scanner_meta.get("fallback_used")),
                            degraded_reason=_scanner_meta.get("fmp_error"),
                        )
                        if _te_written:
                            logger.info(
                                "MARKET COVERAGE: logged %d trade events", _te_written
                            )
                    except Exception as _te_err:
                        logger.warning(
                            "MARKET COVERAGE: trade_event_logger write failed (non-fatal): %s",
                            _te_err,
                        )

                # Write output files
                if not dry_run:
                    try:
                        import json as _json_mc
                        _mc_out = output_dir / "market_opportunities.json"
                        _mc_out.write_text(
                            _json_mc.dumps(_market_coverage_result, indent=2),
                            encoding="utf-8",
                        )
                        _mc_md_out = output_dir / "market_opportunities.md"
                        _mc_md_out.write_text(
                            _build_market_opportunities_md(_market_coverage_result),
                            encoding="utf-8",
                        )
                        logger.info(
                            "MARKET COVERAGE: wrote %s and %s",
                            _mc_out.name, _mc_md_out.name,
                        )
                    except Exception as _mc_write_err:
                        logger.warning(
                            "MARKET COVERAGE: output write failed (non-fatal): %s",
                            _mc_write_err,
                        )

            except Exception as _mc_err:
                logger.warning(
                    "MARKET COVERAGE: step failed (non-fatal): %s", _mc_err
                )
                result["market_coverage"] = _market_coverage_result
        else:
            logger.debug(
                "MARKET COVERAGE: disabled in config (market_universe.enabled=false)"
            )
            result["market_coverage"] = _market_coverage_result

        # =====================
        # 4e. THEME ENGINE (gated by config.theme_engine_enabled)
        # =====================
        _te_result: dict = {}
        _ew_result: dict = {}
        if config.theme_engine_enabled:
            try:
                from theme_engine.__main__ import run as _run_theme_engine
                _te_result = _run_theme_engine(
                    mode=run_mode,
                    config=config,
                    dry_run=dry_run,
                    root=".",
                    provider_override=llm_provider_override,
                )
                logger.info(
                    "THEME ENGINE: %d themes, %d watch candidates",
                    len(_te_result.get("themes", [])),
                    len(_te_result.get("watch_candidates", [])),
                )
                _te_meta = _te_result.get("llm_metadata", {}) if isinstance(_te_result, dict) else {}
                if _te_meta:
                    logger.info(
                        "THEME ENGINE LLM: resolved_provider=%s model=%s base_url=%s llm_fallback=%s data_fallback=%s",
                        _te_meta.get("resolved_provider", "(unknown)"),
                        _te_meta.get("model", "(unset)"),
                        _te_meta.get("base_url", "(n/a)"),
                        _te_meta.get("llm_fallback_triggered", _te_meta.get("fallback_triggered", False)),
                        _te_meta.get("data_fallback_triggered", False),
                    )
                    result["theme_engine"] = {"llm_metadata": dict(_te_meta)}
                # Monthly: apply theme boosts to scanner candidates
                if (
                    run_mode == "monthly"
                    and result["scanner"]["candidates"]
                ):
                    from scanner.candidate_scanner import apply_theme_boosts
                    _signals_path = str(
                        Path(config.theme_engine.get("output_dir", "outputs/latest"))
                        / "theme_signals.json"
                    )
                    result["scanner"]["candidates"] = apply_theme_boosts(
                        result["scanner"]["candidates"],
                        _signals_path,
                        config.theme_engine,
                    )
                    result["scanner"]["candidates"] = _annotate_scanner_candidates_for_data_mode(
                        result["scanner"]["candidates"],
                        result.get("data_health") or result["scanner"].get("meta", {}),
                    )
                    # Refresh local variable used for file output
                    scanner_candidates = result["scanner"]["candidates"]
                    logger.info(
                        "THEME ENGINE: applied boosts to %d scanner candidates",
                        len(scanner_candidates),
                    )

                # ── Extended watchlist promotion ──────────────────────────────
                _ew_cfg = getattr(config, 'extended_watchlist', None) or {}
                if isinstance(_ew_cfg, dict) and _ew_cfg.get('enabled', True):
                    try:
                        from watchlist_scanner.extended_watchlist import ExtendedWatchlist
                        _ew = ExtendedWatchlist(
                            db_path=_ew_cfg.get('db_path', 'data/portfolio.db'),
                            ttl_days=int(_ew_cfg.get('ttl_days', 7)),
                            max_symbols=int(_ew_cfg.get('max_symbols', 3)),
                            confidence_threshold=float(
                                _ew_cfg.get('confidence_threshold', 0.80)
                            ),
                        )
                        _ws_static = (
                            (getattr(config, 'watchlist_scanner', None) or {})
                            .get('watchlist', [])
                        )
                        _ew_result = _ew.evaluate_candidates(
                            candidates=_te_result.get('watch_candidates', []),
                            static_watchlist=_ws_static,
                        )
                        if _ew_result.get('promoted'):
                            logger.info(
                                "EXTENDED WATCHLIST: promoted %s",
                                ', '.join(_ew_result['promoted']),
                            )
                        if _ew_result.get('expired'):
                            logger.info(
                                "EXTENDED WATCHLIST: expired %s",
                                ', '.join(_ew_result['expired']),
                            )
                    except Exception as _ew_err:
                        logger.warning(
                            "EXTENDED WATCHLIST: promotion failed (non-fatal): %s", _ew_err
                        )
            except Exception as _te_err:
                logger.warning("THEME ENGINE: non-fatal error — %s", _te_err)
        else:
            logger.debug("THEME ENGINE: disabled in config (theme_engine.enabled=false)")

        # =====================
        # 4f. WATCHLIST SCANNER (gated by watchlist_scanner.enabled)
        # =====================
        _ws_cfg = getattr(config, 'watchlist_scanner', None) or {}
        _ws_result: dict = {}
        if isinstance(_ws_cfg, dict) and _ws_cfg.get('enabled', False):
            try:
                from watchlist_scanner.__main__ import run as _run_watchlist_scanner
                _ws_result = _run_watchlist_scanner(
                    config=_ws_cfg,
                    dry_run=dry_run,
                    output_dir=_ws_cfg.get('output_dir', 'outputs/latest'),
                    extended_watchlist_config=getattr(config, 'extended_watchlist', None),
                    portfolio_context={
                        'holdings': holdings,
                        'cash_available': config.cash_available,
                        'target_cash_weight': config.target_cash_weight,
                    },
                )
                logger.info(
                    "WATCHLIST SCANNER: %d signals, %d alerts (%d API calls used today)",
                    len(_ws_result.get('results', [])),
                    len(_ws_result.get('alerts', [])),
                    _ws_result.get('calls_used', 0),
                )
            except Exception as _ws_err:
                logger.warning("WATCHLIST SCANNER: non-fatal error — %s", _ws_err)
        else:
            logger.debug("WATCHLIST SCANNER: disabled in config")
        _ws_scan_status = None
        if isinstance(_ws_result, dict):
            _ws_scan_status = ((_ws_result.get("scan_summary") or {}).get("scan_status"))
        _run_data_health = build_data_health_context(
            fmp_attempted=_scanner_meta.get('fmp_attempted', False),
            fmp_succeeded=_scanner_meta.get('fmp_succeeded', False),
            fmp_error=_scanner_meta.get('fmp_error'),
            fallback_used=_scanner_meta.get('fallback_used', False),
            watchlist_source=_scanner_meta.get('watchlist_source', 'none'),
            scan_status=_ws_scan_status,
            data_latency_ms=_scanner_latency_ms,
            stale_cache_days=_scanner_stale_cache_days,
        )
        result["data_health"] = _run_data_health
        result["degraded_mode"] = _run_data_health["degraded_mode"]
        result["degraded_reason"] = _run_data_health["degraded_reason"]
        result["data_mode"] = _run_data_health["data_mode"]
        if result.get("scanner", {}).get("meta"):
            result["scanner"]["meta"]["run_data_mode"] = _run_data_health["data_mode"]
            result["scanner"]["meta"]["run_degraded_mode"] = _run_data_health["degraded_mode"]
            result["scanner"]["meta"]["run_degraded_reason"] = _run_data_health["degraded_reason"]
            result["scanner"]["meta"]["data_sources_used"] = _run_data_health["data_sources_used"]
        logger.info("DATA mode summary: %s", summarize_data_health(_run_data_health))

        # ── Run summary artifact (scanner + scraped-intel observability) ──────────
        # Placed here (after 4f) so _ws_result.scraped_intel_summary is available.
        if config.scanner_enabled:
            try:
                from scraped_intel.run_summary import build_run_summary as _build_run_summary
                _si_stats: dict = {}
                if isinstance(_ws_result, dict):
                    _si_stats = dict(_ws_result.get("scraped_intel_summary") or {})
                    _si_stats["scan_status"] = _ws_scan_status
                if _scanner_latency_ms is not None:
                    _si_stats["data_latency_ms"] = _scanner_latency_ms
                _build_run_summary(
                    run_mode=run_mode,
                    fmp_attempted=_scanner_meta.get('fmp_attempted', False),
                    fmp_succeeded=_scanner_meta.get('fmp_succeeded', False),
                    fmp_error=_scanner_meta.get('fmp_error'),
                    fallback_used=_scanner_meta.get('fallback_used', False),
                    watchlist_source=_scanner_meta.get('watchlist_source', 'none'),
                    symbols_processed=[c['symbol'] for c in scanner_candidates],
                    scraped_intel_stats=_si_stats,
                    market_regime=(_ws_result.get("market_regime") if isinstance(_ws_result, dict) else None),
                    market_coverage=result.get("market_coverage"),
                    output_dir='outputs/latest',
                    dry_run=dry_run,
                )
            except Exception as _sum_err:
                logger.debug("SCANNER: run summary generation failed (non-fatal): %s", _sum_err)

        # ── Data Quality Monitor (observe-only) ──────────────────────────────
        try:
            from portfolio_automation.data_quality_monitor import (
                evaluate_data_quality as _eval_dq,
                write_data_quality_report as _write_dq,
            )
            _dq_records: list = []
            if isinstance(_ws_result, dict):
                _dq_records = [
                    r for r in _ws_result.get("results", [])
                    if isinstance(r, dict)
                ]
            _dq_summary = _eval_dq(_dq_records)
            if not dry_run:
                _write_dq(_dq_summary)
            logger.info(
                "DATA QUALITY: %s (healthy=%d warning=%d critical=%d)",
                _dq_summary.summary_line,
                _dq_summary.healthy_symbols,
                _dq_summary.warning_symbols,
                _dq_summary.critical_symbols,
            )
        except Exception as _dq_err:
            logger.warning("DATA QUALITY MONITOR: non-fatal error — %s", _dq_err)

        # =====================
        # 5. GENERATE RECOMMENDATIONS
        # =====================
        logger.info("Generating recommendations...")
        
        # Build context notes
        context_notes = []
        should_send, is_weekly, is_annual = should_send_report(
            config.schedule, summary
        )
        
        if is_weekly:
            context_notes.append("Weekly summary report")
        if is_annual:
            context_notes.append("Annual portfolio review")
        
        recommendations = generate_recommendations(
            holdings=holdings,
            analyses=analyses,
            summary=summary,
            rules=config.rebalance_rules,
            cash_available=config.cash_available,
            cash_weight=cash_weight,
            target_cash_weight=config.target_cash_weight,
            context_notes=context_notes
        )
        result['recommendations'] = recommendations
        
        # =====================
        # 5b. GENERATE SCORED FINANCE RECOMMENDATIONS
        # =====================
        logger.info("Generating scored finance recommendations...")
        
        finance_config = FinanceConfig.from_investor_config(config)
        history_store = FinanceHistoryStore("data/finance_history.json")
        finance_analyzer = FinanceAnalyzer(finance_config, history_store)
        
        scored_recommendations = finance_analyzer.analyze(
            summary=summary,
            holdings=holdings,
            analyses=analyses,
            current_savings_rate=None,  # Can be provided if tracked
            budget_variances=None  # Can be provided if tracked
        )
        result['scored_recommendations'] = scored_recommendations
        
        # =====================
        # 5c. GENERATE CONSOLIDATED PORTFOLIO ADJUSTMENTS
        # =====================
        logger.info("Generating consolidated portfolio adjustments...")
        
        monthly_contribution = config.monthly_contribution
        has_regular_contributions = config.has_regular_contributions

        portfolio_adjustments = generate_portfolio_adjustments(
            holdings=holdings,
            analyses=analyses,
            total_portfolio=summary.total_portfolio_value,
            cash_available=config.cash_available,
            target_cash_pct=config.target_cash_weight,
            band=config.rebalance_rules.band_threshold,
            monthly_expenses=config.investor.monthly_expenses,
            monthly_contribution=monthly_contribution,
            has_regular_contributions=has_regular_contributions,
            growth_mode=growth_mode_active,
            suppress_sells=suppress_sells,
            concentration_cap=config.concentration_cap,
            leverage_cap=config.leverage_cap,
            is_taxable=config.is_taxable_account,
        )
        result['portfolio_adjustments'] = portfolio_adjustments

        # =====================
        # 5e. CONTRIBUTION PLAN (Growth Mode)
        # =====================
        contribution_plan = []
        if growth_mode_active and monthly_contribution > 0:
            logger.info("Computing contribution allocation plan...")
            contribution_engine = ContributionEngine(
                concentration_cap=config.concentration_cap,
                leverage_cap=config.leverage_cap,
            )
            contribution_plan = contribution_engine.allocate(
                holdings=holdings,
                analyses=analyses,
                total_portfolio=summary.total_portfolio_value,
                monthly_contribution=monthly_contribution,
                drawdown_regime=drawdown_regime,
            )
            result['contribution_plan'] = contribution_plan

            if contribution_plan:
                print("\n" + "=" * 62)
                print("  CONTRIBUTION PLAN — Where to Deploy Next Contribution")
                print("=" * 62)
                for alloc in contribution_plan:
                    print(
                        f"  {alloc.symbol:<6}  ${alloc.recommended_dollars:>8,.2f}  "
                        f"{alloc.drift:+.1%} drift  {alloc.reason}"
                    )
                total_planned = sum(a.recommended_dollars for a in contribution_plan)
                print(f"  {'TOTAL':<6}  ${total_planned:>8,.2f}")
                print("=" * 62)

        # =====================
        # 5f. PROJECTIONS / COMPOUNDING DASHBOARD (Growth Mode)
        # =====================
        dashboard = None
        if growth_mode_active:
            logger.info("Computing compounding projections...")
            portfolio_cagr = compute_portfolio_cagr(  # also stored at function scope
                holdings=holdings,
                total_portfolio=summary.total_portfolio_value,
                expected_returns=config.expected_returns,
                target_cash_weight=config.target_cash_weight,
            )
            dashboard = compute_compounding_dashboard(
                current_value=summary.total_portfolio_value,
                monthly_contribution=monthly_contribution,
                expected_cagr=portfolio_cagr,
                drawdown_pct=drawdown_state.drawdown_from_12m_high,
            )
            result['compounding_dashboard'] = dashboard
            print("\n" + format_dashboard_text(dashboard))
        
        # =====================
        # 5d. ML ADVISOR (Pattern Recognition)
        # =====================
        ml_advisor_enabled = config.ml_advisor.get('enabled', False)

        ml_history = MLHistoryStore("data/ml_history.json")
        cash_analysis = CashAnalysis.calculate(
            available_cash=config.cash_available,
            total_portfolio=summary.total_portfolio_value,
            target_cash_pct=config.target_cash_weight,
            monthly_expenses=config.investor.monthly_expenses,
            monthly_contribution=monthly_contribution
        )

        ml_advisor_inst = MLAdvisor(ml_history) if ml_advisor_enabled else None

        ml_outputs = []
        for adj in portfolio_adjustments:
            # Derive streak, trend, and alert stats from history
            key_records = ml_history.get_records_by_key(adj.rec_key)
            pending_records = sorted(
                [r for r in key_records if not r.is_resolved],
                key=lambda r: r.created_date
            )

            streak_length = pending_records[-1].persistence_periods if pending_records else 0

            alert_count = len(key_records)
            days_since_first_alert = 0
            if key_records:
                first_date = datetime.strptime(
                    min(r.created_date for r in key_records), "%Y-%m-%d"
                ).date()
                days_since_first_alert = (date.today() - first_date).days

            trend_direction = "Flat"
            if len(pending_records) >= 2:
                current_drift = abs(adj.drift or 0.0)
                prev_drift = abs(pending_records[-2].drift_percent)
                if current_drift > prev_drift * 1.1:
                    trend_direction = "Worsening"
                elif current_drift < prev_drift * 0.9:
                    trend_direction = "Improving"

            if ml_advisor_inst:
                ml_output = ml_advisor_inst.advise(
                    rec_key=adj.rec_key,
                    symbol=adj.symbol,
                    metric_type="Drift",
                    asset_class="Leveraged" if adj.is_leveraged else "Equity",
                    current_drift=adj.drift or 0.0,
                    streak_length=streak_length,
                    trend_direction=trend_direction,
                    adjustment_mode=adj.adjustment_mode.value if adj.adjustment_mode else "",
                    original_score=adj.final_score,
                    has_cash_excess=cash_analysis.cash_excess > 0,
                    has_contributions=has_regular_contributions,
                    alert_count=alert_count,
                    days_since_first_alert=days_since_first_alert
                )
                ml_outputs.append(ml_output)

            # Always record for future ML training (if not dry run)
            if not dry_run:
                record = create_record_from_adjustment(
                    adj, cash_analysis,
                    streak_length=streak_length,
                    trend_direction=trend_direction
                )
                ml_history.add_record(record)

        result['ml_outputs'] = ml_outputs

        # Print ML advisor insights
        if ml_outputs:
            print("\n" + "=" * 50)
            print("ML ADVISOR INSIGHTS")
            print("=" * 50)
            for ml_out in ml_outputs:
                print(f"\n📊 {ml_out.symbol} - {ml_out.ml_recommendation}")
                print(f"   Original Score: {ml_out.original_score} → Adjusted: {ml_out.adjusted_score}")
                print(f"   Persistence: {ml_out.persistence.probability:.0%} probability")
                print(f"   Action Benefit: {ml_out.effectiveness.action_benefit_probability:.0%}")
                print(f"   Explanation: {ml_out.explanation}")
            print("\n" + "=" * 50)
        elif ml_advisor_enabled:
            logger.info("ML advisor enabled but no adjustments to advise on")
        else:
            logger.info("ML advisor disabled — history collection only")
        
        # Print portfolio adjustments summary
        if portfolio_adjustments:
            print("\n" + "=" * 50)
            print("CONSOLIDATED PORTFOLIO ADJUSTMENTS")
            print("=" * 50)
            for adj in portfolio_adjustments[:8]:
                level_icon = {
                    AdjActionLevel.ACTION_REQUIRED: "🚨",
                    AdjActionLevel.RECOMMENDED: "📋",
                    AdjActionLevel.MONITOR: "👀",
                    AdjActionLevel.FYI: "ℹ️"
                }.get(adj.action_level, "•")
                print(f"\n{level_icon} [{adj.final_score}] {adj.title}")
                print(f"   Mode: {adj.adjustment_mode.value}")
                print(f"   What: {adj.what}")
                print(f"   Why: {adj.why}")
                print(f"   Do: {adj.do}")
                print(f"   Next: {adj.next_check}")
            print("\n" + "=" * 50)
        
        # Print scored recommendations summary
        if scored_recommendations:
            print("\n" + "=" * 50)
            print("SCORED FINANCE RECOMMENDATIONS")
            print("=" * 50)
            for rec in scored_recommendations[:8]:
                level_icon = {
                    ActionLevel.ACTION_REQUIRED: "🚨",
                    ActionLevel.RECOMMENDED: "📋",
                    ActionLevel.MONITOR: "👀",
                    ActionLevel.FYI: "ℹ️"
                }.get(rec.action_level, "•")
                print(f"\n{level_icon} [{rec.final_score}] {rec.title}")
                print(f"   {rec.action_level.value} | {rec.impact_area.value}")
                print(f"   → {rec.action}")
            print("\n" + "=" * 50)

        # =====================
        # 5g. DECISION ENGINE (observe-only advisory layer)
        # =====================
        # Unifies all source artifacts into a single ranked advisory plan.
        # Never modifies upstream outputs or existing recommendation logic.
        try:
            from portfolio_automation.decision_engine import (
                build_decision_plan as _build_decision_plan,
                summarize_decision_plan as _summarize_decision_plan,
            )

            _de_violations: list = (result.get('guardrails') or {}).get('violations', [])

            _de_adjustments: list = [
                d for d in (
                    _adj_to_de_dict(adj) for adj in (portfolio_adjustments or [])
                ) if d
            ]

            _de_finance_recs: list = [
                d for d in (
                    _finance_rec_to_de_dict(rec) for rec in (scored_recommendations or [])
                ) if d
            ]

            _de_watchlist: list = (
                [r for r in _ws_result.get('results', []) if isinstance(r, dict)]
                if isinstance(_ws_result, dict) else []
            )

            _de_market_opps: list = _market_opps_from_coverage(
                result.get('market_coverage') or {}
            )

            _de_portfolio_ctx: dict = {
                'total_portfolio_value': summary.total_portfolio_value,
                'cash': config.cash_available,
                'current_holdings': {
                    h.symbol: {'value': h.market_value, 'pct': h.actual_weight}
                    for h in (holdings or [])
                    if h.market_value is not None
                },
                'degraded_mode': result.get('degraded_mode', False),
                'data_mode': result.get('data_mode', 'live'),
                'drawdown_regime': drawdown_regime,
                'active_structural_violations': _de_violations,
            }

            _decision_plan: list = _build_decision_plan(
                structural_violations=_de_violations,
                portfolio_adjustments=_de_adjustments,
                watchlist_signals=_de_watchlist,
                market_opportunities=_de_market_opps,
                finance_recommendations=_de_finance_recs,
                portfolio_context=_de_portfolio_ctx,
            )
            _decision_plan_summary: str = _summarize_decision_plan(
                _decision_plan, _de_portfolio_ctx
            )

            result['decision_plan'] = _decision_plan
            result['decision_plan_summary'] = _decision_plan_summary

            logger.info(
                "DECISION ENGINE: %d decisions generated (observe-only)",
                len(_decision_plan),
            )
            for _dp_i, _dp_d in enumerate(_decision_plan[:3], 1):
                logger.info(
                    "  #%d %s %s [%s] pri=%.3f src=%s flags=%s",
                    _dp_i,
                    _dp_d.get('symbol', '?'),
                    _dp_d.get('decision', '?'),
                    _dp_d.get('urgency', '?'),
                    _dp_d.get('priority', 0.0),
                    _dp_d.get('source', '?'),
                    _dp_d.get('risk_flags', []),
                )
        except Exception as _de_err:
            logger.warning("DECISION ENGINE: non-fatal error — %s", _de_err, exc_info=True)

        # =====================
        # 6. OUTPUT TO CONSOLE
        # =====================
        print("\n" + format_summary_text(summary))
        print("\n" + format_holdings_table(analyses))
        print("\n" + format_recommendations_text(recommendations))
        
        if retirement_summary.total_balance > 0:
            print("\n" + format_retirement_summary(retirement_summary))
        
        # =====================
        # 6b. HEARTBEAT
        # =====================
        if not dry_run:
            try:
                heartbeat = {
                    'timestamp': datetime.now().isoformat(),
                    'run_mode': run_mode,
                    'total_value': summary.total_portfolio_value,
                    'drawdown_regime': result['drawdown_regime'],
                }
                hb_path = Path('data/last_success.json')
                hb_path.parent.mkdir(parents=True, exist_ok=True)
                hb_path.write_text(
                    _json.dumps(heartbeat, indent=2), encoding='utf-8'
                )
                logger.info("Heartbeat written to data/last_success.json")
            except Exception as _hb_err:
                logger.warning(f"Heartbeat write failed (non-fatal): {_hb_err}")

        # =====================
        # 7. WRITE OUTPUT FILES
        # =====================
        if not dry_run:
            logger.info("Writing output files...")

            output_dir.mkdir(parents=True, exist_ok=True)
            _clear_conditional_output_artifacts(output_dir, logger)

            # Write CSV snapshot
            csv_success = write_csv_snapshot(
                filepath=str(output_dir / 'portfolio_snapshot.csv'),
                holdings=holdings,
                analyses=analyses,
                summary=summary,
                cash_available=config.cash_available
            )

            if not csv_success:
                result['warnings'].append("Failed to write CSV snapshot")

            # Write Excel workbook
            excel_success = create_excel_workbook(
                filepath=str(output_dir / 'portfolio_tracker.xlsx'),
                holdings=holdings,
                analyses=analyses,
                summary=summary,
                recommendations=recommendations,
                cash_available=config.cash_available,
                append_history=config.output.get('history_enabled', True)
            )
            
            if not excel_success:
                result['warnings'].append("Failed to write Excel workbook")
            
            # Export recommendations
            recs_path = str(output_dir / 'recommendations.csv')
            export_recommendations_csv(recs_path, recommendations, summary)
            
            # Export scored recommendations
            scored_recs_path = str(output_dir / 'scored_recommendations.csv')
            export_scored_recommendations_csv(scored_recs_path, scored_recommendations)
            
            # Export EmailView (consolidated portfolio adjustments)
            email_view_path = str(output_dir / 'email_view.csv')
            email_view_data = format_adjustments_for_email_view(portfolio_adjustments)
            if email_view_data:
                with open(email_view_path, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.DictWriter(f, fieldnames=email_view_data[0].keys())
                    writer.writeheader()
                    writer.writerows(email_view_data)
                logger.info(f"EmailView exported: {email_view_path}")
            
            # Save Claude prompt for email generation
            prompt_path = str(output_dir / 'email_prompt.txt')
            with open(prompt_path, 'w', encoding='utf-8') as f:
                f.write(get_email_prompt())
            logger.info(f"Email prompt saved: {prompt_path}")
            
            # Export ML advisor outputs
            if ml_outputs:
                ml_outputs_path = str(output_dir / 'ml_advisor_outputs.csv')
                ml_rows = [ml.to_dict() for ml in ml_outputs]
                with open(ml_outputs_path, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.DictWriter(f, fieldnames=ml_rows[0].keys())
                    writer.writeheader()
                    writer.writerows(ml_rows)
                logger.info(f"ML outputs exported: {ml_outputs_path}")
            
            # Export ML training data if available
            ml_training_count = ml_history.export_training_data(str(output_dir / 'ml_training_data.csv'))
            if ml_training_count > 0:
                logger.info(f"Exported {ml_training_count} ML training records")
            
            # Save historical analysis prompt
            ml_prompt_path = str(output_dir / 'ml_analysis_prompt.txt')
            with open(ml_prompt_path, 'w', encoding='utf-8') as f:
                f.write(get_historical_analysis_prompt())
            logger.info(f"ML analysis prompt saved: {ml_prompt_path}")

            # ── Decision engine outputs ───────────────────────────────────────
            _write_decision_engine_outputs(
                output_dir,
                result,
                run_mode,
                logger,
            )

            # ── Decision outcome tracker ──────────────────────────────────────
            if _run_outcome_tracker is not None:
                try:
                    _ot_root = _decision_explainer_root_from_output_dir(output_dir)
                    _ot_summary, _ = _run_outcome_tracker(_ot_root)
                    logger.info(
                        "OUTCOME TRACKER: tracked (total=%d resolved=%d hit_rate=%s)",
                        _ot_summary.get('total_decisions', 0),
                        _ot_summary.get('resolved', 0),
                        f"{_ot_summary['hit_rate']:.0%}" if _ot_summary.get('hit_rate') is not None else "n/a",
                    )
                except Exception as _ot_err:
                    logger.warning(
                        "OUTCOME TRACKER: non-fatal error — %s", _ot_err, exc_info=True
                    )

            # ── Decision triage ────────────────────────────────────────────────
            if _run_triage is not None:
                try:
                    _triage_root = _decision_explainer_root_from_output_dir(output_dir)
                    _triage_payload, _ = _run_triage(_triage_root)
                    logger.info(
                        "DECISION TRIAGE: decision_triage.json + .md written"
                        " (total=%d critical=%d action=%d monitor=%d ignore=%d)",
                        _triage_payload.get("total_decisions", 0),
                        (_triage_payload.get("bucket_counts") or {}).get("critical_action", 0),
                        (_triage_payload.get("bucket_counts") or {}).get("action_candidate", 0),
                        (_triage_payload.get("bucket_counts") or {}).get("monitor", 0),
                        (_triage_payload.get("bucket_counts") or {}).get("ignore_for_now", 0),
                    )
                except Exception as _triage_err:
                    logger.warning(
                        "DECISION TRIAGE: non-fatal error — %s", _triage_err, exc_info=True
                    )

            # ── Confidence calibration ─────────────────────────────────────────
            if _run_calibration is not None:
                try:
                    _cal_root = _decision_explainer_root_from_output_dir(output_dir)
                    _cal_payload, _ = _run_calibration(_cal_root)
                    if _cal_payload.get("insufficient_data"):
                        logger.info(
                            "CONFIDENCE CALIBRATION: skipped — %s",
                            _cal_payload.get("summary_line", "insufficient data"),
                        )
                    else:
                        logger.info(
                            "CONFIDENCE CALIBRATION: confidence_calibration.json + .md written"
                            " (resolved=%d hit_rate=%s)",
                            _cal_payload.get("total_resolved", 0),
                            f"{_cal_payload['overall_hit_rate']:.0%}"
                            if _cal_payload.get("overall_hit_rate") is not None
                            else "n/a",
                        )
                except Exception as _cal_err:
                    logger.warning(
                        "CONFIDENCE CALIBRATION: non-fatal error — %s", _cal_err, exc_info=True
                    )

            # ── Performance attribution ────────────────────────────────────────
            if _run_performance_attribution is not None:
                try:
                    _pa_root = _decision_explainer_root_from_output_dir(output_dir)
                    _pa_payload, _ = _run_performance_attribution(_pa_root)
                    if _pa_payload.get("insufficient_data"):
                        logger.info(
                            "PERFORMANCE ATTRIBUTION: skipped — %s",
                            _pa_payload.get("summary_line", "insufficient data"),
                        )
                    else:
                        logger.info(
                            "PERFORMANCE ATTRIBUTION: decision_performance_attribution.json written"
                            " (resolved=%d hit_rate=%s)",
                            _pa_payload.get("resolved_decisions", 0),
                            f"{_pa_payload['hit_rate']:.0%}"
                            if _pa_payload.get("hit_rate") is not None
                            else "n/a",
                        )
                except Exception as _pa_err:
                    logger.warning(
                        "PERFORMANCE ATTRIBUTION: non-fatal error — %s", _pa_err, exc_info=True
                    )

            # ── AI Budget Summary (observe-only) ─────────────────────────────
            try:
                from portfolio_automation.ai_budget import (
                    load_recent_ai_usage_events as _load_ai_events,
                    write_ai_budget_summary as _write_ai_budget,
                )
                _ai_events = _load_ai_events()
                if not dry_run:
                    _ai_budget_summary = _write_ai_budget(_ai_events)
                    logger.info("AI BUDGET: %s", _ai_budget_summary.summary_line)
            except Exception as _ab_err:
                logger.warning("AI BUDGET: non-fatal error — %s", _ab_err)

            # ── Scanner outputs ────────────────────────────────────────────────
            if scanner_candidates:
                # candidates_top20.csv
                top20_path = output_dir / 'candidates_top20.csv'
                top20_rows = scanner_candidates[:20]
                with open(top20_path, 'w', newline='', encoding='utf-8-sig') as _f:
                    _fields = [
                        'symbol', 'score', 'sector', 'mkt_cap', 'rev_growth',
                        'fcf_yield', 'roe', 'pe', 'price', 'price_200dma',
                        'above_200dma', 'reasons', 'scanned_at',
                        'theme_boost', 'theme_names',
                        'data_mode', 'degraded_mode', 'degraded_reason',
                        'degraded_confidence_penalty',
                    ]
                    _w = csv.DictWriter(_f, fieldnames=_fields, extrasaction='ignore')
                    _w.writeheader()
                    _w.writerows(top20_rows)
                logger.info(f"Scanner top-20 candidates written: {top20_path}")

                # candidates_debug.csv (only on monthly/weekly when debug rows available)
                if scanner_debug_rows:
                    debug_path = output_dir / 'candidates_debug.csv'
                    with open(debug_path, 'w', newline='', encoding='utf-8-sig') as _f:
                        _w = csv.DictWriter(
                            _f,
                            fieldnames=['symbol', 'passed', 'failed_filters', 'score'],
                            extrasaction='ignore',
                        )
                        _w.writeheader()
                        _w.writerows(scanner_debug_rows)
                    logger.info(f"Scanner debug CSV written: {debug_path}")

            if scanner_sleeve_plan:
                sleeve_path = output_dir / 'spec_sleeve_plan.csv'
                sleeve_rows = [r.to_dict() for r in scanner_sleeve_plan]
                with open(sleeve_path, 'w', newline='', encoding='utf-8-sig') as _f:
                    _w = csv.DictWriter(_f, fieldnames=sleeve_rows[0].keys())
                    _w.writeheader()
                    _w.writerows(sleeve_rows)
                logger.info(f"Spec sleeve plan written: {sleeve_path}")

            # ── Growth Mode outputs ───────────────────────────────────────────
            if growth_mode_active:
                # Contribution plan CSV
                if contribution_plan:
                    contrib_path = str(output_dir / 'contribution_plan.csv')
                    write_contribution_plan_csv(
                        contrib_path, contribution_plan, drawdown_regime
                    )

                # Compounding dashboard text file
                if dashboard:
                    dash_path = str(output_dir / 'compounding_dashboard.txt')
                    write_compounding_dashboard_txt(
                        dash_path, format_dashboard_text(dashboard)
                    )
        else:
            logger.info("Dry run - skipping file writes")

        # =====================
        # 7d. POLICY EVALUATION (recommendation observability)
        # =====================
        # Advisory only — never blocks the main run, never modifies scores.
        try:
            from policy_evaluator import (
                append_run_recommendations,
                evaluate_history,
                write_evaluation_reports,
            )
            _policy_run_id = run_id or f"{date.today().isoformat()}_{run_mode}"
            append_run_recommendations(
                scored_recommendations=scored_recommendations or [],
                run_id=_policy_run_id,
                run_mode=run_mode,
                data_health=result.get("data_health"),
                drawdown_state=drawdown_state if "drawdown_state" in dir() else None,
                drawdown_regime=result.get("drawdown_regime", "normal"),
                guardrails=result.get("guardrails"),
                growth_mode=growth_cfg.get("mode", "none") if "growth_cfg" in dir() else "none",
                dry_run=dry_run,
            )
            _eval_result = evaluate_history()
            write_evaluation_reports(_eval_result, dry_run=dry_run)
            logger.info(
                "POLICY EVAL: %d records across %d runs (%s → %s)",
                _eval_result.total_records,
                _eval_result.total_runs,
                _eval_result.date_range.get("first"),
                _eval_result.date_range.get("last"),
            )
        except Exception as _policy_err:
            logger.debug("POLICY EVAL: non-fatal error — %s", _policy_err)

        # =====================
        # 7e. PROFIT ATTRIBUTION (read-only learning layer)
        # =====================
        # Answers "what decisions actually made money?" — never modifies live logic.
        _pa_summary = None
        try:
            from profit_attribution import run_profit_attribution, write_attribution_reports
            _pa_summary = run_profit_attribution()
            write_attribution_reports(_pa_summary, dry_run=dry_run)
            _pa_m = _pa_summary.metrics
            logger.info(
                "PROFIT ATTR: %d trades, %d attr | win=%.0f%% rr=%s exp=%s | %d missed opps",
                _pa_m.total_entries,
                _pa_m.attributable_entries,
                (_pa_m.win_rate or 0) * 100,
                f"{_pa_m.risk_reward:.2f}x" if _pa_m.risk_reward is not None else "—",
                f"{(_pa_m.expectancy or 0) * 100:+.2f}%" if _pa_m.expectancy is not None else "—",
                len(_pa_summary.missed_opportunities),
            )
        except Exception as _pa_err:
            logger.debug("PROFIT ATTR: non-fatal error — %s", _pa_err)

        # =====================
        # 7c. BUILD DIGEST CONTEXT (enhanced email sections)
        # =====================
        digest_ctx = None
        try:
            from digest_builder import DigestContext as _DigestCtx

            _prior_snap: dict = {}
            _prior_regime: str | None = None
            _weekly_days_ago: int | None = None
            _monthly_days_ago: int | None = None

            if store is not None:
                try:
                    _snaps = store.get_recent_snapshots(mode=run_mode, n=1)
                    if _snaps:
                        _prior_snap = _snaps[0]
                except Exception as _e:
                    logger.debug("get_recent_snapshots failed (non-fatal): %s", _e)

                try:
                    _lw = store.get_last_successful_run('weekly')
                    if _lw and _lw.get('completed_at'):
                        _weekly_days_ago = (
                            datetime.now() - datetime.fromisoformat(_lw['completed_at'])
                        ).days
                except Exception:
                    pass

                try:
                    _lm = store.get_last_successful_run('monthly')
                    if _lm and _lm.get('completed_at'):
                        _monthly_days_ago = (
                            datetime.now() - datetime.fromisoformat(_lm['completed_at'])
                        ).days
                except Exception:
                    pass

            _guardrail_viols: list = (
                (result.get('guardrails') or {}).get('violations', [])
            )

            _fmp_cb_open = False
            _fmp_until: str | None = None
            if store is not None:
                try:
                    _fmp_cb_open = store.is_subsystem_disabled("fmp")
                    if _fmp_cb_open:
                        _sh = store.get_subsystem_health("fmp") or {}
                        _fmp_until = _sh.get('disabled_until')
                except Exception:
                    pass

            _opportunity_cost_cfg = getattr(config, 'opportunity_cost', {}) or {}

            # ── Build theme_highlights for digest ─────────────────────────
            _theme_highlights: dict | None = None
            try:
                if _te_result:
                    _ew_cfg_local = getattr(config, 'extended_watchlist', None) or {}
                    _ew_enabled_local = isinstance(_ew_cfg_local, dict) and _ew_cfg_local.get('enabled', True)

                    # Prior-day themes from SQLite for delta detection
                    _themes_prior: list = []
                    if store is not None:
                        try:
                            _all_recent = store.get_recent_theme_signals(days=2)
                            _today_str = datetime.now().strftime('%Y-%m-%d')
                            # Collapse yesterday's rows: keep max confidence per theme
                            _prior_by_name: dict = {}
                            for _sig in _all_recent:
                                if _sig.get('run_date') != _today_str:
                                    _name = _sig.get('theme_name', '')
                                    _conf = float(_sig.get('confidence', 0))
                                    if _name not in _prior_by_name or _conf > _prior_by_name[_name]:
                                        _prior_by_name[_name] = _conf
                            _themes_prior = [
                                {'name': n, 'confidence': c}
                                for n, c in _prior_by_name.items()
                            ]
                        except Exception:
                            pass

                    # Outcome feedback via get_outcome_history (prioritised, last 14 days)
                    _outcome_updates: list = []
                    if _ew_enabled_local:
                        try:
                            from watchlist_scanner.extended_watchlist import ExtendedWatchlist
                            _ew_hist = ExtendedWatchlist(
                                db_path=_ew_cfg_local.get('db_path', 'data/portfolio.db')
                            )
                            for entry in _ew_hist.get_outcome_history(days=14):
                                _outcome_updates.append({
                                    'symbol': entry['symbol'],
                                    'outcome': entry.get('outcome', 'none'),
                                    'days_since_promoted': ExtendedWatchlist.days_since(
                                        entry.get('promoted_at', '')
                                    ),
                                    'scan_count': entry.get('scan_count', 0),
                                    'alert_count': entry.get('alert_count', 0),
                                })
                        except Exception as _oe:
                            logger.debug("outcome_history failed (non-fatal): %s", _oe)

                    # Budget-skipped extended tickers from scanner result
                    _budget_scanner_skipped: list = []
                    try:
                        _ew_meta = _ws_result.get('extended_watchlist_meta', {})
                        _budget_scanner_skipped = _ew_meta.get('skipped_for_budget', [])
                    except Exception:
                        pass

                    _theme_highlights = {
                        'themes_today': [
                            {
                                'name': t.get('name', ''),
                                'confidence': t.get('confidence', 0),
                                'persistence_7d': t.get('persistence_7d', 0),
                            }
                            for t in _te_result.get('themes', [])
                        ],
                        'themes_prior': _themes_prior,
                        'new_candidates': _te_result.get('watch_candidates', []),
                        'promoted': _ew_result.get('promoted', []),
                        'reinforced': _ew_result.get('reinforced', []),
                        'expired': _ew_result.get('expired', []),
                        'skipped': _ew_result.get('skipped', []),
                        'outcome_updates': _outcome_updates,
                        'budget_scanner_skipped': _budget_scanner_skipped,
                    }
            except Exception as _th_err:
                logger.debug("theme_highlights build failed (non-fatal): %s", _th_err)

            digest_ctx = _DigestCtx(
                total_value=summary.total_portfolio_value,
                cash_available=config.cash_available,
                max_drift=abs(summary.max_drift) if summary.max_drift is not None else 0.0,
                drawdown_pct=drawdown_state.drawdown_from_12m_high,
                drawdown_regime=drawdown_regime,
                monthly_contribution=monthly_contribution,
                expected_cagr=portfolio_cagr,
                prior_snapshot=_prior_snap or None,
                prior_drawdown_regime=_prior_snap.get('drawdown_regime') if _prior_snap else None,
                dashboard=dashboard,
                portfolio_adjustments=portfolio_adjustments,
                scored_recommendations=scored_recommendations,
                contribution_plan=contribution_plan,
                holdings=holdings,
                holding_rationale=getattr(config, 'holding_rationale', {}) or {},
                guardrail_violations=_guardrail_viols,
                av_budget_remaining=_av_budget.remaining() if _av_budget is not None else None,
                av_budget_total=25,
                fmp_circuit_breaker_open=_fmp_cb_open,
                fmp_disabled_until=_fmp_until,
                scanner_enabled=bool(config.scanner_enabled),
                watchlist_enabled=bool(
                    (getattr(config, 'watchlist_scanner', None) or {}).get('enabled', False)
                ),
                last_successful_weekly_days_ago=_weekly_days_ago,
                last_successful_monthly_days_ago=_monthly_days_ago,
                idle_cash_threshold=float(
                    _opportunity_cost_cfg.get('idle_cash_threshold', 2000.0)
                ),
                idle_cash_projection_years=int(
                    _opportunity_cost_cfg.get('projection_years', 10)
                ),
                theme_highlights=_theme_highlights,
            )
            logger.info("Digest context built successfully")
        except Exception as _ctx_err:
            logger.warning("Failed to build digest context (non-fatal): %s", _ctx_err)
            digest_ctx = None

        # =====================
        # 8. SEND EMAIL REPORT  (gated by --run-mode)
        # =====================
        if skip_email:
            logger.info("Email send skipped by flag")
        elif dry_run:
            logger.info("Dry run - skipping email send")
        elif not config.email.get('enabled', False) and not force_email:
            logger.info("Email disabled in config")
        else:
            logger.info(f"Evaluating email send conditions (run_mode={run_mode})...")

            try:
                digest_sender = FinanceEmailDigest(
                    smtp_server=config.email.get('smtp_server', 'smtp.gmail.com'),
                    smtp_port=config.email.get('smtp_port', 587),
                    use_tls=config.email.get('use_tls', True),
                    sender_email=config.email.get('sender_email'),
                    recipient_email=config.email.get('recipient_email')
                )

                if not digest_sender.is_configured():
                    logger.warning("Email not configured (missing credentials)")
                    result['warnings'].append("Email credentials not configured")
                else:
                    summary_lines = finance_analyzer.get_summary_lines(summary)

                    # Prepend degraded-mode notices
                    _degraded: list[str] = []
                    _run_data_health = result.get("data_health") or {}
                    if _run_data_health.get("degraded_mode"):
                        _degraded.append(
                            "[DEGRADED DATA] "
                            f"mode={_run_data_health.get('data_mode', 'fallback')} "
                            f"reason={_run_data_health.get('degraded_reason') or 'unknown'} "
                            f"sources={','.join(_run_data_health.get('data_sources_used', ['fallback']))}"
                        )
                    if _av_budget is not None and _av_budget.remaining() < 5:
                        _degraded.append(f"[DEGRADED] AV budget low: {_av_budget.status_line()}")
                    if store is not None and store.is_subsystem_disabled("fmp"):
                        _sh = store.get_subsystem_health("fmp") or {}
                        _degraded.append(
                            f"[DEGRADED] FMP scanner disabled until {_sh.get('disabled_until', '?')}"
                        )
                    if store is not None:
                        for _sv in store.get_all_structural_violations():
                            if _sv.get("days_active", 0) > 7:
                                _degraded.append(
                                    f"[PERSISTENT VIOLATION] {_sv['violation_key']} "
                                    f"active {_sv['days_active']}d "
                                    f"(escalation level {_sv['escalation_level']})"
                                )
                    if _degraded:
                        summary_lines = _degraded + summary_lines

                    email_sent = False

                    if run_mode == 'daily':
                        # Only send if there are ACTION_REQUIRED items (structural violations)
                        has_urgent = any(
                            a.action_level == AdjActionLevel.ACTION_REQUIRED
                            for a in portfolio_adjustments
                        ) or any(
                            r.action_level == ActionLevel.ACTION_REQUIRED
                            for r in scored_recommendations
                        )
                        if has_urgent or force_email:
                            logger.info("Daily mode: action-required items found — sending alert")
                            _digest_hash = compute_digest_hash(scored_recommendations, summary_lines)
                            if not force_email and store is not None and store.was_hash_sent_recently(_digest_hash, days=7):
                                logger.info("Duplicate digest hash — skipping email send")
                            else:
                                email_sent = digest_sender.send_digest(
                                    recommendations=scored_recommendations,
                                    summary_lines=summary_lines,
                                    is_digest_day=False,
                                    force_send=True,
                                    context=digest_ctx,
                                )
                                if email_sent and store is not None:
                                    store.record_email_sent(_digest_hash, run_mode)
                        else:
                            logger.info("Daily mode: no action-required items — running silently")

                    elif run_mode == 'weekly':
                        # Always send the full digest on weekly runs
                        _digest_hash = compute_digest_hash(scored_recommendations, summary_lines)
                        if not force_email and store is not None and store.was_hash_sent_recently(_digest_hash, days=7):
                            logger.info("Duplicate weekly digest hash — skipping email send")
                        else:
                            email_sent = digest_sender.send_digest(
                                recommendations=scored_recommendations,
                                summary_lines=summary_lines,
                                is_digest_day=True,
                                force_send=True,
                                context=digest_ctx,
                            )
                            if email_sent and store is not None:
                                store.record_email_sent(_digest_hash, run_mode)

                    elif run_mode == 'monthly':
                        # Send Capital Deployment Memo
                        if dashboard is not None:
                            contrib_rows = [a.to_dict() for a in contribution_plan]
                            _memo_hash = compute_monthly_memo_hash(
                                summary_lines, contrib_rows, dashboard.to_dict()
                            )
                            if not force_email and store is not None and store.was_hash_sent_recently(_memo_hash, days=7):
                                logger.info("Duplicate monthly memo hash — skipping email send")
                            else:
                                email_sent = digest_sender.send_monthly_memo(
                                    summary_lines=summary_lines,
                                    contribution_rows=contrib_rows,
                                    dashboard_dict=dashboard.to_dict(),
                                    drawdown_regime=drawdown_regime,
                                    context=digest_ctx,
                                )
                                if email_sent and store is not None:
                                    store.record_email_sent(_memo_hash, run_mode)
                        else:
                            # Growth mode disabled; fall back to weekly digest
                            logger.warning(
                                "Monthly mode but compounding dashboard unavailable "
                                "(is growth_mode.mode set to 'accumulation_aggressive'?). "
                                "Falling back to weekly digest."
                            )
                            _digest_hash = compute_digest_hash(scored_recommendations, summary_lines)
                            if not force_email and store is not None and store.was_hash_sent_recently(_digest_hash, days=7):
                                logger.info("Duplicate fallback digest hash — skipping email send")
                            else:
                                email_sent = digest_sender.send_digest(
                                    recommendations=scored_recommendations,
                                    summary_lines=summary_lines,
                                    is_digest_day=True,
                                    force_send=True,
                                    context=digest_ctx,
                                )
                                if email_sent and store is not None:
                                    store.record_email_sent(_digest_hash, run_mode)

                    if email_sent:
                        logger.info("Email sent successfully")
                    else:
                        logger.info("No email sent")

            except Exception as e:
                logger.error(f"Email error: {e}")
                result['warnings'].append(f"Email failed: {e}")
        
        # ── Memo email delivery (observe-only, disabled by default) ───────────
        # Reads outputs/latest/daily_memo.txt and .md written by daily_memo.py.
        # Fires only when MEMO_EMAIL_ENABLED=1; non-blocking unless
        # MEMO_EMAIL_STRICT_FAILURE=1.
        if _run_memo_email_delivery is not None and not dry_run:
            try:
                _memo_base = (
                    output_dir.parent
                    if output_dir is not None and output_dir.name == "latest"
                    else Path("outputs")
                )
                _memo_run_id = f"{date.today().isoformat()}_{run_mode}"
                _memo_result = _run_memo_email_delivery(
                    run_id=_memo_run_id,
                    base_dir=_memo_base,
                )
                logger.info(
                    "MEMO EMAIL: enabled=%s sent=%s skipped=%s reason=%s",
                    _memo_result.get("enabled"),
                    _memo_result.get("sent"),
                    _memo_result.get("skipped"),
                    _memo_result.get("reason"),
                )
            except Exception as _memo_err:
                logger.warning("MEMO EMAIL: non-fatal error — %s", _memo_err, exc_info=True)

        # =====================
        # 9. FINALIZE
        # =====================
        result['success'] = True
        logger.info("Portfolio update completed successfully")
        
    except Exception as e:
        logger.error(f"Portfolio update failed: {e}")
        logger.debug(traceback.format_exc())
        result['errors'].append(str(e))
        result['success'] = False

    # Pipeline run status (additive, non-blocking). Mirrors the sandbox lane's
    # sandbox_run_status.json so operators have one consistent shape for both
    # lanes. Failures here must not affect the run result.
    try:
        from portfolio_automation.run_status import (
            status_from_main_result,
            write_pipeline_run_status,
        )
        _run_status = status_from_main_result(
            result,
            run_mode=run_mode,
            duration_seconds=time.monotonic() - _run_status_start,
        )
        _run_status_paths = write_pipeline_run_status(_run_status)
        if "error" in _run_status_paths:
            logger.warning("pipeline_run_status write failed: %s", _run_status_paths["error"])
        else:
            logger.info(
                "pipeline_run_status written: %s",
                _run_status_paths.get("pipeline_run_status_json"),
            )
    except Exception as _status_err:
        logger.warning("pipeline_run_status emission failed (non-fatal): %s", _status_err)

    return result


def main() -> int:
    """Main entry point for scheduled and manual runs."""
    args = parse_arguments()

    # Setup logging — writes to stdout and logs/YYYY-MM-DD.log
    logger = setup_logging(debug=args.debug)
    logger.info("=" * 60)
    logger.info(f"PORTFOLIO AUTOMATION SYSTEM  [run_mode={args.run_mode}]")
    logger.info("=" * 60)

    # ── Run mode governance — normalize and log active mode (non-blocking) ────
    try:
        from portfolio_automation.run_mode_governance import (
            normalize_run_mode as _normalize_run_mode,
            create_run_mode_context as _create_run_mode_context,
            is_official_mode as _is_official_mode,
        )
        _active_run_mode = _normalize_run_mode(args.run_mode)
        _run_mode_ctx = _create_run_mode_context(_active_run_mode)
        logger.info(
            "RUN MODE GOVERNANCE: mode=%s lane=%s "
            "can_write_latest=%s can_emit_recommendations=%s can_execute_trades=%s",
            _active_run_mode.value,
            "official" if _is_official_mode(_active_run_mode) else "research",
            _run_mode_ctx.policy.can_write_latest,
            _run_mode_ctx.policy.can_emit_recommendations,
            _run_mode_ctx.policy.can_execute_trades,
        )
    except Exception as _rmg_err:
        logger.debug("Run mode governance logging skipped (non-fatal): %s", _rmg_err)

    # ── Run lock: exit cleanly if another run is already in progress ──────────
    lock_file = Path("data/run.lock")
    if not acquire_run_lock(lock_file):
        logger.info("Exiting — another run is already in progress.")
        return 0

    exit_code = 1
    store = None
    run_id = None
    run_started = False
    try:
        # Load environment variables
        logger.info("Loading environment...")
        load_env(args.env)

        # Load configuration
        logger.info(f"Loading configuration from: {args.config}")
        config = load_config(args.config, profile=args.profile)

        logger.info(f"Investor: {config.investor.name}")
        logger.info(f"Holdings: {len(config.holdings)} assets")
        logger.info(f"Rebalance band: ±{config.rebalance_rules.band_threshold:.1%}")
        if args.run_mode == "daily":
            try:
                from agent.llm_adapters import (
                    resolve_ollama_base_url,
                    resolve_provider,
                    resolve_task_provider,
                )

                theme_cfg = getattr(config, "theme_engine", {}) or {}
                task_providers = theme_cfg.get("task_providers", {}) if isinstance(theme_cfg.get("task_providers"), dict) else {}
                llm_provider = resolve_task_provider(
                    cli_provider=args.llm_provider,
                    task_provider=task_providers.get(args.run_mode),
                    fallback_task_provider=theme_cfg.get("llm_provider"),
                )
                llm_provider = llm_provider or resolve_provider(None, default="ollama")
                fallback_chain = [llm_provider]
                if llm_provider == "anthropic":
                    llm_model = os.environ.get("ANTHROPIC_MODEL") or theme_cfg.get("anthropic_model", "claude-haiku-4-5-20251001")
                    base_url = "(n/a)"
                elif llm_provider == "openai":
                    llm_model = os.environ.get("OPENAI_MODEL") or theme_cfg.get("openai_model", "")
                    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
                else:
                    llm_model = os.environ.get("OLLAMA_MODEL") or theme_cfg.get("ollama_model", "gemma3:4b")
                    try:
                        base_url = resolve_ollama_base_url(
                            os.environ.get("OLLAMA_BASE_URL") or theme_cfg.get("ollama_base_url")
                        )
                    except Exception as exc:
                        base_url = f"<invalid: {exc}>"
                logger.info(
                    "LLM startup: task=%s provider=%s model=%s base_url=%s fallback_chain=%s",
                    f"theme_engine.{args.run_mode}",
                    llm_provider,
                    llm_model or "(unset)",
                    base_url,
                    " -> ".join(fallback_chain),
                )
            except Exception as _llm_log_err:
                logger.debug("LLM startup logging skipped (non-fatal): %s", _llm_log_err)

        if args.dry_run:
            logger.info("DRY RUN MODE - No files will be written, no emails sent")

        # ── SQLite state store ─────────────────────────────────────────────────
        store = PortfolioStateStore(Path("data/portfolio.db"))
        try:
            if config.scanner_enabled and store.is_subsystem_disabled("fmp"):
                _sh = store.get_subsystem_health("fmp") or {}
                logger.warning(
                    "DATA degraded startup: FMP circuit breaker open; fallback data will be used until %s",
                    _sh.get("disabled_until", "?"),
                )
        except Exception as _startup_health_err:
            logger.debug("Startup data-health logging skipped (non-fatal): %s", _startup_health_err)

        # ── Run heartbeat / overdue detection ─────────────────────────────────
        try:
            for _mode, _max_days, _label in [
                ("weekly", 8, "weekly digest"),
                ("monthly", 35, "monthly memo"),
            ]:
                _last = store.get_last_successful_run(_mode)
                if _last:
                    _last_dt = datetime.fromisoformat(_last["completed_at"])
                    _age = (datetime.now() - _last_dt).days
                    if _age > _max_days:
                        logger.warning(
                            "HEARTBEAT: %s last ran %d days ago (threshold %d) — "
                            "check Task Scheduler or cron.",
                            _label, _age, _max_days,
                        )
        except Exception as _hb_check_err:
            logger.debug("Heartbeat overdue check failed (non-fatal): %s", _hb_check_err)

        # ── Idempotency check ──────────────────────────────────────────────────
        run_id = f"{date.today().isoformat()}_{args.run_mode}"
        if not args.dry_run:
            if store.is_completed(run_id):
                logger.info(
                    f"Run {run_id} already completed today — "
                    "outputs/latest is current. Exiting (idempotent)."
                )
                return 0
            if store.is_stale_running(run_id, stale_minutes=30):
                logger.warning(
                    f"Run {run_id} found stuck in 'running' state — "
                    "treating as failed, proceeding."
                )
                store.fail_run(run_id)
            if not store.start_run(run_id, args.run_mode):
                logger.info(f"Run {run_id} is already in progress — exiting.")
                return 0

        # ── Output directories ─────────────────────────────────────────────────
        # Always write to outputs/latest/ so the newest run is easy to find.
        # After a successful run we copy once to outputs/history/YYYY-MM-DD/
        # so there is a permanent record per day without duplicates.
        output_dir = Path('outputs') / 'latest'

        if not args.dry_run:
            run_started = True

        # Run the portfolio update
        result = run_portfolio_update(
            config=config,
            dry_run=args.dry_run,
            force_email=args.force_email,
            skip_email=args.skip_email,
            run_mode=args.run_mode,
            output_dir=output_dir,
            logger=logger,
            store=store,
            llm_provider_override=args.llm_provider,
        )

        # ── Record run outcome in state store ──────────────────────────────────
        if not args.dry_run:
            try:
                if result['success']:
                    if result['summary']:
                        store.record_snapshot(
                            run_id=run_id,
                            total_value=result['summary'].total_portfolio_value,
                            cash=config.cash_available,
                            max_drift=result['summary'].max_drift,
                            drawdown_regime=result.get('drawdown_regime', 'normal'),
                        )
                    store.complete_run(run_id)
                else:
                    store.fail_run(run_id)
            except Exception as _store_err:
                logger.warning(f"State store update failed (non-fatal): {_store_err}")

        # ── Archive today's outputs (once per day, no duplicates) ─────────────
        if result['success'] and not args.dry_run and output_dir.exists():
            history_dir = Path('outputs') / 'history' / date.today().isoformat()
            if not history_dir.exists():
                history_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(str(output_dir), str(history_dir))
                logger.info(f"Outputs archived to {history_dir}")
            else:
                logger.debug(f"History already exists for today at {history_dir}; skipping archive")

        # Report results
        logger.info("-" * 60)
        if result['success']:
            logger.info("✓ Portfolio update completed successfully")

            if result['warnings']:
                logger.info(f"  Warnings: {len(result['warnings'])}")
                for warn in result['warnings']:
                    logger.warning(f"    - {warn}")

            if result['summary']:
                logger.info(f"  Total Portfolio: ${result['summary'].total_portfolio_value:,.2f}")
                logger.info(f"  Max Drift: {result['summary'].max_drift:.2%} ({result['summary'].max_drift_symbol})")
                logger.info(f"  Rebalance Needed: {'Yes' if result['summary'].has_breach else 'No'}")
            _llm_provider = (
                ((result.get("theme_engine") or {}).get("llm_metadata") or {}).get("actual_provider")
                or ((result.get("theme_engine") or {}).get("llm_metadata") or {}).get("resolved_provider")
                or "n/a"
            )
            _data_health = result.get("data_health") or {}
            if _data_health:
                logger.info(
                    "Run completed (degraded mode: %s, data=%s, llm=%s, latency=%sms)",
                    "yes" if _data_health.get("degraded_mode") else "no",
                    _data_health.get("data_mode", "live"),
                    _llm_provider,
                    _data_health.get("data_latency_ms", "n/a"),
                )

            exit_code = 0
        else:
            logger.error("✗ Portfolio update failed")
            for error in result['errors']:
                logger.error(f"  - {error}")
            exit_code = 1

    except FileNotFoundError as e:
        if run_started:
            _mark_failed_run(store, run_id, args.dry_run, logger)
        logger.error(f"File not found: {e}")
        exit_code = 1

    except EnvironmentError as e:
        if run_started:
            _mark_failed_run(store, run_id, args.dry_run, logger)
        logger.error(f"Environment error: {e}")
        logger.error("Please check your .env file and ensure all required variables are set")
        exit_code = 1

    except KeyboardInterrupt:
        if run_started:
            _mark_failed_run(store, run_id, args.dry_run, logger)
        logger.info("Interrupted by user")
        exit_code = 130

    except Exception as e:
        if run_started:
            _mark_failed_run(store, run_id, args.dry_run, logger)
        logger.error(f"Unexpected error: {e}")
        if args.debug:
            logger.error(traceback.format_exc())
        exit_code = 1

    finally:
        release_run_lock(lock_file)

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
