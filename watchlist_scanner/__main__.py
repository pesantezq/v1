"""
Watchlist Scanner CLI.

Usage:
    py -m watchlist_scanner [--dry-run] [--debug] [--config config.json]
                            [--output-dir outputs/latest]

Output files (written to --output-dir):
    watchlist_signals.json    — full results for all scanned tickers
    watchlist_alerts.csv      — tickers that triggered an alert
    watchlist_summary.md      — human-readable summary
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from config.loader import load_runtime_config_dict
from portfolio_automation.holdings_resolver import broker_overlaid_portfolio
from degraded_mode import build_data_health_context
from market_regime import detect_market_regime, regime_fit_commentary
from watchlist_scanner.conviction import apply_conviction_layer
from watchlist_scanner.models import PortfolioContext, WatchlistScanResult
from watchlist_scanner.output_writers import (
    _write_alerts_csv,
    _write_portfolio_snapshot_json,
    _write_portfolio_summary_md,
    _write_signals_json,
    _write_summary_md,
)
from watchlist_scanner.performance_feedback import run_signal_feedback_cycle
from watchlist_scanner.portfolio_construction import apply_portfolio_construction_layer
from watchlist_scanner.postprocess import (
    _apply_alert_cooldown,
    _apply_output_ordering,
    _apply_portfolio_priority_overlay,
    _apply_signal_meta_layer,
)

logger = logging.getLogger("watchlist_scanner")


def _load_prior_regime(portfolio_out: Path) -> dict | None:
    """Return the market_regime dict from the previous run's portfolio snapshot, or None."""
    try:
        path = portfolio_out / "portfolio_snapshot.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            regime = data.get("market_regime") if isinstance(data, dict) else None
            if isinstance(regime, dict) and regime.get("regime_label"):
                return regime
    except Exception as exc:
        logger.debug("Prior regime unavailable (non-fatal): %s", exc)
    return None


