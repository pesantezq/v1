"""Simulation Graphs dashboard loader (Strategy Lab section).

Read-only / observe-only. Reads the normalized ``outputs/latest/simulation_charts.json``
artifact (produced by ``portfolio_automation.simulation_charts``) and shapes it for
the Strategy Lab template: it pre-computes inline-SVG geometry so the template stays
dumb, and it degrades honestly when data is missing, malformed, or stale.

Fallback: when the persisted artifact is absent it builds a LIMITED view live from
``outputs/sandbox/strategy_comparison.json`` (at minimum the Risk vs Return + drawdown
views) rather than showing nothing — marked ``status="limited"``.

This module renders research/simulation context only. It never trades, never emits
buy/sell/hold language, and never reads or writes ``decision_plan.json``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Charts older than this are flagged (not hidden) as possibly stale.
_STALE_AFTER_S = 14 * 24 * 3600  # backtest/projection refresh weekly

# inline-SVG plot box (viewBox 0..100 x 0..50), with padding for labels
_W, _H = 100.0, 50.0
_PX0, _PX1 = 4.0, 96.0   # horizontal plot area
_PY0, _PY1 = 5.0, 44.0   # vertical plot area (y grows downward in SVG)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _age_seconds(generated_at: str | None) -> float | None:
    if not generated_at:
        return None
    try:
        ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def _scale(v: float, lo: float, hi: float, out0: float, out1: float) -> float:
    if hi <= lo:
        return (out0 + out1) / 2.0
    return out0 + (float(v) - lo) / (hi - lo) * (out1 - out0)


def _line_geometry(series: list[dict], *, zero_line: bool = False) -> dict[str, Any]:
    """Map multi-series {label, points:[{x,y}]} into SVG polyline strings."""
    all_y = [p["y"] for s in series for p in s.get("points", []) if isinstance(p.get("y"), (int, float))]
    if not all_y:
        return {}
    y_lo, y_hi = min(all_y), max(all_y)
    if zero_line:
        y_lo, y_hi = min(y_lo, 0.0), max(y_hi, 0.0)
    if y_hi == y_lo:
        y_hi = y_lo + 1.0
    polylines = []
    for s in series:
        pts = s.get("points", [])
        n = len(pts)
        coords = []
        for i, p in enumerate(pts):
            px = _scale(i, 0, max(n - 1, 1), _PX0, _PX1) if n > 1 else (_PX0 + _PX1) / 2
            py = _scale(p["y"], y_lo, y_hi, _PY1, _PY0)  # invert: high value → small y
            coords.append(f"{px:.2f},{py:.2f}")
        polylines.append({"label": s.get("label"), "role": s.get("role", "primary"), "points_str": " ".join(coords)})
    out = {
        "polylines": polylines,
        "x_labels": [str(p.get("x")) for p in series[0].get("points", [])] if series else [],
        "y_min_label": f"{y_lo:,.0f}", "y_max_label": f"{y_hi:,.0f}",
    }
    if zero_line:
        out["zero_y"] = round(_scale(0.0, y_lo, y_hi, _PY1, _PY0), 2)
    return out


def _scatter_geometry(points: list[dict]) -> dict[str, Any]:
    xs = [p["risk_pct"] for p in points if isinstance(p.get("risk_pct"), (int, float))]
    ys = [p["return_pct"] for p in points if isinstance(p.get("return_pct"), (int, float))]
    if not xs or not ys:
        return {}
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = min(ys), max(ys)
    dots = []
    for p in points:
        dots.append({
            "cx": round(_scale(p["risk_pct"], x_lo, x_hi, _PX0 + 3, _PX1 - 3), 2),
            "cy": round(_scale(p["return_pct"], y_lo, y_hi, _PY1, _PY0), 2),
            "label": p.get("label"), "return_pct": p.get("return_pct"),
            "risk_pct": p.get("risk_pct"), "drawdown_pct": p.get("drawdown_pct"),
        })
    return {
        "dots": dots,
        "x_min_label": f"{x_lo:.0f}%", "x_max_label": f"{x_hi:.0f}%",
        "y_min_label": f"{y_lo:.0f}%", "y_max_label": f"{y_hi:.0f}%",
    }


def _bars_geometry(bars: list[dict], value_key: str) -> list[dict]:
    vals = [abs(b.get(value_key) or 0) for b in bars]
    mx = max(vals) if vals else 0
    out = []
    for b in bars:
        v = b.get(value_key) or 0
        out.append({**b, "pct_width": round((abs(v) / mx * 100.0), 1) if mx else 0})
    return out


def _shape_chart(key: str, chart: dict) -> dict[str, Any]:
    """Attach a `kind` + pre-computed `geometry` for the template."""
    base = {
        "title": chart.get("title"), "help_text": chart.get("help_text"),
        "takeaway": chart.get("takeaway") or "",
        "available": bool(chart.get("available")),
        "missing_reason": chart.get("missing_reason"),
    }
    if not base["available"]:
        base["kind"] = "empty"
        return base
    if key in ("growth_over_time", "rolling_outperformance"):
        base["kind"] = "line"
        base["horizon_label"] = chart.get("horizon_label")
        base["geometry"] = _line_geometry(chart.get("series", []), zero_line=bool(chart.get("zero_line")))
        base["legend"] = [{"label": s.get("label"), "role": s.get("role", "primary")} for s in chart.get("series", [])]
    elif key == "risk_return":
        base["kind"] = "scatter"
        base["geometry"] = _scatter_geometry(chart.get("points", []))
    elif key == "drawdown":
        base["kind"] = "bars"
        base["bars"] = _bars_geometry(chart.get("bars", []), "value_pct")
        base["value_suffix"] = "%"
    elif key == "contribution_sensitivity":
        base["kind"] = "bars_money"
        base["window_label"] = chart.get("window_label")
        base["bars"] = _bars_geometry(chart.get("bars", []), "final_balance")
    else:
        base["kind"] = "empty"
    # geometry could come back empty if numbers were unusable → honest empty
    if base.get("kind") in ("line", "scatter") and not base.get("geometry"):
        base["available"] = False
        base["kind"] = "empty"
        base["missing_reason"] = base.get("missing_reason") or "Not enough simulation data to draw this chart yet."
    return base


_SUMMARY_META = [
    ("best_growth", "Best Growth", "return_pct", "%", "green"),
    ("best_risk_control", "Best Risk Control", "max_drawdown_pct", "%", "sky"),
    ("best_balance", "Best Balance", "score", "", "blue"),
    ("biggest_pain_point", "Biggest Pain Point", "max_drawdown_pct", "%", "amber"),
]


def _shape_summary(summary: dict) -> list[dict]:
    out = []
    for key, title, vkey, suffix, severity in _SUMMARY_META:
        c = summary.get(key) or {}
        val = c.get(vkey)
        out.append({
            "key": key, "title": title, "severity": severity,
            "strategy": c.get("strategy"),
            "value_label": (f"{val}{suffix}" if val is not None else "—"),
            "plain_english": c.get("plain_english") or "",
        })
    return out


def _empty_view(message: str) -> dict[str, Any]:
    return {
        "available": False, "status": "absent", "empty_message": message,
        "observe_only": True, "summary": [], "charts": {},
        "safety": {"mode": "sandbox", "official_advisory_source": "decision_plan.json", "can_execute_trades": False},
    }


def collect_simulation_charts_view(root: Path) -> dict[str, Any]:
    """Build the Simulation Graphs view for the Strategy Lab template.

    Order of preference:
      1. outputs/latest/simulation_charts.json (normalized artifact) → status "ok"
      2. live fallback build from outputs/sandbox/strategy_comparison.json → status "limited"
      3. honest empty state
    Never raises; always returns a dict the template can render.
    """
    try:
        root = Path(root)
        payload = _load(root / "outputs" / "latest" / "simulation_charts.json")
        status = "ok"

        if not payload or "charts" not in payload:
            # ── fallback: build live from whatever sandbox artifacts exist ──
            try:
                from portfolio_automation.simulation_charts import build_simulation_charts
                sb = root / "outputs" / "sandbox"
                comparison = _load(sb / "strategy_comparison.json")
                backtest = _load(sb / "portfolio_backtest.json")
                projection = _load(sb / "portfolio_projection.json")
                if not (comparison.get("comparison") or backtest.get("status") == "ok" or projection.get("status") == "ok"):
                    return _empty_view(
                        "Simulation charts are not available yet. Run the simulation/backtest "
                        "pipeline to generate outputs/latest/simulation_charts.json."
                    )
                payload = build_simulation_charts(comparison=comparison, backtest=backtest, projection=projection)
                status = "limited"
            except Exception:
                return _empty_view(
                    "Simulation charts are not available yet. Run the simulation/backtest "
                    "pipeline to generate outputs/latest/simulation_charts.json."
                )

        if payload.get("status") == "error":
            return _empty_view("Simulation charts could not be generated on the last run. They will refresh on the next pipeline run.")

        charts_in = payload.get("charts") or {}
        order = ["growth_over_time", "drawdown", "risk_return",
                 "rolling_outperformance", "contribution_sensitivity", "allocation_drift"]
        charts = {k: _shape_chart(k, charts_in.get(k) or {"title": k, "available": False,
                  "missing_reason": "Not enough simulation data to draw this chart yet."}) for k in order if k in charts_in or True}

        age = _age_seconds(payload.get("generated_at"))
        stale = age is not None and age > _STALE_AFTER_S
        return {
            "available": True,
            "status": status,
            "observe_only": True,
            "generated_at": payload.get("generated_at"),
            "stale": stale,
            "stale_message": (f"Simulation data may be stale. Last generated: {payload.get('generated_at')}." if stale else None),
            "limited_message": ("Showing a limited view derived live from strategy_comparison.json — "
                                "the full simulation_charts.json artifact has not been generated yet." if status == "limited" else None),
            "safety": payload.get("safety") or {"mode": "sandbox", "official_advisory_source": "decision_plan.json", "can_execute_trades": False},
            "source_files": payload.get("source_files", []),
            "source_files_present": payload.get("source_files_present", []),
            "summary": _shape_summary(payload.get("summary") or {}),
            "charts": charts,
            "chart_order": order,
        }
    except Exception:
        return _empty_view("Simulation charts are not available yet.")
