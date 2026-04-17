"""
agent/bundle_builder.py — Build agent_bundle.json from existing engine outputs.

Reads from (all optional — gracefully skips missing files):
  outputs/latest/portfolio_snapshot.csv   → holdings weights, drift
  outputs/latest/contribution_plan.csv    → top 4 allocation recommendations
  outputs/latest/candidates_top20.csv     → scanner top candidates
  outputs/latest/spec_sleeve_plan.csv     → sleeve plan
  data/drawdown_state.json               → current_value, ATH, drawdown
  data/price_cache.json                  → live prices + freshness
  data/finance_history.json             → last snapshot (savings_rate etc.)
  data/portfolio.db                      → latest SQLite snapshot (optional)
  config.json                            → holdings, caps, contribution, flags

Does NOT import any investment-logic modules.
Does NOT call FMP or any external API.
Does NOT modify config.json.

Output written to: outputs/latest/agent_bundle.json
"""

import csv
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agent.io_utils import read_json_safe
from config.loader import load_runtime_config_dict

logger = logging.getLogger("stockbot.agent.bundle_builder")

# Relative paths (all resolved against `root` at call time)
_PORTFOLIO_CSV = "outputs/latest/portfolio_snapshot.csv"
_CONTRIBUTION_CSV = "outputs/latest/contribution_plan.csv"
_CANDIDATES_CSV = "outputs/latest/candidates_top20.csv"
_SLEEVE_CSV = "outputs/latest/spec_sleeve_plan.csv"
_THEME_SIGNALS_JSON = "outputs/latest/theme_signals.json"
_WATCHLIST_SIGNALS_JSON = "outputs/latest/watchlist_signals.json"
_PERFORMANCE_SUMMARY_JSON = "outputs/performance/performance_summary.json"
_REGIME_PERFORMANCE_JSON = "outputs/regime/regime_performance.json"
_POLICY_SIMULATION_JSON = "outputs/simulations/policy_simulation.json"
_POLICY_RECOMMENDATION_JSON = "outputs/policy/policy_recommendation.json"
_DRAWDOWN_JSON = "data/drawdown_state.json"
_PRICE_CACHE_JSON = "data/price_cache.json"
_FINANCE_HISTORY_JSON = "data/finance_history.json"
_DB_PATH = "data/portfolio.db"
_CONFIG_JSON = "config.json"
_BUNDLE_OUT = "outputs/latest/agent_bundle.json"


