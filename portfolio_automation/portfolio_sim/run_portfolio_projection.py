"""
Forward projection orchestrator: project each tactic over config horizons using
the Monte-Carlo block-bootstrap engine; write a sandbox artifact with terminal
percentiles, prob-reach-target, drawdown distribution, and a fan for the anchor.

Sandbox-only, observe-only, labeled "illustration not forecast". Never raises.
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
from portfolio_automation.portfolio_sim.prices import load_price_panel
from portfolio_automation.portfolio_sim.projection_engine import project
from portfolio_automation.portfolio_sim.sim_base import SimStatus, sim_envelope, utc_now_iso
from portfolio_automation.portfolio_sim.tactics import all_static_tactics
from portfolio_automation.strategy.strategy_selection import (
    load_active_selection,
    resolve_anchor_tactic_id,
)

logger = logging.getLogger("stockbot.portfolio_sim.run_projection")

_PROJECTION_JSON = "portfolio_projection.json"
_PROJECTION_MD = "portfolio_projection_summary.md"

_ASSUMPTIONS = (
    "Historical-return block resampling (past != future); no regime-shift / "
    "structural-break modeling; no fees/taxes/inflation; contributions assumed "
    "constant. A projection is a probabilistic illustration, not a forecast."
)


def _config(root: Path) -> dict[str, Any]:
    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
    except Exception:
        raw = {}
    ps = raw.get("portfolio_sim") or {}
    proj = ps.get("projection") or {}
    return {
        "enabled": ps.get("enabled", False),
        "monthly_contribution": float(ps.get("monthly_contribution", 1000)),
        "start_value": float(ps.get("start_value", 10000.0)),
        "n_paths": int(proj.get("n_paths", 5000)),
        "seed": int(proj.get("seed", 12345)),
        "block_months": int(proj.get("block_months", 1)),
        "horizons_years": proj.get("horizons_years", [1, 5, 10, 35]),
        "target_cagr": float((raw.get("growth_mode") or {}).get("target_cagr", 0.09)),
    }


def run_portfolio_projection(
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

    if not cfg["enabled"]:
        warnings.append("portfolio_sim.enabled=false")
        return _write(root, run_id, mode, SimStatus.DISABLED.value, warnings, [], [], {}, write_files)

    tactics = all_static_tactics(root)
    tickers = sorted({t for tac in tactics for t in tac.target_weights})
    panel = load_price_panel(tickers, root)
    months, matrix = panel.monthly_returns(tickers)
    if len(matrix) < 6:
        warnings.append("insufficient_monthly_history")
        return _write(root, run_id, mode, SimStatus.INSUFFICIENT_DATA.value, warnings, [], [], {}, write_files)
    if panel.missing:
        warnings.append(f"missing_price_history:{','.join(panel.missing[:8])}")

    # Operator-approved active strategy re-anchors the projection (sandbox-only).
    # The baseline fan is always kept; the selected strategy's fan is added
    # alongside it. anchor_strategy_id is the operator's selection (or None).
    selection = load_active_selection(root)
    anchor_tid = resolve_anchor_tactic_id(
        selection.get("active_strategy_id"), [t.tactic_id for t in tactics])
    anchor_strategy_id = selection.get("active_strategy_id") if anchor_tid else None

    horizons = [int(round(y * 12)) for y in cfg["horizons_years"]]
    rows: list[dict[str, Any]] = []
    fans: dict[str, Any] = {}
    selected_fans: dict[str, Any] = {}
    for tac in tactics:
        for hy, hm in zip(cfg["horizons_years"], horizons):
            res = project(tac.target_weights, matrix, tickers,
                          horizon_months=hm, n_paths=cfg["n_paths"],
                          start_value=cfg["start_value"], monthly_contribution=cfg["monthly_contribution"],
                          seed=cfg["seed"], block=cfg["block_months"],
                          target_cagr=cfg["target_cagr"], tactic_id=tac.tactic_id)
            if res.metrics.get("status") != "ok":
                continue
            rows.append({"tactic_id": tac.tactic_id, "name": tac.name,
                         "horizon_years": hy, "horizon_label": f"{hy}y", **res.metrics})
            if tac.tactic_id == "shadow_actual_baseline":
                fans[f"{hy}y"] = res.fan
            if anchor_tid and tac.tactic_id == anchor_tid:
                selected_fans[f"{hy}y"] = res.fan

    status = SimStatus.OK.value if rows else SimStatus.INSUFFICIENT_DATA.value
    return _write(root, run_id, mode, status, warnings, rows,
                  [f"{y}y" for y in cfg["horizons_years"]], fans, write_files,
                  seed=cfg["seed"], target_cagr=cfg["target_cagr"],
                  anchor_strategy_id=anchor_strategy_id, selected_fan=selected_fans)


def _write(root, run_id, mode, status, warnings, rows, horizons, fans, write_files,
           seed=None, target_cagr=None, anchor_strategy_id=None,
           selected_fan=None) -> dict[str, Any]:
    env = sim_envelope(run_id=run_id, run_mode=mode.value, status=status, warnings=warnings)
    payload = {**env, "assumptions": _ASSUMPTIONS, "seed": seed, "target_cagr": target_cagr,
               "horizons": horizons, "rows": rows, "anchor_fan": fans,
               "anchor_strategy_id": anchor_strategy_id,
               "selected_fan": selected_fan or {}}
    artifacts: dict[str, str] = {}
    wrote = False
    if write_files:
        try:
            assert_can_write_namespace(mode, OutputNamespace.SANDBOX)
            base = root / "outputs"
            artifacts["portfolio_projection"] = str(
                safe_write_json(OutputNamespace.SANDBOX, _PROJECTION_JSON, payload, base_dir=base))
            safe_write_text(OutputNamespace.SANDBOX, _PROJECTION_MD,
                            _render_md(payload), base_dir=base)
            wrote = True
        except Exception as exc:
            logger.warning("portfolio_sim projection: write skipped/failed (%s)", exc)
            warnings.append(f"write_skipped:{exc}")
    return {"status": status, "run_mode": mode.value, "row_count": len(rows),
            "wrote_files": wrote, "artifacts": artifacts, "warnings": warnings,
            "anchor_strategy_id": anchor_strategy_id,
            "anchor_fan": fans, "selected_fan": selected_fan or {},
            "observe_only": True, "sandbox_only": True}


def _render_md(payload: dict[str, Any]) -> str:
    lines = ["# Forward Projection — Sandbox", "",
             f"_{payload['assumptions']}_", ""]
    # headline: actual baseline at each horizon
    for r in payload.get("rows", []):
        if r["tactic_id"] == "shadow_actual_baseline":
            lines.append(f"- **{r['horizon_label']}**: p50 ${r['p50_balance']:,.0f} "
                         f"(p5 ${r['p5_balance']:,.0f} / p95 ${r['p95_balance']:,.0f}) · "
                         f"P(reach {payload.get('target_cagr')}) {r['prob_reach_target']:.0%} · "
                         f"p95 maxDD {r['max_drawdown_p95']:.0%}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Portfolio Monte-Carlo projection (sandbox)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--run-mode", default="discovery")
    args = ap.parse_args()
    print(json.dumps(run_portfolio_projection(root=args.root, run_mode=args.run_mode), indent=2, default=str))
