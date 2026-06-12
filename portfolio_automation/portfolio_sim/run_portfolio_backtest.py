"""
Backtest orchestrator: build tactics → run each tactic × policy × window →
rank by excess-vs-SPY → contribution sensitivity → strategy catalog → artifacts.

Sandbox-only, observe-only. Reads the HISTORICAL price archive; writes SANDBOX
artifacts + the auto-generated docs/STRATEGY_CATALOG.md. Never raises into the
pipeline; never writes decision_plan / config / registry.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)
from portfolio_automation.run_mode_governance import (
    RunMode,
    assert_can_write_namespace,
    normalize_run_mode,
)
from portfolio_automation.portfolio_sim.backtest_engine import (
    benchmark_total_return,
    run_backtest,
)
from portfolio_automation.portfolio_sim.prices import load_price_panel
from portfolio_automation.portfolio_sim.rebalance import make_policy
from portfolio_automation.portfolio_sim.sim_base import SimStatus, sim_envelope, utc_now_iso
from portfolio_automation.portfolio_sim.strategy_docs import (
    build_strategy_catalog,
    render_strategy_catalog_md,
)
from portfolio_automation.portfolio_sim.tactics import all_static_tactics
from portfolio_automation.portfolio_sim.windows import resolve_windows

logger = logging.getLogger("stockbot.portfolio_sim.run_backtest")

_BACKTEST_JSON = "portfolio_backtest.json"
_BACKTEST_MD = "portfolio_backtest_summary.md"
_CATALOG_JSON = "strategy_catalog.json"
_CATALOG_DOC = "docs/STRATEGY_CATALOG.md"
_CROWD_BACKTEST_JSON = "crowd_tactic_backtest.json"

_DEFAULTS = {
    "enabled": False,
    "primary_benchmark": "SPY",
    "secondary_benchmarks": ["QQQ"],
    "monthly_contribution": 1000,
    "contribution_scenarios": [500, 1000, 2000],
    "windows": ["ytd", "trailing_1y", "trailing_3y", "trailing_5y",
                "calendar_quarter", "calendar_month"],
    "rebalance_policies": ["buy_and_hold", "periodic"],
    "start_value": 10000.0,
}


def _config(root: Path) -> dict[str, Any]:
    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
        cfg = dict(_DEFAULTS)
        cfg.update(raw.get("portfolio_sim") or {})
        cfg["_rebalance_rules"] = raw.get("rebalance_rules") or {}
        return cfg
    except Exception:
        return dict(_DEFAULTS, _rebalance_rules={})


def run_portfolio_backtest(
    root: str | Path = ".",
    run_mode: str | RunMode = "discovery",
    run_id: str | None = None,
    *,
    write_files: bool = True,
) -> dict[str, Any]:
    root = Path(root)
    run_id = run_id or utc_now_iso()
    try:
        mode = normalize_run_mode(run_mode)
    except Exception:
        mode = RunMode.DISCOVERY
    cfg = _config(root)
    warnings: list[str] = []

    if not cfg.get("enabled"):
        warnings.append("portfolio_sim.enabled=false")
        return _write_disabled(root, run_id, mode, warnings, write_files)

    tactics = all_static_tactics(root)
    if not tactics:
        warnings.append("no_tactics_materialized")
        return _write_disabled(root, run_id, mode, warnings, write_files,
                               status=SimStatus.INSUFFICIENT_DATA.value)

    # Load a panel covering all tactic tickers + benchmarks.
    benchmarks = [cfg["primary_benchmark"], *cfg.get("secondary_benchmarks", [])]
    tickers = sorted({t for tac in tactics for t in tac.target_weights} | set(benchmarks))
    panel = load_price_panel(tickers, root)
    if len(panel.dates) < 2:
        warnings.append("price_panel_empty")
        return _write_disabled(root, run_id, mode, warnings, write_files,
                               status=SimStatus.INSUFFICIENT_DATA.value)
    if panel.missing:
        warnings.append(f"missing_price_history:{','.join(panel.missing[:8])}")

    windows = resolve_windows(cfg["windows"], panel.dates)
    policies = [make_policy(n, rebalance_rules=cfg["_rebalance_rules"])
                for n in cfg["rebalance_policies"]]
    monthly = float(cfg["monthly_contribution"])
    start_value = float(cfg["start_value"])

    results: list[dict[str, Any]] = []
    results_by_tactic: dict[str, list[dict[str, Any]]] = {}
    for win in windows:
        bench_ret = {b: benchmark_total_return(panel, b, win) for b in benchmarks}
        for tac in tactics:
            for pol in policies:
                r = run_backtest(tac, pol, panel, win, start_value=start_value,
                                 monthly_contribution=monthly, benchmark_returns=bench_ret)
                if r.metrics.get("status") != "ok":
                    continue
                row = {"tactic_id": tac.tactic_id, "name": tac.name, "source": tac.source,
                       "approximate": tac.approximate, "policy": pol.name,
                       "window": win.key, **r.metrics, "degraded": r.degraded,
                       "value_series": r.value_series}
                results.append(row)
                results_by_tactic.setdefault(tac.tactic_id, []).append(row)

    # Rank by the operator objective (excess vs SPY) within each window.
    leaderboard: dict[str, list[dict[str, Any]]] = {}
    for win in windows:
        rows = [r for r in results if r["window"] == win.key]
        rows.sort(key=lambda r: r.get("excess_vs_spy", 0.0), reverse=True)
        leaderboard[win.key] = [{k: r[k] for k in
                                 ("tactic_id", "name", "policy", "excess_vs_spy",
                                  "cagr", "max_drawdown", "sharpe", "final_balance_dca")}
                                for r in rows]

    # Contribution sensitivity ("based on how much money I put in") — actual baseline.
    contrib_sens = _contribution_sensitivity(tactics, panel, windows, cfg, start_value)

    # Crowd-signal tactic — labeled volume/momentum PROXY backtest (not the real
    # crowd record; the real evaluation is the forward shadow-track ledger).
    crowd_proxy = _run_crowd_proxy(tactics, panel, windows, cfg, start_value, monthly, run_id, mode)

    catalog = build_strategy_catalog(tactics, results_by_tactic)

    env = sim_envelope(run_id=run_id, run_mode=mode.value,
                       status=SimStatus.OK.value, warnings=warnings)
    payload = {**env,
               "objective": cfg.get("objective", "maximize_excess_vs_sp500"),
               "primary_benchmark": cfg["primary_benchmark"],
               "windows": [w.key for w in windows],
               "policies": [p.name for p in policies],
               "monthly_contribution": monthly,
               "tactic_count": len(tactics),
               "result_count": len(results),
               "leaderboard": leaderboard,
               "contribution_sensitivity": contrib_sens,
               "results": results}

    artifacts: dict[str, str] = {}
    wrote = False
    if write_files:
        try:
            assert_can_write_namespace(mode, OutputNamespace.SANDBOX)
            base = root / "outputs"
            artifacts["portfolio_backtest"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _BACKTEST_JSON, payload, base_dir=base))
            artifacts["portfolio_backtest_summary"] = str(
                safe_write_text(OutputNamespace.SANDBOX, _BACKTEST_MD,
                                _render_summary_md(payload), base_dir=base))
            artifacts["strategy_catalog"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _CATALOG_JSON,
                                {**env, **catalog}, base_dir=base))
            artifacts["crowd_tactic_backtest"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _CROWD_BACKTEST_JSON, crowd_proxy, base_dir=base))
            # Auto-generated doc (repo docs/, not a namespace artifact).
            doc_path = root / _CATALOG_DOC
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text(render_strategy_catalog_md(catalog), encoding="utf-8")
            artifacts["strategy_catalog_doc"] = _CATALOG_DOC
            wrote = True
        except Exception as exc:
            logger.warning("portfolio_sim backtest: write skipped/failed (%s)", exc)
            warnings.append(f"write_skipped:{exc}")

    return {"status": SimStatus.OK.value, "run_mode": mode.value,
            "tactic_count": len(tactics), "result_count": len(results),
            "coverage_complete": catalog["coverage_complete"],
            "wrote_files": wrote, "artifacts": artifacts, "warnings": warnings,
            "observe_only": True, "sandbox_only": True}


def _contribution_sensitivity(tactics, panel, windows, cfg, start_value) -> dict[str, Any]:
    """Final DCA balance of the actual baseline per (window × contribution level)."""
    baseline = next((t for t in tactics if t.tactic_id == "shadow_actual_baseline"), None)
    if baseline is None or not windows:
        return {}
    pol = make_policy("periodic", rebalance_rules=cfg["_rebalance_rules"])
    out: dict[str, Any] = {"tactic_id": baseline.tactic_id, "by_window": {}}
    for win in windows:
        per_scenario = {}
        for amt in cfg["contribution_scenarios"]:
            r = run_backtest(baseline, pol, panel, win, start_value=start_value,
                             monthly_contribution=float(amt))
            if r.metrics.get("status") == "ok":
                per_scenario[str(amt)] = {
                    "final_balance_dca": r.metrics["final_balance_dca"],
                    "total_contributed": r.metrics["total_contributed"],
                    "net_gain_dca": r.metrics["net_gain_dca"]}
        out["by_window"][win.key] = per_scenario
    return out


def _run_crowd_proxy(tactics, panel, windows, cfg, start_value, monthly, run_id, mode) -> dict[str, Any]:
    """Run the crowd-signal tactic in labeled PROXY mode over the windows."""
    from portfolio_automation.portfolio_sim.crowd_tactic import CrowdTactic

    env = sim_envelope(run_id=run_id, run_mode=mode.value, status=SimStatus.OK.value)
    baseline = next((t for t in tactics if t.tactic_id == "shadow_actual_baseline"), None)
    if baseline is None or not windows:
        return {**env, "proxy": True, "available": False,
                "measures": "volume/momentum attention, NOT real crowd evidence/sentiment",
                "results": []}
    pol = make_policy("periodic", rebalance_rules=cfg["_rebalance_rules"])
    crowd = CrowdTactic(baseline.target_weights, mode="proxy", proxy_universe=panel.tickers)
    bspy = cfg["primary_benchmark"]
    rows = []
    for win in windows:
        bench = {bspy: benchmark_total_return(panel, bspy, win)}
        r = run_backtest(crowd, pol, panel, win, start_value=start_value,
                         monthly_contribution=monthly, benchmark_returns=bench)
        if r.metrics.get("status") == "ok":
            rows.append({"window": win.key, "window_label": win.label, **r.metrics,
                         "degraded": r.degraded})
    return {**env, "proxy": True, "available": bool(rows),
            "measures": "volume/momentum attention, NOT real crowd evidence/sentiment",
            "forward_maturing_note": "Real evaluation is the forward shadow-track in social_signal_backtest.json.",
            "results": rows}


def _render_summary_md(payload: dict[str, Any]) -> str:
    lines = ["# Portfolio Backtest — Sandbox", "",
             "_Observe-only simulation. Not a trade recommendation. Objective: "
             "maximize excess return vs the S&P 500 (SPY)._", ""]
    for win, rows in (payload.get("leaderboard") or {}).items():
        if not rows:
            continue
        lines.append(f"## {win} — top by excess vs SPY")
        for r in rows[:5]:
            lines.append(f"- **{r['name']}** ({r['policy']}): excess vs SPY "
                         f"{r['excess_vs_spy']:+.2%} · CAGR {r['cagr']:+.2%} · "
                         f"maxDD {r['max_drawdown']:.2%} · Sharpe {r['sharpe']:.2f}")
        lines.append("")
    return "\n".join(lines)


def _write_disabled(root, run_id, mode, warnings, write_files, status=SimStatus.DISABLED.value):
    env = sim_envelope(run_id=run_id, run_mode=mode.value, status=status, warnings=warnings)
    payload = {**env, "leaderboard": {}, "results": [], "tactic_count": 0}
    wrote = False
    if write_files:
        try:
            assert_can_write_namespace(mode, OutputNamespace.SANDBOX)
            safe_write_json(OutputNamespace.SANDBOX, _BACKTEST_JSON, payload, base_dir=root / "outputs")
            safe_write_text(OutputNamespace.SANDBOX, _BACKTEST_MD,
                            "# Portfolio Backtest — Sandbox\n\n_Disabled or insufficient data._\n",
                            base_dir=root / "outputs")
            wrote = True
        except Exception as exc:
            warnings.append(f"write_skipped:{exc}")
    return {"status": status, "run_mode": mode.value, "wrote_files": wrote,
            "artifacts": {}, "warnings": warnings, "observe_only": True, "sandbox_only": True}


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Portfolio tactic backtest (sandbox)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--run-mode", default="discovery")
    args = ap.parse_args()
    print(json.dumps(run_portfolio_backtest(root=args.root, run_mode=args.run_mode), indent=2, default=str))
