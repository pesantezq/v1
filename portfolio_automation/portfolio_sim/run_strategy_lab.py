"""
Research-Backed Strategy Lab orchestrator.

Builds the suite tactics + research-library tactics, backtests each across windows,
computes the master strategy score (after-cost-ish, risk-adjusted excess vs SPY,
consistency, research support, minus overfit/turnover/tax/concentration/leverage),
and writes a leaderboard ranked by score + a research strategy catalog.

Sandbox-only, observe-only, default-disabled. Overfit penalty is filled by the
walk-forward phase; until then it is `None` (flagged overfit_unknown). Tax/turnover
use documented proxies flagged `gross_until_cost_model`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json, safe_write_text
from portfolio_automation.run_mode_governance import (
    RunMode, assert_can_write_namespace, normalize_run_mode,
)
from portfolio_automation.portfolio_sim.backtest_engine import benchmark_total_return, run_backtest
from portfolio_automation.portfolio_sim.prices import load_price_panel
from portfolio_automation.portfolio_sim.rebalance import make_policy
from portfolio_automation.portfolio_sim.research_library import research_tactics
from portfolio_automation.portfolio_sim.sim_base import SimStatus, sim_envelope, utc_now_iso
from portfolio_automation.portfolio_sim.strategy_score import rank, score
from portfolio_automation.portfolio_sim.tactics import TimeVaryingTactic, all_static_tactics
from portfolio_automation.portfolio_sim.windows import resolve_windows

logger = logging.getLogger("stockbot.portfolio_sim.run_strategy_lab")

_LEADERBOARD_JSON = "strategy_leaderboard.json"
_LEADERBOARD_MD = "strategy_leaderboard_summary.md"
_CATALOG_JSON = "research_strategy_catalog.json"
_WALK_FORWARD_JSON = "walk_forward_results.json"

_DEFAULT_WINDOWS = ["trailing_1y", "trailing_3y", "trailing_5y", "ytd"]


def _config(root: Path) -> dict[str, Any]:
    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
    except Exception:
        raw = {}
    ps = raw.get("portfolio_sim") or {}
    lab = ps.get("strategy_lab") or {}
    holdings = (raw.get("portfolio") or {}).get("holdings", []) or []
    return {
        "enabled": bool(ps.get("enabled")) and bool(lab.get("enabled", True)),
        "lab_enabled_explicit": lab.get("enabled"),
        "windows": lab.get("windows", _DEFAULT_WINDOWS),
        "monthly_contribution": float(ps.get("monthly_contribution", 1000)),
        "start_value": float(ps.get("start_value", 10000.0)),
        "primary_benchmark": ps.get("primary_benchmark", "SPY"),
        "scoring_weights": lab.get("scoring") or {},
        "rebalance_rules": raw.get("rebalance_rules") or {},
        "leveraged": {str(h.get("symbol", "")).upper() for h in holdings if h.get("is_leveraged")},
    }


def _walk_forward_results(panel, cfg) -> dict[str, Any]:
    """Walk-forward OOS validation for the parameterized tactics (momentum)."""
    from portfolio_automation.portfolio_sim.research_library import MomentumRotation
    from portfolio_automation.portfolio_sim.walk_forward import walk_forward
    universe = sorted({t for t in panel.tickers if t != cfg["primary_benchmark"]})
    equities = [t for t in universe if t not in {"BND", "TLT", "GLD", "IAU"}]
    grid = [{"lookback_months": lb, "top_n": n} for lb in (3, 6, 12) for n in (1, 2, 3)]
    build = lambda p: MomentumRotation(equities or universe, lookback_months=p["lookback_months"],
                                       top_n=p["top_n"], leveraged=cfg["leveraged"])
    out: dict[str, Any] = {}
    try:
        out["research_momentum_rotation"] = walk_forward(
            build, grid, panel, benchmark=cfg["primary_benchmark"], train_months=24, test_months=3)
    except Exception as exc:  # pragma: no cover
        logger.debug("walk_forward failed (%s)", exc)
    return out


def _score_tactic(tac, panel, windows, bench, cfg, overfit_by_tactic=None) -> dict[str, Any] | None:
    pol = make_policy("periodic", rebalance_rules=cfg["rebalance_rules"])
    excesses, drawdowns, finals = [], [], []
    for win in windows:
        r = run_backtest(tac, pol, panel, win, start_value=cfg["start_value"],
                         monthly_contribution=cfg["monthly_contribution"],
                         benchmark_returns={cfg["primary_benchmark"]: bench[win.key]})
        if r.metrics.get("status") != "ok":
            continue
        excesses.append(r.metrics["excess_vs_spy"])
        drawdowns.append(r.metrics["max_drawdown"])
        finals.append({"window": win.key, "excess_vs_spy": r.metrics["excess_vs_spy"],
                       "cagr": r.metrics["cagr"], "max_drawdown": r.metrics["max_drawdown"],
                       "sharpe": r.metrics["sharpe"], "final_balance_dca": r.metrics["final_balance_dca"]})
    if not excesses:
        return None
    mean_excess = sum(excesses) / len(excesses)
    prob_beat = sum(1 for e in excesses if e > 0) / len(excesses)
    worst_dd = min(drawdowns) if drawdowns else 0.0
    conc = max(tac.target_weights.values()) if tac.target_weights else 0.0
    lev = sum(w for t, w in tac.target_weights.items() if t in cfg["leveraged"])
    turnover = 0.7 if isinstance(tac, TimeVaryingTactic) else 0.3
    has_research = bool(tac.metadata.get("academic_basis"))
    wf = (overfit_by_tactic or {}).get(tac.tactic_id)
    overfit = wf.get("overfit") if isinstance(wf, dict) and wf.get("status") == "ok" else None
    components = {
        "excess_return_vs_spy": mean_excess, "probability_beat_spy": prob_beat,
        "drawdown": worst_dd, "consistency": prob_beat, "has_research": has_research,
        "turnover": turnover, "tax_drag": 0.0, "concentration": conc, "leverage": lev,
        "overfit": overfit,
    }
    sc = score(components, cfg["scoring_weights"])
    return {"tactic_id": tac.tactic_id, "name": tac.name, "source": tac.source,
            "approximate": tac.approximate,
            "academic_basis": tac.metadata.get("academic_basis", ""),
            "strategy_score": sc["strategy_score"], "flags": sc["flags"],
            "mean_excess_vs_spy": round(mean_excess, 6), "prob_beat_spy": round(prob_beat, 4),
            "worst_max_drawdown": round(worst_dd, 6), "by_window": finals,
            "overfit": overfit,
            "still_works_oos": (wf.get("still_works_oos") if isinstance(wf, dict) and wf.get("status") == "ok" else None),
            "tax_note": "gross_until_cost_model"}


def run_strategy_lab(root: str | Path = ".", run_mode: str | RunMode = "discovery",
                     run_id: str | None = None, *, write_files: bool = True) -> dict[str, Any]:
    root = Path(root)
    run_id = run_id or utc_now_iso()
    try:
        mode = normalize_run_mode(run_mode)
    except Exception:
        mode = RunMode.DISCOVERY
    cfg = _config(root)
    warnings: list[str] = []

    if not cfg["enabled"]:
        warnings.append("portfolio_sim.strategy_lab disabled")
        return _write(root, run_id, mode, SimStatus.DISABLED.value, warnings, [], write_files)

    tactics = all_static_tactics(root) + research_tactics(root)
    bench_t = cfg["primary_benchmark"]
    tickers = sorted({t for tac in tactics for t in tac.target_weights} | {bench_t})
    panel = load_price_panel(tickers, root)
    if len(panel.dates) < 2:
        warnings.append("price_panel_empty")
        return _write(root, run_id, mode, SimStatus.INSUFFICIENT_DATA.value, warnings, [], write_files)
    if panel.missing:
        warnings.append(f"missing_price_history:{','.join(panel.missing[:8])}")

    windows = resolve_windows(cfg["windows"], panel.dates)
    bench = {w.key: benchmark_total_return(panel, bench_t, w) for w in windows}

    wf_results = _walk_forward_results(panel, cfg)
    scored = [s for s in (_score_tactic(t, panel, windows, bench, cfg, wf_results) for t in tactics) if s]
    leaderboard = rank(scored)
    status = SimStatus.OK.value if leaderboard else SimStatus.INSUFFICIENT_DATA.value
    return _write(root, run_id, mode, status, warnings, leaderboard, write_files,
                  windows=[w.key for w in windows], wf_results=wf_results)


def _write(root, run_id, mode, status, warnings, leaderboard, write_files, windows=None,
           wf_results=None) -> dict[str, Any]:
    env = sim_envelope(run_id=run_id, run_mode=mode.value, status=status, warnings=warnings)
    coverage_complete = all(row.get("academic_basis") or row["source"] in ("shadow", "baseline",
                            "strategy_profile", "benchmark") for row in leaderboard)
    payload = {**env, "objective": "maximize_excess_vs_sp500", "windows": windows or [],
               "tactic_count": len(leaderboard), "leaderboard": leaderboard}
    catalog = {**env, "coverage_complete": coverage_complete,
               "undocumented": [r["tactic_id"] for r in leaderboard
                                if not r.get("academic_basis") and r["source"] not in
                                ("shadow", "baseline", "strategy_profile", "benchmark")],
               "cards": leaderboard}
    artifacts: dict[str, str] = {}
    wrote = False
    if write_files:
        try:
            assert_can_write_namespace(mode, OutputNamespace.SANDBOX)
            base = root / "outputs"
            artifacts["strategy_leaderboard"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _LEADERBOARD_JSON, payload, base_dir=base))
            safe_write_text(OutputNamespace.SANDBOX, _LEADERBOARD_MD, _render_md(payload), base_dir=base)
            safe_write_json(OutputNamespace.SANDBOX, _CATALOG_JSON, catalog, base_dir=base)
            safe_write_json(OutputNamespace.SANDBOX, _WALK_FORWARD_JSON,
                            {**env, "results": wf_results or {}}, base_dir=base)
            wrote = True
        except Exception as exc:
            logger.warning("strategy_lab: write skipped/failed (%s)", exc)
            warnings.append(f"write_skipped:{exc}")
    return {"status": status, "run_mode": mode.value, "tactic_count": len(leaderboard),
            "coverage_complete": coverage_complete, "wrote_files": wrote,
            "artifacts": artifacts, "warnings": warnings, "observe_only": True, "sandbox_only": True}


def _render_md(payload: dict[str, Any]) -> str:
    lines = ["# Research Strategy Lab — Leaderboard (Sandbox)", "",
             "_Observe-only. Ranked by master strategy score (after-cost-ish, risk-adjusted "
             "excess vs SPY). Not a trade recommendation._", ""]
    for i, r in enumerate(payload.get("leaderboard", [])[:12], 1):
        lines.append(f"{i}. **{r['name']}** — score {r['strategy_score']:+.3f} · "
                     f"mean excess vs SPY {r['mean_excess_vs_spy']:+.2%} · "
                     f"beats SPY {r['prob_beat_spy']:.0%} of windows · "
                     f"worst maxDD {r['worst_max_drawdown']:.0%}"
                     + (f" · {r['academic_basis'][:60]}" if r.get("academic_basis") else ""))
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="Research-Backed Strategy Lab (sandbox)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--run-mode", default="discovery")
    args = ap.parse_args()
    print(json.dumps(run_strategy_lab(root=args.root, run_mode=args.run_mode), indent=2, default=str))