def _apply_post_cooldown_fallback(
    scan_result: WatchlistScanResult,
    signals_config: dict | None = None,
) -> WatchlistScanResult:
    """Surface fallback opportunities only after cooldown suppression is final."""
    alerts = list(scan_result.get("alerts") or [])
    if alerts:
        return scan_result

    cfg = signals_config or {}
    fallback_top_n = int(cfg.get("fallback_top_n", 3))
    fallback_min_signal = float(cfg.get("fallback_min_signal_score", 0.25))
    if fallback_top_n <= 0:
        return scan_result

    results = list(scan_result.get("results") or [])
    candidates = [
        row for row in results
        if float(row.get("signal_score") or 0.0) >= fallback_min_signal
    ]
    if not candidates:
        return scan_result

    candidates.sort(
        key=lambda row: (
            row.get("priority_score", 0.0),
            row.get("final_rank_score", 0.0),
        ),
        reverse=True,
    )
    fallback_alerts: list[dict] = []
    for row in candidates[:fallback_top_n]:
        row["alert_priority"] = "watch"
        row["alert_type"] = "opportunity"
        row["alert_reason"] = "top-ranked fallback"
        row["filter_allowed"] = True
        row["filter_reason_code"] = "fallback_top_n"
        row["notification_status"] = "fallback_opportunity"
        row["notification_reason"] = "fallback opportunity surfaced after cooldown"
        fallback_alerts.append(row)

    if not fallback_alerts:
        return scan_result

    summary = scan_result.setdefault("scan_summary", {})
    summary["fallback_alerts_used"] = True
    summary["fallback_alert_count"] = len(fallback_alerts)
    summary["fallback_trigger_stage"] = "post_cooldown"
    summary["alerts_watch_level"] = max(int(summary.get("alerts_watch_level") or 0), len(fallback_alerts))
    scan_result["alerts"] = fallback_alerts
    logger.info(
        "Post-cooldown fallback surfaced %d top-ranked opportunit%s",
        len(fallback_alerts),
        "y" if len(fallback_alerts) == 1 else "ies",
    )
    return scan_result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    config: dict,
    dry_run: bool = False,
    output_dir: str = "outputs/latest",
    extended_watchlist_config: dict | None = None,
    portfolio_context: PortfolioContext | None = None,
    signals_config: dict | None = None,
    ranking_config: dict | None = None,
    scraped_intel_config: dict | None = None,
    data_sources_config: dict | None = None,
) -> WatchlistScanResult:
    """
    Execute the watchlist scanner from config dict.

    Args:
        config:                   ``watchlist_scanner`` sub-dict from config.json.
        dry_run:                  If True, skip API calls (use cache only).
        output_dir:               Directory to write output files.
        extended_watchlist_config: ``extended_watchlist`` sub-dict from config.json,
                                  or None to disable the extended layer.
        portfolio_context:        Optional portfolio holdings/cash context for
                                  portfolio-aware ordering overlays.
        scraped_intel_config:     ``scraped_intel`` sub-dict from config.json,
                                  or None to disable the scraped intelligence layer.

    Returns:
        scan_result dict (same as WatchlistScanner.run()) with an additional
        ``extended_watchlist_meta`` key containing outcome metadata.
    """
    from watchlist_scanner.cache_manager import CacheManager
    from watchlist_scanner.scanner import WatchlistScanner
    from watchlist_scanner.config import DEFAULT_WATCHLIST

    cache_dir = config.get("cache_dir", "data/watchlist_cache")
    static_watchlist = config.get("watchlist", DEFAULT_WATCHLIST)
    manual_dry_run = bool(dry_run)
    cache_only_mode = manual_dry_run
    if signals_config is None:
        signals_config = config.get("signals") or None

    cache = CacheManager(cache_dir=cache_dir)

    # —— Extended watchlist: load active symbols ————————————————————————————————
    # FMP is the data source and is effectively uncapped (FMP plan = 300/min,
    # config fmp_daily_calls_budget=0 means no cap), so active extended symbols
    # are always included in the scan whenever the live (non-dry-run) path runs.
    ew_cfg = extended_watchlist_config or {}
    ew_enabled = bool(ew_cfg.get("enabled", True))
    extended_tickers: list[str] = []
    # symbol → theme_name, used to thread a finer watchlist_source label below.
    _extended_theme_by_symbol: dict[str, str] = {}
    _ew_obj = None

    if ew_enabled:
        try:
            from watchlist_scanner.extended_watchlist import ExtendedWatchlist
            _ew_obj = ExtendedWatchlist(
                db_path=ew_cfg.get("db_path", "data/portfolio.db"),
                ttl_days=int(ew_cfg.get("ttl_days", 7)),
                max_symbols=int(ew_cfg.get("max_symbols", 3)),
                confidence_threshold=float(ew_cfg.get("confidence_threshold", 0.80)),
            )
            active_entries = _ew_obj.get_active_symbols()
            # FMP-primary: no AV daily-call cap to ration against, so always
            # include the active extended symbols on the live path.
            if active_entries and not cache_only_mode:
                extended_tickers = [e["symbol"] for e in active_entries]
                _extended_theme_by_symbol = {
                    e["symbol"].upper(): (e.get("theme_name") or "")
                    for e in active_entries
                }
                logger.info(
                    "ExtendedWatchlist: adding %d extended symbols to scan: %s",
                    len(extended_tickers),
                    ", ".join(extended_tickers),
                )
            elif active_entries:
                logger.info(
                    "ExtendedWatchlist: cache-only mode — skipping %d extended symbols: %s",
                    len(active_entries),
                    ", ".join(e["symbol"] for e in active_entries),
                )
        except Exception as _ew_err:
            logger.warning("ExtendedWatchlist: failed to load (non-fatal): %s", _ew_err)

    # Merge: static first (priority), then extended (de-duplicated)
    static_upper = {s.upper() for s in static_watchlist}
    extra = [t for t in extended_tickers if t.upper() not in static_upper]
    watchlist = list(static_watchlist) + extra

    # FMP is the primary (and only) market-data client.
    _fmp_client = None
    _ds_cfg = dict(data_sources_config or {})
    if _ds_cfg.get("fmp_enabled", True):
        try:
            from portfolio_automation.data_budget.factory import governed_client
            _fmp_client = governed_client("daily")
            logger.info("FMP client ready (governed)")
        except Exception as exc:
            logger.info("FMP client not available: %s", exc)

    scanner = WatchlistScanner(
        watchlist=watchlist,
        cache=cache,
        price_change_alert_pct=float(config.get("price_change_alert_pct", 3.0)),
        volume_spike_factor=float(config.get("volume_spike_factor", 1.5)),
        theme_score_threshold=float(config.get("theme_score_threshold", 0.40)),
        min_signal_score=float(config.get("min_signal_score", 0.50)),
        signals_config=signals_config,
        ranking_config=ranking_config,
        fmp_client=_fmp_client,
        data_sources=_ds_cfg,
    )

    result = scanner.run(dry_run=cache_only_mode)

    # —— Tag results with watchlist_source and record outcomes —————————————————————
    # Static symbols → "static". Extended (discovery-promoted) symbols carry a
    # finer label "discovery:<theme_name>" when the promoting theme is known,
    # falling back to "extended_theme". watchlist_source is consumed downstream
    # by performance_feedback.py; any string is acceptable there.
    extended_upper = {t.upper() for t in extended_tickers}
    alerted_tickers = {a["ticker"].upper() for a in result.get("alerts", [])}
    for r in result.get("results", []):
        sym = r.get("ticker", "").upper()
        if sym in extended_upper:
            _theme_name = _extended_theme_by_symbol.get(sym, "")
            r["watchlist_source"] = (
                f"discovery:{_theme_name}" if _theme_name else "extended_theme"
            )
        else:
            r["watchlist_source"] = "static"

    # Record scan outcomes for extended symbols
    if _ew_obj and extended_tickers:
        for sym in extended_tickers:
            try:
                _ew_obj.record_scan(sym, alerted=(sym.upper() in alerted_tickers))
            except Exception:
                pass

    # Attach extended watchlist metadata to result
    result["extended_watchlist_meta"] = {
        "extended_tickers": extended_tickers,
        "skipped_for_budget": [
            e["symbol"] for e in (
                (_ew_obj.get_active_symbols() if _ew_obj and not extended_tickers else [])
            )
        ],
    }

    # ── Scraped intelligence (optional, additive, never modifies hard fields) ──
    _si_cfg = scraped_intel_config or {}
    if _si_cfg.get("enabled", False):
        try:
            from scraped_intel.pipeline import run_scraped_intel
            from scraped_intel.export import export_training_rows
            _si_bundles = run_scraped_intel(
                symbols=watchlist,
                config=_si_cfg,
                dry_run=cache_only_mode,
            )
            # Attach scraped_intel dict to each result row (new key only)
            for r in result.get("results", []):
                sym = (r.get("ticker") or "").upper()
                bundle = _si_bundles.get(sym)
                if bundle:
                    r["scraped_intel"] = bundle.to_dict()
            result["scraped_intel_summary"] = {
                "symbols_processed": len(_si_bundles),
                "adapters": _si_cfg.get("adapters", []),
            }
            # Optional training export
            if _si_cfg.get("export_enabled", False) and not manual_dry_run:
                export_training_rows(
                    scan_results=result.get("results", []),
                    bundles=_si_bundles,
                    export_dir=_si_cfg.get("export_dir", "data/training_export"),
                )
            # Optional shadow-mode comparison report
            if _si_cfg.get("comparison_mode", False):
                try:
                    from scraped_intel.comparison import run_comparison
                    _cmp_dir = _si_cfg.get(
                        "comparison_output_dir",
                        output_dir,
                    )
                    _cmp_rows = run_comparison(
                        scan_results=result.get("results", []),
                        bundles=_si_bundles,
                        output_dir=_cmp_dir,
                        config=_si_cfg,
                    )
                    result["scraped_intel_summary"]["comparison_rows"] = len(_cmp_rows)
                    result["scraped_intel_summary"]["comparison_output_dir"] = str(_cmp_dir)
                except Exception as _cmp_err:
                    logger.warning(
                        "scraped_intel comparison: non-fatal error — %s", _cmp_err
                    )
        except Exception as _si_err:
            logger.warning("scraped_intel: non-fatal pipeline error — %s", _si_err)

    result = _apply_alert_cooldown(
        result,
        db_path=ew_cfg.get("db_path", "data/portfolio.db"),
        cooldown_days=int(config.get("alert_cooldown_days", 3)),
        signals_config=signals_config,
    )
    result = _apply_post_cooldown_fallback(
        result,
        signals_config=signals_config,
    )
    scan_status = ((result.get("scan_summary") or {}).get("scan_status")) or "ok"
    data_health = build_data_health_context(
        scan_status=scan_status,
        extra_sources=["fmp"],
    )
    result = _apply_signal_meta_layer(
        result,
        data_health=data_health,
        db_path=ew_cfg.get("db_path", "data/portfolio.db"),
        signals_config=signals_config,
    )
    result = _apply_portfolio_priority_overlay(result, portfolio_context=portfolio_context)
    result = _apply_output_ordering(result)
    result["degraded_mode"] = data_health["degraded_mode"]
    result["degraded_reason"] = data_health["degraded_reason"]
    result["data_sources_used"] = data_health["data_sources_used"]
    result["data_mode"] = data_health["data_mode"]
    result["data_fallback_triggered"] = data_health["data_fallback_triggered"]
    result["scan_summary"] = dict(result.get("scan_summary") or {})
    result["scan_summary"]["degraded_mode"] = data_health["degraded_mode"]
    result["scan_summary"]["degraded_reason"] = data_health["degraded_reason"]
    result["scan_summary"]["data_sources_used"] = data_health["data_sources_used"]
    result["scan_summary"]["data_mode"] = data_health["data_mode"]
    result["scan_summary"]["degraded_confidence_penalty"] = data_health["degraded_confidence_penalty"]
    for row in result.get("results", []):
        confidence = float(row.get("confidence_score", 0.0) or 0.0)
        row["data_mode"] = (
            "live"
            if row.get("data_quality") == "fresh"
            else ("mixed" if row.get("data_quality") == "partial" else "fallback")
        )
        row["degraded_confidence_penalty"] = data_health["degraded_confidence_penalty"]
        row["degraded_confidence_score"] = max(
            0.0,
            round(confidence - data_health["degraded_confidence_penalty"], 3),
        )
    for row in result.get("alerts", []):
        if "degraded_confidence_score" not in row:
            confidence = float(row.get("confidence_score", 0.0) or 0.0)
            row["data_mode"] = result["data_mode"]
            row["degraded_confidence_penalty"] = data_health["degraded_confidence_penalty"]
            row["degraded_confidence_score"] = max(
                0.0,
                round(confidence - data_health["degraded_confidence_penalty"], 3),
            )

    performance_output_dir = Path(output_dir).parent / "performance"
    run_signal_feedback_cycle(
        result,
        db_path=ew_cfg.get("db_path", "data/portfolio.db"),
        cache_dir=cache_dir,
        output_dir=performance_output_dir,
        dry_run=manual_dry_run,
        feedback_config=config.get("performance_feedback", {}),
    )
    # P4.1 — Read prior run's kelly_sizing_advisor.json (if present) so the
    # conviction layer can scale band multipliers by realized hit-rate
    # × win/loss ratio. Read is best-effort; conviction falls back to
    # static multipliers when the artifact is missing or malformed.
    _kelly_plan_for_conviction: dict | None = None
    try:
        _kelly_path = Path(output_dir) / "kelly_sizing_advisor.json"
        if _kelly_path.exists():
            _kelly_plan_for_conviction = json.loads(_kelly_path.read_text(encoding="utf-8"))
    except Exception as _kelly_err:
        logger.warning("conviction: kelly_sizing_advisor.json read failed (non-fatal): %s", _kelly_err)
        _kelly_plan_for_conviction = None

    apply_conviction_layer(
        result,
        conviction_config=config.get("conviction", {}),
        sizing_config=config.get("sizing", {}),
        kelly_plan=_kelly_plan_for_conviction,
    )
    apply_portfolio_construction_layer(
        result,
        portfolio_config=config.get("portfolio_construction", {}),
    )
    _portfolio_out = Path(output_dir).parent / "portfolio"
    _prior_regime = _load_prior_regime(_portfolio_out)
    regime = detect_market_regime(
        results=result.get("results", []),
        portfolio_construction=result.get("portfolio_construction"),
        data_health=data_health,
        regime_inputs=config.get("market_regime", {}),
        prior_regime=_prior_regime,
    )
    regime_commentary = regime_fit_commentary(
        regime=regime,
        portfolio_construction=result.get("portfolio_construction"),
    )
    regime.update(regime_commentary)
    result["market_regime"] = regime
    result["scan_summary"]["market_regime_summary_line"] = regime["regime_summary_line"]
    if isinstance(result.get("portfolio_construction"), dict):
        result["portfolio_construction"]["market_regime"] = regime

    # Write outputs unless dry_run
    if not manual_dry_run:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        portfolio_out = out.parent / "portfolio"
        portfolio_out.mkdir(parents=True, exist_ok=True)
        _write_signals_json(out, result)
        _write_alerts_csv(out, result.get("alerts", []))
        _write_summary_md(out, result)
        _write_portfolio_snapshot_json(portfolio_out, result.get("portfolio_construction") or {})
        _write_portfolio_summary_md(portfolio_out, result.get("portfolio_construction") or {})
    else:
        logger.info("Dry-run: output writes skipped")

    return result


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="watchlist_scanner",
        description="FMP-primary watchlist scanner — fundamentals + news + technicals",
    )
    parser.add_argument("--config",     default="config.json",      help="Path to config.json or config/ directory")
    parser.add_argument("--profile",    default=None,               help="Optional structured config profile name")
    parser.add_argument("--output-dir", default="outputs/latest",   help="Output directory")
    parser.add_argument("--dry-run",    action="store_true",         help="Use cached data only, no API calls")
    parser.add_argument("--debug",      action="store_true",         help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # Load config
    config_path = Path(args.config)
    ws_config: dict = {}
    full_cfg: dict = {}
    try:
        full_cfg = load_runtime_config_dict(str(config_path), profile=args.profile)
        ws_config = full_cfg.get("watchlist_scanner", {})
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        sys.exit(1)

    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not os.environ.get("FMP_API_KEY"):
        print("ERROR: FMP_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    try:
        result = run(
            config=ws_config,
            dry_run=args.dry_run,
            output_dir=ws_config.get("output_dir", args.output_dir),
            extended_watchlist_config=full_cfg.get("extended_watchlist"),
            portfolio_context=broker_overlaid_portfolio(full_cfg.get("portfolio") or {}, Path(".")),
            signals_config=full_cfg.get("signals"),
            ranking_config=full_cfg.get("ranking"),
            scraped_intel_config=full_cfg.get("scraped_intel"),
            data_sources_config=full_cfg.get("data_sources", {}),
        )
    except Exception as exc:
        logger.error("Watchlist scanner failed: %s", exc, exc_info=True)
        sys.exit(1)

    # Print summary to console
    alerts  = result.get("alerts", [])
    results = result.get("results", [])
    scan_summary = result.get("scan_summary", {})
    print(f"\nWatchlist Scanner — {result.get('run_date')}")
    print(f"  Scanned:  {len(results)} symbols")
    print(f"  Alerts:   {len(alerts)}")
    print(f"  API calls used today: {result.get('calls_used', 0)}")
    if scan_summary:
        status = scan_summary.get("scan_status", "ok")
        print(
            f"  Data quality: {scan_summary.get('symbols_fresh', 0)} fresh, "
            f"{scan_summary.get('symbols_partial', 0)} partial, "
            f"{scan_summary.get('symbols_budget_skipped', 0)} budget_skipped, "
            f"{scan_summary.get('symbols_cached', 0)} cached  [{status}]"
        )
    if results:
        n_high   = sum(1 for r in results if r.get("confidence_band") == "high")
        n_medium = sum(1 for r in results if r.get("confidence_band") == "medium")
        n_low    = sum(1 for r in results if r.get("confidence_band") == "low")
        print(f"  Confidence:   {n_high} high, {n_medium} medium, {n_low} low")
    if alerts:
        print("\n  ALERTS:")
        for a in sorted(
            alerts,
            key=lambda x: (x.get("priority_score", 0), x.get("signal_score", 0)),
            reverse=True,
        ):
            pct      = a.get("price_change_pct")
            pct_str  = f"{pct:+.2f}%" if pct is not None else "N/A"
            themes   = ", ".join(a.get("themes") or []) or "—"
            spike    = " [vol-spike]" if a.get("volume_spike") else ""
            fund     = a.get("fundamentals") or {}
            sector   = fund.get("sector") or "N/A"
            avg_sent = a.get("avg_sentiment") or 0.0
            tier     = a.get("alert_tier") or "none"
            priority = float(a.get("priority_score") or 0.0)
            print(
                f"    [{priority:.2f} | {tier}] {a['ticker']:6s} {pct_str:>8s}{spike}"
                f"  sent:{avg_sent:+.2f}  {sector}  themes: {themes}"
            )

    out_dir = ws_config.get("output_dir", args.output_dir)
    if not args.dry_run:
        print(f"\n  Output: {out_dir}/watchlist_signals.json")
        print(f"          {out_dir}/watchlist_alerts.csv")
    print(f"          {out_dir}/watchlist_summary.md")


if __name__ == "__main__":
    main()
