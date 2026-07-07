"""
Risk Delta Advisor — observe-only exposure-vs-cap panel.

Surfaces three risk metrics every run so the operator can see at a glance
where the portfolio sits inside the structural caps without waiting for a
bad market day to discover the answer:

  1. Single-position concentration  vs `concentration_cap` (config.json)
  2. Total leveraged exposure       vs `leverage_cap`      (config.json)
  3. 1-day 95% Value-at-Risk        as a benchmark-proxy estimate

The two cap comparisons are exact accounting reads of current holdings.
The VaR is intentionally a coarse benchmark-volatility proxy (uses
vol_regime_advisor's SPY-derived annual sigma) — a "crude number beats no
number" first pass. Per-position VaR is a v2 enhancement; the budget cost
of fetching historical prices for every holding is not justified for a
risk-panel layer in this initial version.

Inputs (read-only):
  - config.json portfolio.holdings (symbols, shares, target_weight, leverage)
  - config.json growth_mode.concentration_cap / leverage_cap
  - outputs/latest/decision_plan.json portfolio_context (portfolio value, cash)
  - outputs/latest/vol_regime_advisor.json sigma_annual

Outputs (LATEST namespace):
  - outputs/latest/risk_delta.json
  - outputs/latest/risk_delta.md

Hard guarantees:
  - observe_only=True hardcoded in every artifact.
  - No mutation of any score, decision, allocation, or recommendation state.
  - Degrades to status="insufficient_data" when essential inputs are missing.

Public API:
  compute_concentration(holdings, portfolio_value, cap)
  compute_leverage(holdings, cap)
  compute_var(portfolio_value, sigma_annual, confidence_z, horizon_days)
  build_risk_delta(...)
  write_risk_delta_artifacts(...)
  run_risk_delta_advisor(root, write_files)
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.risk_delta_advisor")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "risk_delta_advisor"
_OBSERVE_ONLY = True

# Z-score for the 1-day 95% Value-at-Risk: P(loss > 1.645σ) = 5% under
# standard normal returns. Documented here because the value drives the
# dollar VaR estimate.
_VAR_95_Z = 1.645
_TRADING_DAYS_PER_YEAR = 252

_DISCLAIMER = (
    "Observe-only risk panel. Surfaces current exposure vs structural caps and a "
    "1-day 95% VaR proxy using benchmark volatility. Not a recommendation; does "
    "not modify portfolio, allocation, scoring, or decision state."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _load_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("risk_delta: failed to load %s — %s", path, exc)
        return {}


def _classify_headroom(headroom_pct: float) -> str:
    """
    Status flag for a cap distance line.
      "breach"    : headroom <= 0  (exposure exceeds cap)
      "near_cap"  : headroom in (0, 0.05]  (within 5 percentage points)
      "ok"        : headroom > 0.05
    """
    if headroom_pct <= 0:
        return "breach"
    if headroom_pct <= 0.05:
        return "near_cap"
    return "ok"


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------


def compute_concentration(
    holdings: list[dict[str, Any]],
    portfolio_value: float,
    cap: float,
    *,
    quotes: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Per-position concentration vs the concentration cap.

    Each holding's weight is shares × price / portfolio_value. Price comes
    from `quotes` (symbol → price). When a quote is missing, the holding's
    `target_weight` is used as a fallback (lets the panel render in degraded
    mode rather than emit nothing on a price-cache miss).

    Returns a dict with top-3 ranked positions, top-3 total, and the per-
    position list sorted descending by weight.
    """
    if portfolio_value <= 0 or not isinstance(holdings, list):
        return {"available": False, "reason": "no_portfolio_value_or_holdings"}

    quotes = quotes or {}
    rows: list[dict[str, Any]] = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        sym = str(h.get("symbol") or "").upper().strip()
        if not sym:
            continue
        shares = _safe_float(h.get("shares")) or 0.0
        price = quotes.get(sym)
        weight: float | None = None
        if price is not None and shares > 0:
            weight = (shares * float(price)) / portfolio_value
        else:
            tw = _safe_float(h.get("target_weight"))
            if tw is not None:
                weight = tw  # Degraded-mode fallback
        if weight is None:
            continue
        headroom = cap - weight
        rows.append({
            "symbol": sym,
            "weight": round(weight, 4),
            "cap": round(cap, 4),
            "headroom": round(headroom, 4),
            "status": _classify_headroom(headroom),
            "shares": shares,
            "price_source": "live_quote" if price is not None else "target_weight_fallback",
        })

    if not rows:
        return {"available": False, "reason": "no_priced_holdings"}

    rows.sort(key=lambda r: r["weight"], reverse=True)
    top_3_total = round(sum(r["weight"] for r in rows[:3]), 4)

    return {
        "available": True,
        "cap": round(cap, 4),
        "top_position": rows[0],
        "top_3_total": top_3_total,
        "positions": rows,
        "breach_count": sum(1 for r in rows if r["status"] == "breach"),
        "near_cap_count": sum(1 for r in rows if r["status"] == "near_cap"),
    }