def build_bundle(mode: str, root: Path) -> dict:
    """
    Build and persist the agent bundle.

    Args:
        mode: Run mode ("daily", "weekly", or "monthly").
        root: Repository root directory.

    Returns:
        The bundle dict (also written to outputs/latest/agent_bundle.json).
    """
    root = Path(root).resolve()
    bundle: dict[str, Any] = {
        "run_mode": mode,
        "generated_at": datetime.now().isoformat(),
        "sources": [],
    }

    # ------------------------------------------------------------------
    # 1. config.json
    # ------------------------------------------------------------------
    config_path = root / "config" if (root / "config").exists() else root / _CONFIG_JSON
    try:
        cfg = load_runtime_config_dict(
            str(config_path),
            profile=os.environ.get("CONFIG_PROFILE") or None,
            record_history=False,
        )
    except Exception:
        cfg = read_json_safe(root / _CONFIG_JSON) or {}
    if cfg:
        bundle["sources"].append(str(config_path.relative_to(root)) if Path(config_path).exists() else _CONFIG_JSON)

    portfolio_cfg = cfg.get("portfolio", {})
    growth_cfg = cfg.get("growth_mode", {})
    email_cfg = cfg.get("email", {})
    scanner_cfg = cfg.get("scanner", {})
    sleeve_cfg = cfg.get("speculative_sleeve", {})

    holdings_cfg: list[dict] = portfolio_cfg.get("holdings", [])
    monthly_contribution: float = float(portfolio_cfg.get("monthly_contribution", 0))
    target_cagr: float = float(growth_cfg.get("target_cagr", 0.09))
    concentration_cap: float = float(growth_cfg.get("concentration_cap", 0.40))
    leverage_cap: float = float(growth_cfg.get("leverage_cap", 0.15))
    expected_returns: dict = growth_cfg.get("expected_returns", {})
    scanner_enabled: bool = bool(scanner_cfg.get("enabled", False))
    sleeve_enabled: bool = bool(sleeve_cfg.get("enabled", False))
    email_enabled: bool = bool(email_cfg.get("enabled", False))

    bundle["config"] = {
        "monthly_contribution": monthly_contribution,
        "target_cagr": target_cagr,
        "concentration_cap": concentration_cap,
        "leverage_cap": leverage_cap,
        "scanner_enabled": scanner_enabled,
        "sleeve_enabled": sleeve_enabled,
        "growth_mode": growth_cfg.get("mode", "unknown"),
        "investor_name": cfg.get("investor", {}).get("name", "unknown"),
        "investor_age": cfg.get("investor", {}).get("age"),
    }

    # ------------------------------------------------------------------
    # 2. Drawdown state
    # ------------------------------------------------------------------
    dd = read_json_safe(root / _DRAWDOWN_JSON) or {}
    if dd:
        bundle["sources"].append(_DRAWDOWN_JSON)

    current_value: float = float(dd.get("current_value", 0.0))
    ath: float = float(dd.get("all_time_high", current_value))
    drawdown_pct: float = (ath - current_value) / ath if ath > 0 else 0.0

    bundle["drawdown"] = {
        "current_value": current_value,
        "all_time_high": ath,
        "rolling_12m_high": float(dd.get("rolling_12m_high", ath)),
        "drawdown_pct": round(drawdown_pct, 4),
        "last_update_date": dd.get("last_update_date"),
    }

    # Derive regime from drawdown
    bundle["drawdown_regime"] = _derive_regime(
        drawdown_pct, growth_cfg.get("drawdown_thresholds", {})
    )

    # ------------------------------------------------------------------
    # 3. Price cache — live prices + freshness
    # ------------------------------------------------------------------
    prices = read_json_safe(root / _PRICE_CACHE_JSON) or {}
    if prices:
        bundle["sources"].append(_PRICE_CACHE_JSON)

    # Find most recent price timestamp for freshness
    price_timestamps = [
        v.get("timestamp", "")
        for v in prices.values()
        if isinstance(v, dict)
    ]
    price_asof = max(price_timestamps) if price_timestamps else None
    bundle["prices"] = {
        sym: {"price": float(v.get("price", 0)), "timestamp": v.get("timestamp")}
        for sym, v in prices.items()
        if isinstance(v, dict)
    }
    bundle["data_freshness"] = {
        "price_asof": price_asof,
        "fundamentals_asof": None,  # set below if scanner CSV found
    }

    # ------------------------------------------------------------------
    # 4. Reconstruct portfolio value + weights from prices + config
    # ------------------------------------------------------------------
    cash: float = float(portfolio_cfg.get("cash_available", 0.0))
    position_values: dict[str, float] = {}
    for h in holdings_cfg:
        sym = h["symbol"]
        p_entry = prices.get(sym, {})
        price = float(p_entry.get("price", 0)) if isinstance(p_entry, dict) else 0.0
        val = float(h.get("shares", 0)) * price
        position_values[sym] = val

    holdings_total = sum(position_values.values())
    portfolio_value: float = holdings_total + cash

    # Use drawdown current_value if prices aren't available (engine run already done)
    if portfolio_value < 1.0 and current_value > 0:
        portfolio_value = current_value

    bundle["portfolio_value"] = round(portfolio_value, 2)
    bundle["cash_available"] = round(cash, 2)

    # ------------------------------------------------------------------
    # 5. Compute expected CAGR (weighted by actual position weights)
    # ------------------------------------------------------------------
    expected_cagr = _compute_expected_cagr(
        holdings_cfg, position_values, cash, portfolio_value, expected_returns
    )
    bundle["expected_cagr"] = round(expected_cagr, 4)

    # ------------------------------------------------------------------
    # 6. Portfolio snapshot CSV (if engine has already run)
    # ------------------------------------------------------------------
    snapshot_rows = _read_csv_safe(root / _PORTFOLIO_CSV)
    if snapshot_rows:
        bundle["sources"].append(_PORTFOLIO_CSV)
        holdings_from_csv: list[dict] = []
        for row in snapshot_rows:
            sym = row.get("Symbol", "")
            if not sym or sym.startswith("#") or sym == "SUMMARY":
                continue
            try:
                holdings_from_csv.append({
                    "symbol": sym,
                    "shares": _safe_float(row.get("Shares")),
                    "price": _safe_float(row.get("Price")),
                    "market_value": _safe_float(row.get("Market_Value")),
                    "target_weight": _safe_float(row.get("Target_Weight")),
                    "actual_weight": _safe_float(row.get("Actual_Weight")),
                    "drift": _safe_float(row.get("Drift")),
                    "status": row.get("Status", ""),
                })
            except Exception:
                pass
        bundle["holdings_snapshot"] = holdings_from_csv

    # ------------------------------------------------------------------
    # 7. Guardrail violations — computed inline (no module imports)
    # ------------------------------------------------------------------
    bundle["guardrails"] = _check_guardrails(
        holdings_cfg=holdings_cfg,
        position_values=position_values,
        total=portfolio_value,
        concentration_cap=concentration_cap,
        leverage_cap=leverage_cap,
        drawdown_pct=drawdown_pct,
        sleeve_enabled=sleeve_enabled,
    )

    # ------------------------------------------------------------------
    # 8. Contribution plan CSV
    # ------------------------------------------------------------------
    contrib_rows = _read_csv_safe(root / _CONTRIBUTION_CSV)
    if contrib_rows:
        bundle["sources"].append(_CONTRIBUTION_CSV)
        plan: list[dict] = []
        for row in contrib_rows:
            sym = row.get("Symbol", "")
            if not sym or sym.startswith("#"):
                continue
            dollars_str = row.get("RecommendedContributionDollars", "0") or "0"
            try:
                dollars = float(dollars_str)
            except ValueError:
                continue
            if dollars <= 0:
                continue
            plan.append({
                "symbol": sym,
                "dollars": round(dollars, 2),
                "asset_class": row.get("AssetClass", ""),
                "drift": _safe_float(row.get("Drift")),
                "reason": row.get("Reason", ""),
            })
        # Top 4 by dollars descending
        plan.sort(key=lambda r: r["dollars"], reverse=True)
        bundle["contribution_plan"] = plan[:4]
    else:
        bundle["contribution_plan"] = []

    # ------------------------------------------------------------------
    # 9. Candidates top 20 (scanner)
    # ------------------------------------------------------------------
    cand_rows = _read_csv_safe(root / _CANDIDATES_CSV)
    if cand_rows:
        bundle["sources"].append(_CANDIDATES_CSV)
        # Grab fundamentals_asof from first row if present
        asof = cand_rows[0].get("fundamentals_asof") or cand_rows[0].get("price_asof")
        if asof:
            bundle["data_freshness"]["fundamentals_asof"] = asof
        bundle["candidates_top20"] = [
            {
                "symbol": r.get("Symbol", r.get("symbol", "")),
                "score": _safe_float(r.get("Score", r.get("score"))),
                "reason": r.get("Reasons", r.get("reasons", r.get("reason", ""))),
            }
            for r in cand_rows[:20]
        ]
    else:
        bundle["candidates_top20"] = None

    # ------------------------------------------------------------------
    # 10. Sleeve plan
    # ------------------------------------------------------------------
    sleeve_rows = _read_csv_safe(root / _SLEEVE_CSV)
    sleeve_total_add: float = 0.0
    if sleeve_rows:
        bundle["sources"].append(_SLEEVE_CSV)
        sleeve_total_add = sum(_safe_float(r.get("MaxAddDollars", 0)) for r in sleeve_rows)
    sleeve_pct = sleeve_total_add / portfolio_value if portfolio_value > 0 else 0.0
    blocked_reason = None
    if not sleeve_enabled:
        blocked_reason = "sleeve disabled in config"
    elif drawdown_pct > 0.20:
        blocked_reason = f"anti-panic gate active (drawdown {drawdown_pct:.1%} > 20%)"
    bundle["sleeve_status"] = {
        "enabled": sleeve_enabled,
        "planned_add_dollars": round(sleeve_total_add, 2),
        "pct_of_portfolio": round(sleeve_pct, 4),
        "blocked_reason": blocked_reason,
    }

    # ------------------------------------------------------------------
    # 11. Finance history — last entry
    # ------------------------------------------------------------------
    fh = read_json_safe(root / _FINANCE_HISTORY_JSON)
    if fh and isinstance(fh, list) and fh:
        bundle["sources"].append(_FINANCE_HISTORY_JSON)
        bundle["finance_history_latest"] = fh[-1]

    # ------------------------------------------------------------------
    # 12. SQLite latest snapshot (optional)
    # ------------------------------------------------------------------
    db_path = root / _DB_PATH
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM snapshots ORDER BY recorded_at DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                bundle["sqlite_latest_snapshot"] = dict(row)
                bundle["sources"].append("data/portfolio.db (snapshots)")
        except Exception as exc:
            logger.debug("SQLite snapshot read failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # 13. Theme signals (optional — written by theme engine)
    # ------------------------------------------------------------------
    theme_signals_path = root / _THEME_SIGNALS_JSON
    if theme_signals_path.exists():
        bundle["theme_signals"] = read_json_safe(theme_signals_path)
        bundle["sources"].append(_THEME_SIGNALS_JSON)
    else:
        bundle["theme_signals"] = None

    # ------------------------------------------------------------------
    # 14. Watchlist signal summary (optional — written by watchlist scanner)
    # ------------------------------------------------------------------
    watchlist_signals_path = root / _WATCHLIST_SIGNALS_JSON
    if watchlist_signals_path.exists():
        watchlist_payload = read_json_safe(watchlist_signals_path)
        bundle["watchlist_signal_summary"] = _build_watchlist_signal_summary(watchlist_payload)
        bundle["portfolio_construction_view"] = _build_portfolio_construction_summary(watchlist_payload)
        bundle["market_regime"] = _build_market_regime_summary(watchlist_payload)
        bundle["sources"].append(_WATCHLIST_SIGNALS_JSON)
    else:
        bundle["watchlist_signal_summary"] = None
        bundle["portfolio_construction_view"] = None
        bundle["market_regime"] = None

    # ------------------------------------------------------------------
    # 15. Signal performance summary (optional)
    # ------------------------------------------------------------------
    performance_summary_path = root / _PERFORMANCE_SUMMARY_JSON
    if performance_summary_path.exists():
        bundle["signal_performance_summary"] = read_json_safe(performance_summary_path)
        bundle["sources"].append(_PERFORMANCE_SUMMARY_JSON)
    else:
        bundle["signal_performance_summary"] = None

    regime_performance_path = root / _REGIME_PERFORMANCE_JSON
    if regime_performance_path.exists():
        bundle["regime_performance_summary"] = read_json_safe(regime_performance_path)
        bundle["sources"].append(_REGIME_PERFORMANCE_JSON)
    else:
        bundle["regime_performance_summary"] = None

    policy_simulation_path = root / _POLICY_SIMULATION_JSON
    if policy_simulation_path.exists():
        bundle["policy_simulation_summary"] = read_json_safe(policy_simulation_path)
        bundle["sources"].append(_POLICY_SIMULATION_JSON)
    else:
        bundle["policy_simulation_summary"] = None

    policy_recommendation_path = root / _POLICY_RECOMMENDATION_JSON
    if policy_recommendation_path.exists():
        bundle["policy_recommendation"] = read_json_safe(policy_recommendation_path)
        bundle["sources"].append(_POLICY_RECOMMENDATION_JSON)
    else:
        bundle["policy_recommendation"] = None

    # ------------------------------------------------------------------
    # 17. Email / should_email
    # ------------------------------------------------------------------
    bundle["should_email"] = email_enabled
    bundle["email_reason"] = (
        "email.enabled=true in config" if email_enabled
        else "email.enabled=false in config"
    )

    # ------------------------------------------------------------------
    # 18. Write bundle to outputs/latest/
    # ------------------------------------------------------------------
    out_path = root / _BUNDLE_OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_path.write_text(
            json.dumps(bundle, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("agent_bundle.json written → %s", out_path)
    except Exception as exc:
        logger.warning("Failed to write agent_bundle.json: %s", exc)

    return bundle


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_regime(drawdown_pct: float, thresholds: dict) -> str:
    deploy_all = float(thresholds.get("deploy_all_cash", 0.30))
    aggressive = float(thresholds.get("aggressive_equity_tilt", 0.20))
    modest = float(thresholds.get("modest_equity_tilt", 0.10))
    if drawdown_pct >= deploy_all:
        return "deploy_all_cash"
    if drawdown_pct >= aggressive:
        return "aggressive_equity_tilt"
    if drawdown_pct >= modest:
        return "modest_equity_tilt"
    return "normal"


def _build_watchlist_signal_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    results = payload.get("results")
    if not isinstance(results, list):
        return None

    scan_summary = payload.get("scan_summary") if isinstance(payload.get("scan_summary"), dict) else {}

    def _signal_item(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticker": row.get("ticker", ""),
            "signal_score": _safe_float(row.get("signal_score")),
            "confidence_score": _safe_float(row.get("confidence_score")),
            "effective_score": _safe_float(row.get("effective_score")),
            "conviction_score": _safe_float(row.get("conviction_score")),
            "conviction_band": row.get("conviction_band", ""),
            "sizing_recommendation": row.get("sizing_recommendation", ""),
            "target_allocation_band": row.get("target_allocation_band", ""),
            "notification_status": row.get("notification_status", ""),
            "notification_reason": row.get("action_suppression_reason") or row.get("notification_reason", ""),
            "cooldown_active": bool(row.get("cooldown_active", False)),
        }

    high_confidence = [
        row for row in results
        if float(row.get("confidence_score") or 0.0) >= 0.80
        and row.get("notification_status") == "alerted"
    ]
    high_confidence.sort(
        key=lambda row: (
            float(row.get("effective_score") or 0.0),
            float(row.get("signal_score") or 0.0),
        ),
        reverse=True,
    )

    suppressed = [
        row for row in results
        if row.get("cooldown_active") or row.get("action_suppressed")
    ]
    suppressed.sort(
        key=lambda row: (
            float(row.get("effective_score") or 0.0),
            float(row.get("signal_score") or 0.0),
        ),
        reverse=True,
    )

    high_conviction = [
        row for row in results
        if row.get("conviction_band") == "high_conviction"
    ]
    high_conviction.sort(
        key=lambda row: (
            float(row.get("conviction_score") or 0.0),
            float(row.get("effective_score") or 0.0),
        ),
        reverse=True,
    )

    starter_sized = [
        row for row in results
        if row.get("conviction_band") == "starter"
    ]
    starter_sized.sort(
        key=lambda row: (
            float(row.get("conviction_score") or 0.0),
            float(row.get("effective_score") or 0.0),
        ),
        reverse=True,
    )

    deferred = [
        row for row in results
        if row.get("conviction_band") in {"defer", "observe"}
    ]
    deferred.sort(
        key=lambda row: (
            float(row.get("conviction_score") or 0.0),
            float(row.get("effective_score") or 0.0),
        ),
    )

    return {
        "data_mode": payload.get("data_mode", "live"),
        "degraded_mode": bool(payload.get("degraded_mode", False)),
        "suppressed_signals_count": int(scan_summary.get("signals_suppressed", len(suppressed)) or 0),
        "cooldown_hits": int(scan_summary.get("cooldown_hits", 0) or 0),
        "conviction_band_counts": dict(scan_summary.get("conviction_band_counts") or {}),
        "conviction_summary_line": str(scan_summary.get("conviction_summary_line") or ""),
        "high_confidence_signals": [_signal_item(row) for row in high_confidence[:3]],
        "high_conviction_candidates": [_signal_item(row) for row in high_conviction[:3]],
        "starter_sized_ideas": [_signal_item(row) for row in starter_sized[:3]],
        "deferred_signals": [_signal_item(row) for row in deferred[:3]],
        "suppressed_signals": [_signal_item(row) for row in suppressed[:3]],
    }


def _build_portfolio_construction_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    portfolio_view = payload.get("portfolio_construction")
    if not isinstance(portfolio_view, dict):
        return None

    rows = list(portfolio_view.get("rows") or [])

    def _row_item(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticker": row.get("ticker", ""),
            "conviction_score": _safe_float(row.get("conviction_score")),
            "conviction_band": row.get("conviction_band", ""),
            "suggested_allocation": _safe_float(row.get("suggested_allocation")),
            "normalized_allocation": _safe_float(row.get("normalized_allocation")),
            "allocation_cap_reason": row.get("allocation_cap_reason", ""),
            "sector": row.get("sector", "Unknown"),
        }

    high_allocations = [
        row for row in rows
        if _safe_float(row.get("normalized_allocation")) > 0
    ]
    high_allocations.sort(
        key=lambda row: (
            float(row.get("normalized_allocation") or 0.0),
            float(row.get("conviction_score") or 0.0),
        ),
        reverse=True,
    )

    capped = [
        row for row in rows
        if row.get("allocation_capped")
    ]
    capped.sort(
        key=lambda row: (
            float(row.get("normalized_allocation") or 0.0),
            float(row.get("conviction_score") or 0.0),
        ),
        reverse=True,
    )

    return {
        "summary_label": str(portfolio_view.get("summary_label") or "balanced"),
        "summary_line": str(portfolio_view.get("summary_line") or ""),
        "total_suggested_allocation": _safe_float(portfolio_view.get("total_suggested_allocation")),
        "total_normalized_allocation": _safe_float(portfolio_view.get("total_normalized_allocation")),
        "capped_positions": int(portfolio_view.get("capped_positions", 0) or 0),
        "sectors_capped": list(portfolio_view.get("sectors_capped") or []),
        "warnings": list(portfolio_view.get("warnings") or []),
        "top_sector": dict(portfolio_view.get("top_sector") or {}),
        "top_3_ticker_concentration_pct": _safe_float(portfolio_view.get("top_3_ticker_concentration_pct")),
        "allocation_by_sector": dict(portfolio_view.get("allocation_by_sector") or {}),
        "high_allocation_candidates": [_row_item(row) for row in high_allocations[:5]],
        "capped_candidates": [_row_item(row) for row in capped[:5]],
    }


def _build_market_regime_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    regime = payload.get("market_regime")
    if not isinstance(regime, dict):
        return None
    return {
        "regime_label": str(regime.get("regime_label") or "neutral"),
        "regime_confidence": _safe_float(regime.get("regime_confidence")),
        "regime_reasoning": str(regime.get("regime_reasoning") or ""),
        "regime_summary_line": str(regime.get("regime_summary_line") or ""),
        "regime_data_quality": str(regime.get("regime_data_quality") or "limited"),
        "regime_inputs": dict(regime.get("regime_inputs") or {}),
        "regime_portfolio_fit": regime.get("regime_portfolio_fit"),
        "regime_portfolio_commentary": regime.get("regime_portfolio_commentary"),
    }


def _compute_expected_cagr(
    holdings_cfg: list[dict],
    position_values: dict[str, float],
    cash: float,
    total: float,
    expected_returns: dict,
) -> float:
    if total <= 0:
        return 0.0
    cagr = 0.0
    for h in holdings_cfg:
        sym = h["symbol"]
        asset_class = h.get("asset_class", "us_equity")
        val = position_values.get(sym, 0.0)
        weight = val / total
        er = float(expected_returns.get(asset_class, 0.08))
        cagr += weight * er
    # Cash portion
    cash_weight = cash / total
    cagr += cash_weight * float(expected_returns.get("cash", 0.04))
    return cagr


def _check_guardrails(
    holdings_cfg: list[dict],
    position_values: dict[str, float],
    total: float,
    concentration_cap: float,
    leverage_cap: float,
    drawdown_pct: float,
    sleeve_enabled: bool,
) -> dict:
    violations: list[dict] = []
    if total <= 0:
        return {"pass": False, "violations": [{"rule": "data_error", "detail": "total=0"}]}

    for h in holdings_cfg:
        sym = h["symbol"]
        weight = position_values.get(sym, 0.0) / total
        if weight > concentration_cap:
            violations.append({
                "rule": "concentration_cap",
                "symbol": sym,
                "actual_weight": round(weight, 4),
                "cap": concentration_cap,
                "excess": round(weight - concentration_cap, 4),
            })

    lev_exposure = sum(
        (position_values.get(h["symbol"], 0.0) / total) * h.get("leverage_factor", 1)
        for h in holdings_cfg
        if h.get("is_leveraged", False)
    )
    if lev_exposure > leverage_cap:
        violations.append({
            "rule": "leverage_cap",
            "actual_effective_exposure": round(lev_exposure, 4),
            "cap": leverage_cap,
            "excess": round(lev_exposure - leverage_cap, 4),
        })

    if drawdown_pct > 0.20 and sleeve_enabled:
        violations.append({
            "rule": "anti_panic_sleeve_block",
            "drawdown_pct": round(drawdown_pct, 4),
            "threshold": 0.20,
        })

    return {"pass": len(violations) == 0, "violations": violations}


def _read_csv_safe(path: Path) -> list[dict]:
    """Read a CSV into a list of dicts. Returns [] on any error."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