# ---------------------------------------------------------------------------
# Leverage
# ---------------------------------------------------------------------------


def compute_leverage(
    holdings: list[dict[str, Any]],
    portfolio_value: float,
    cap: float,
    *,
    quotes: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Total leveraged exposure vs the leverage cap.

    Each leveraged holding (`is_leveraged=True`) contributes weight ×
    `leverage_factor` to the total. Example: QLD at 5% weight with
    leverage_factor=2 contributes 10% leveraged exposure.
    """
    if portfolio_value <= 0 or not isinstance(holdings, list):
        return {"available": False, "reason": "no_portfolio_value_or_holdings"}

    quotes = quotes or {}
    leveraged_rows: list[dict[str, Any]] = []
    total_exposure = 0.0
    for h in holdings:
        if not isinstance(h, dict) or not h.get("is_leveraged"):
            continue
        sym = str(h.get("symbol") or "").upper().strip()
        if not sym:
            continue
        shares = _safe_float(h.get("shares")) or 0.0
        factor = _safe_float(h.get("leverage_factor")) or 1.0
        price = quotes.get(sym)
        if price is not None and shares > 0:
            weight = (shares * float(price)) / portfolio_value
        else:
            weight = _safe_float(h.get("target_weight")) or 0.0
        exposure = weight * factor
        total_exposure += exposure
        leveraged_rows.append({
            "symbol": sym,
            "weight": round(weight, 4),
            "leverage_factor": round(factor, 4),
            "exposure": round(exposure, 4),
        })

    headroom = cap - total_exposure
    return {
        "available": True,
        "cap": round(cap, 4),
        "total_exposure": round(total_exposure, 4),
        "headroom": round(headroom, 4),
        "status": _classify_headroom(headroom),
        "leveraged_positions": leveraged_rows,
    }


# ---------------------------------------------------------------------------
# Value-at-Risk
# ---------------------------------------------------------------------------


def compute_var(
    portfolio_value: float,
    sigma_annual: float | None,
    *,
    confidence_z: float = _VAR_95_Z,
    horizon_days: int = 1,
) -> dict[str, Any]:
    """
    1-day Value-at-Risk using a benchmark-equivalent volatility proxy.

      daily_sigma = sigma_annual / sqrt(252)
      VaR%        = confidence_z × daily_sigma × sqrt(horizon_days)
      VaR$        = VaR% × portfolio_value

    Documented as a crude first-pass estimate: it assumes the portfolio
    tracks the benchmark's volatility, which understates concentrated
    portfolios and overstates highly diversified ones. Per-position VaR
    is a v2 enhancement (would cost FMP budget for historical prices).
    """
    if portfolio_value <= 0 or sigma_annual is None or sigma_annual <= 0:
        return {
            "available": False,
            "reason": "missing_portfolio_value_or_sigma",
        }

    horizon_days = max(1, int(horizon_days))
    daily_sigma = float(sigma_annual) / math.sqrt(_TRADING_DAYS_PER_YEAR)
    var_pct = confidence_z * daily_sigma * math.sqrt(horizon_days)
    var_dollar = var_pct * portfolio_value

    return {
        "available": True,
        "method": "benchmark_sigma_proxy",
        "horizon_days": horizon_days,
        "confidence_pct": 95.0 if confidence_z == _VAR_95_Z else None,
        "confidence_z": round(confidence_z, 3),
        "sigma_annual": round(float(sigma_annual), 4),
        "sigma_daily": round(daily_sigma, 5),
        "var_pct": round(var_pct, 4),
        "var_dollar": round(var_dollar, 2),
        "portfolio_value": round(portfolio_value, 2),
        "assumption": (
            "Portfolio tracks benchmark-equivalent volatility; per-position vol "
            "is not modeled. Understates concentrated portfolios."
        ),
    }


# ---------------------------------------------------------------------------
# Build + render
# ---------------------------------------------------------------------------


def build_risk_delta(
    *,
    holdings: list[dict[str, Any]],
    portfolio_value: float,
    concentration_cap: float,
    leverage_cap: float,
    sigma_annual: float | None,
    quotes: dict[str, float] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble the full artifact dict. Pure function; no file writes."""
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    concentration = compute_concentration(
        holdings, portfolio_value, concentration_cap, quotes=quotes
    )
    leverage = compute_leverage(
        holdings, portfolio_value, leverage_cap, quotes=quotes
    )
    var = compute_var(portfolio_value, sigma_annual)

    # Aggregate status: the worst of any sub-section.
    status_priority = {"breach": 3, "near_cap": 2, "ok": 1}
    sub_status_flags: list[str] = []
    if concentration.get("available"):
        if concentration.get("breach_count", 0) > 0:
            sub_status_flags.append("breach")
        elif concentration.get("near_cap_count", 0) > 0:
            sub_status_flags.append("near_cap")
        else:
            sub_status_flags.append("ok")
    if leverage.get("available"):
        sub_status_flags.append(leverage.get("status") or "ok")
    overall_status = "ok"
    for f in sub_status_flags:
        if status_priority.get(f, 0) > status_priority.get(overall_status, 0):
            overall_status = f

    return {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "overall_status": overall_status,
        "portfolio_value": round(portfolio_value, 2) if portfolio_value > 0 else None,
        "concentration": concentration,
        "leverage": leverage,
        "var": var,
        "disclaimer": _DISCLAIMER,
    }


def _render_status_tag(status: str) -> str:
    """Markdown badge for the cap-distance status."""
    return {
        "breach":   "🔴 BREACH",
        "near_cap": "🟡 near cap",
        "ok":       "🟢 ok",
    }.get(status, status)


def render_risk_delta_md(payload: dict[str, Any]) -> str:
    """Render the artifact as a compact Markdown report."""
    lines: list[str] = []
    a = lines.append

    a(f"# Risk Delta Panel — {payload.get('generated_at', '')[:10]}")
    a("")
    a(f"**Generated:** {payload.get('generated_at', '')}  ")
    a(f"**Status:** {_render_status_tag(payload.get('overall_status', 'ok'))}  ")
    pv = payload.get("portfolio_value")
    if pv is not None:
        a(f"**Portfolio value:** ${pv:,.2f}  ")
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")

    conc = payload.get("concentration") or {}
    if conc.get("available"):
        cap_pct = conc.get("cap", 0)
        a(f"## Concentration vs {cap_pct:.0%} cap")
        top = conc.get("top_position") or {}
        if top:
            a(
                f"- **Top position:** `{top.get('symbol')}` at {top.get('weight', 0):.1%}"
                f" — headroom {top.get('headroom', 0) * 100:+.1f}pp {_render_status_tag(top.get('status', 'ok'))}"
            )
        top3 = conc.get("top_3_total")
        if top3 is not None:
            a(f"- **Top-3 total:** {top3:.1%} of portfolio")
        positions = conc.get("positions") or []
        if positions:
            a("- **Per-position breakdown:**")
            for p in positions:
                a(
                    f"  - `{p.get('symbol')}`: {p.get('weight', 0):.1%}"
                    f" (headroom {p.get('headroom', 0) * 100:+.1f}pp) — {_render_status_tag(p.get('status', 'ok'))}"
                )
        a("")
    else:
        a("## Concentration")
        a(f"_Not available: {conc.get('reason', 'unknown')}._")
        a("")

    lev = payload.get("leverage") or {}
    if lev.get("available"):
        cap_pct = lev.get("cap", 0)
        a(f"## Leverage vs {cap_pct:.0%} cap")
        a(
            f"- **Total leveraged exposure:** {lev.get('total_exposure', 0):.1%}"
            f" — headroom {lev.get('headroom', 0) * 100:+.1f}pp {_render_status_tag(lev.get('status', 'ok'))}"
        )
        lp = lev.get("leveraged_positions") or []
        if lp:
            a("- **Leveraged positions:**")
            for p in lp:
                a(
                    f"  - `{p.get('symbol')}`: weight {p.get('weight', 0):.1%}"
                    f" × {p.get('leverage_factor', 1):.1f}x"
                    f" = {p.get('exposure', 0):.1%} exposure"
                )
        a("")
    else:
        a("## Leverage")
        a(f"_Not available: {lev.get('reason', 'unknown')}._")
        a("")

    var = payload.get("var") or {}
    if var.get("available"):
        a("## 1-day 95% Value-at-Risk")
        a(
            f"- **Estimate:** ${var.get('var_dollar', 0):,.2f}"
            f" ({var.get('var_pct', 0):.2%} of portfolio)"
        )
        a(
            f"- Method: `{var.get('method')}`"
            f" — annual σ {var.get('sigma_annual', 0):.1%},"
            f" daily σ {var.get('sigma_daily', 0):.2%}"
        )
        a(f"- _{var.get('assumption', '')}_")
        a("")
    else:
        a("## 1-day 95% Value-at-Risk")
        a(f"_Not available: {var.get('reason', 'unknown')}._")
        a("")

    a("---")
    a("_Advisory only — no trades executed._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def write_risk_delta_artifacts(
    payload: dict[str, Any],
    *,
    base_dir: str | Path = "outputs",
) -> dict[str, str]:
    """Write the JSON + MD artifacts under outputs/latest/."""
    base = Path(base_dir)
    md = render_risk_delta_md(payload)
    json_path = safe_write_json(
        OutputNamespace.LATEST,
        "risk_delta.json",
        payload,
        base_dir=base,
    )
    md_path = safe_write_text(
        OutputNamespace.LATEST,
        "risk_delta.md",
        md,
        base_dir=base,
    )
    return {
        "risk_delta_json": str(json_path),
        "risk_delta_md": str(md_path),
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


_CONFIG_REL = ("config.json",)
_DECISION_PLAN_REL = ("outputs", "latest", "decision_plan.json")
_VOL_REGIME_REL = ("outputs", "latest", "vol_regime_advisor.json")
_PORTFOLIO_SNAPSHOT_REL = ("outputs", "portfolio", "portfolio_snapshot.json")


def _load_holdings(root: Path) -> tuple[list[dict], float, str]:
    """Return (holdings list, portfolio_value, holdings_source).

    Prefers the live Schwab snapshot via ``broker_overlaid_portfolio`` (which
    merges broker shares with config per-symbol metadata — is_leveraged,
    leverage_factor, target_weight — so concentration + leverage stay correct),
    falling back to raw config holdings when the broker is stale/absent. Read-only.
    """
    cfg = _load_json_safe(root.joinpath(*_CONFIG_REL))
    portfolio = cfg.get("portfolio") or {} if isinstance(cfg, dict) else {}
    holdings = list(portfolio.get("holdings") or [])
    holdings_source = "config"
    try:
        from portfolio_automation.holdings_resolver import broker_overlaid_portfolio
        overlaid = broker_overlaid_portfolio(portfolio, root)
        if isinstance(overlaid, dict) and overlaid.get("holdings"):
            holdings = list(overlaid.get("holdings") or holdings)
            holdings_source = overlaid.get("holdings_source", "config")
    except Exception:
        pass

    plan = _load_json_safe(root.joinpath(*_DECISION_PLAN_REL))
    ctx = plan.get("portfolio_context") or {} if isinstance(plan, dict) else {}
    portfolio_value = _safe_float(ctx.get("total_portfolio_value")) or 0.0

    return holdings, portfolio_value, holdings_source


def _load_caps(root: Path) -> tuple[float, float]:
    """Return (concentration_cap, leverage_cap) from config.json growth_mode."""
    cfg = _load_json_safe(root.joinpath(*_CONFIG_REL))
    growth = cfg.get("growth_mode") or {} if isinstance(cfg, dict) else {}
    conc = _safe_float(growth.get("concentration_cap")) or 0.60
    lev = _safe_float(growth.get("leverage_cap")) or 0.25
    return conc, lev


def _load_sigma(root: Path) -> float | None:
    """Pull benchmark sigma_annual from vol_regime_advisor.json."""
    vol = _load_json_safe(root.joinpath(*_VOL_REGIME_REL))
    return _safe_float(vol.get("sigma_annual"))


def _load_quotes(root: Path) -> dict[str, float]:
    """Pull current prices from data/price_cache.json (best-effort)."""
    cache_path = root / "data" / "price_cache.json"
    quotes: dict[str, float] = {}
    if not cache_path.exists():
        return quotes
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            for sym, entry in data.items():
                if isinstance(entry, dict):
                    p = _safe_float(entry.get("price"))
                    if p is not None and p > 0:
                        quotes[str(sym).upper()] = p
    except Exception as exc:
        logger.debug("risk_delta: price_cache load failed — %s", exc)
    return quotes


def run_risk_delta_advisor(
    *,
    root: str | Path = ".",
    write_files: bool = True,
) -> dict[str, Any]:
    """Top-level: read inputs, build artifact, optionally write to LATEST."""
    root_path = Path(root).resolve()
    try:
        holdings, portfolio_value, holdings_source = _load_holdings(root_path)
        concentration_cap, leverage_cap = _load_caps(root_path)
        sigma_annual = _load_sigma(root_path)
        quotes = _load_quotes(root_path)

        payload = build_risk_delta(
            holdings=holdings,
            portfolio_value=portfolio_value,
            concentration_cap=concentration_cap,
            leverage_cap=leverage_cap,
            sigma_annual=sigma_annual,
            quotes=quotes,
        )
        if isinstance(payload, dict):
            payload["holdings_source"] = holdings_source

        artifacts: dict[str, str] = {}
        if write_files:
            artifacts = write_risk_delta_artifacts(
                payload,
                base_dir=root_path / "outputs",
            )

        return {
            "status": "ok",
            "overall_status": payload.get("overall_status"),
            "artifacts": artifacts,
            "concentration_top": (payload.get("concentration") or {}).get("top_position"),
            "leverage_exposure": (payload.get("leverage") or {}).get("total_exposure"),
            "var_pct": (payload.get("var") or {}).get("var_pct"),
        }
    except Exception as exc:
        logger.error("risk_delta_advisor failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    import sys

    result = run_risk_delta_advisor(root=Path(__file__).resolve().parents[1])
    top = result.get("concentration_top") or {}
    print(
        f"risk_delta: status={result.get('status')}"
        f" overall={result.get('overall_status')}"
        f" top={top.get('symbol')}@{top.get('weight', 0):.1%}"
        f" lev={(result.get('leverage_exposure') or 0):.1%}"
        f" var_pct={(result.get('var_pct') or 0):.2%}"
    )
    sys.exit(0)
