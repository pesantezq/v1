"""
Research Strategy Library — academic strategy families as sandbox Tactics.

Each tactic carries an `academic_basis` (citation + one-line claim). Static ones
return fixed vectors; parameterized ones (momentum, dual-momentum, mean-variance,
risk-parity) are TimeVaryingTactics that compute weights from the price panel as
of the rebalance date (look-ahead safe — data ≤ date only). All weights are
normalized and clamped to config caps.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from portfolio_automation.portfolio_sim.tactics import (
    Tactic,
    TimeVaryingTactic,
    _clamp_caps,
    _normalize,
)

logger = logging.getLogger("stockbot.portfolio_sim.research_library")


# ---------------------------------------------------------------------------
# panel helpers (trailing stats from data ≤ date)
# ---------------------------------------------------------------------------

def _trailing_return(panel, ticker: str, asof: str, months: int) -> float | None:
    dates = [d for d in panel.dates if d <= asof]
    if len(dates) < 2:
        return None
    # ~21 trading days/month
    lookback = min(len(dates) - 1, months * 21)
    p0 = panel.close(ticker, dates[-1 - lookback])
    p1 = panel.close(ticker, dates[-1])
    if p0 and p1 and p0 > 0:
        return p1 / p0 - 1.0
    return None


def _trailing_vol(panel, ticker: str, asof: str, days: int = 63) -> float | None:
    dates = [d for d in panel.dates if d <= asof][-(days + 1):]
    closes = [panel.close(ticker, d) for d in dates]
    closes = [c for c in closes if c]
    if len(closes) < 5:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 4:
        return None
    return float(np.std(rets, ddof=1))


# ---------------------------------------------------------------------------
# parameterized tactics
# ---------------------------------------------------------------------------

class MomentumRotation(TimeVaryingTactic):
    """Jegadeesh & Titman 1993: hold the top-N ETFs by trailing K-month return."""

    def __init__(self, universe: list[str], *, lookback_months: int = 6, top_n: int = 3,
                 leveraged: set[str] | None = None, caps=(0.60, 0.25)):
        super().__init__("research_momentum_rotation", "Momentum Rotation", "strategy_profile",
                         {u: 1.0 / len(universe) for u in universe} if universe else {},
                         metadata={"academic_basis": "Jegadeesh & Titman (1993): recent winners "
                                   "outperform over 3-12m windows.",
                                   "params": {"lookback_months": lookback_months, "top_n": top_n},
                                   "materialization": {"rules": [f"top {top_n} by {lookback_months}m momentum"]}})
        self.universe = universe
        self.lookback = lookback_months
        self.top_n = top_n
        self.leveraged = leveraged or set()
        self.caps = caps

    def target_weights_asof(self, date, ctx=None):
        panel = (ctx or {}).get("panel")
        if panel is None:
            return dict(self.target_weights)
        scored = [(t, _trailing_return(panel, t, date, self.lookback)) for t in self.universe]
        scored = [(t, r) for t, r in scored if r is not None]
        scored.sort(key=lambda x: x[1], reverse=True)
        winners = [t for t, r in scored[: self.top_n] if r > 0]
        if not winners:
            return {}  # all negative momentum → to cash (empty = out of market)
        w = {t: 1.0 / len(winners) for t in winners}
        return _clamp_caps(w, self.leveraged, *self.caps)


class DualMomentum(TimeVaryingTactic):
    """Antonacci dual momentum: risk-on asset if abs+rel momentum positive, else defensive."""

    def __init__(self, risk_on: list[str], defensive: list[str], *, lookback_months: int = 12):
        super().__init__("research_dual_momentum", "Dual Momentum", "strategy_profile",
                         {risk_on[0]: 1.0} if risk_on else {},
                         metadata={"academic_basis": "Antonacci: combine absolute + relative "
                                   "momentum; rotate to defensive when risk-on momentum is negative.",
                                   "params": {"lookback_months": lookback_months},
                                   "materialization": {"rules": ["risk-on if abs+rel momentum>0 else defensive"]}})
        self.risk_on = risk_on
        self.defensive = defensive
        self.lookback = lookback_months

    def target_weights_asof(self, date, ctx=None):
        panel = (ctx or {}).get("panel")
        if panel is None:
            return dict(self.target_weights)
        ro = [(t, _trailing_return(panel, t, date, self.lookback)) for t in self.risk_on]
        ro = [(t, r) for t, r in ro if r is not None]
        if ro:
            best_t, best_r = max(ro, key=lambda x: x[1])
            if best_r > 0:
                return {best_t: 1.0}
        # defensive leg: best defensive by momentum (or equal-weight)
        df = [(t, _trailing_return(panel, t, date, self.lookback)) for t in self.defensive]
        df = [(t, r) for t, r in df if r is not None]
        if df:
            return {max(df, key=lambda x: x[1])[0]: 1.0}
        return _normalize({t: 1.0 for t in self.defensive})


class RiskParityLite(TimeVaryingTactic):
    """Inverse-volatility weights across the basket (risk-budgeting approximation)."""

    def __init__(self, universe: list[str], *, vol_days: int = 63, leveraged=None, caps=(0.60, 0.25)):
        super().__init__("research_risk_parity_lite", "Risk Parity Lite", "strategy_profile",
                         {u: 1.0 / len(universe) for u in universe} if universe else {},
                         metadata={"academic_basis": "Risk budgeting: inverse-vol weighting "
                                   "equalizes risk contribution across assets.",
                                   "params": {"vol_days": vol_days},
                                   "materialization": {"rules": ["weight ∝ 1/trailing_vol"]}})
        self.universe = universe
        self.vol_days = vol_days
        self.leveraged = leveraged or set()
        self.caps = caps

    def target_weights_asof(self, date, ctx=None):
        panel = (ctx or {}).get("panel")
        if panel is None:
            return dict(self.target_weights)
        invvol = {}
        for t in self.universe:
            v = _trailing_vol(panel, t, date, self.vol_days)
            if v and v > 0:
                invvol[t] = 1.0 / v
        if not invvol:
            return _normalize({t: 1.0 for t in self.universe})
        return _clamp_caps(invvol, self.leveraged, *self.caps)


class VolManaged(TimeVaryingTactic):
    """Moreira & Muir 2017: cut the leverage sleeve when realized vol is high."""

    def __init__(self, base_weights: dict[str, float], leveraged: set[str], *,
                 vol_threshold: float = 0.018, vol_days: int = 21,
                 defensive: tuple[str, ...] = ("BND", "GLD"), caps=(0.60, 0.25)):
        super().__init__("research_vol_managed", "Volatility-Managed", "strategy_profile",
                         dict(base_weights),
                         metadata={"academic_basis": "Moreira & Muir (2017): taking less risk "
                                   "when volatility is high raised Sharpe / alpha.",
                                   "params": {"vol_threshold": vol_threshold, "vol_days": vol_days},
                                   "materialization": {"rules": ["scale leverage sleeve down when "
                                                                  "realized vol > threshold"]}})
        self.base = dict(base_weights)
        self.leveraged = leveraged
        self.vol_threshold = vol_threshold
        self.vol_days = vol_days
        self.defensive = [d for d in defensive]
        self.caps = caps

    def target_weights_asof(self, date, ctx=None):
        panel = (ctx or {}).get("panel")
        if panel is None or not self.leveraged:
            return _clamp_caps(self.base, self.leveraged, *self.caps)
        # use the most-volatile leveraged name's trailing vol as the risk gauge
        vols = [_trailing_vol(panel, t, date, self.vol_days) for t in self.leveraged]
        vols = [v for v in vols if v is not None]
        gauge = max(vols) if vols else 0.0
        w = dict(self.base)
        if gauge > self.vol_threshold:
            freed = 0.0
            for t in self.leveraged:
                if t in w:
                    cut = w[t] * 0.5
                    w[t] -= cut
                    freed += cut
            recips = [d for d in self.defensive if d in (panel.tickers if panel else [])] or self.defensive
            if freed > 0 and recips:
                for d in recips:
                    w[d] = w.get(d, 0.0) + freed / len(recips)
        return _clamp_caps(w, self.leveraged, *self.caps)


class BlackLittermanBlend(TimeVaryingTactic):
    """
    Idzorek/Black-Litterman: blend a market/target prior with a confidence-scaled
    view tilt. Here a static confidence blend toward a view vector — bounded so it
    never produces an extreme/concentrated allocation.
    """

    def __init__(self, prior: dict[str, float], view: dict[str, float], *, confidence: float = 0.2,
                 leveraged=None, caps=(0.60, 0.25)):
        conf = max(0.0, min(1.0, confidence))
        blended = {t: (1 - conf) * prior.get(t, 0.0) + conf * view.get(t, 0.0)
                   for t in set(prior) | set(view)}
        super().__init__("research_black_litterman", "Black-Litterman Blend", "strategy_profile",
                         _normalize(blended),
                         metadata={"academic_basis": "Black-Litterman / Idzorek: combine market "
                                   "prior with confidence-weighted views; confidence caps tilt size.",
                                   "params": {"confidence": conf},
                                   "materialization": {"rules": [f"{int(conf*100)}% tilt toward view, "
                                                                 "rest = prior"]}})
        self.target_weights = _clamp_caps(_normalize(blended), leveraged or set(), *caps)


class MeanVarianceFrontier(TimeVaryingTactic):
    """Markowitz 1952: max-Sharpe weights from trailing mean/cov, clamped to caps."""

    def __init__(self, universe: list[str], *, lookback_days: int = 252, leveraged=None, caps=(0.60, 0.25)):
        super().__init__("research_mean_variance", "Mean-Variance (max-Sharpe)", "strategy_profile",
                         {u: 1.0 / len(universe) for u in universe} if universe else {},
                         metadata={"academic_basis": "Markowitz (1952): mean-variance efficient "
                                   "portfolio; here the long-only max-Sharpe point, capped.",
                                   "params": {"lookback_days": lookback_days},
                                   "materialization": {"rules": ["trailing max-Sharpe, long-only, capped"]}})
        self.universe = universe
        self.lookback = lookback_days
        self.leveraged = leveraged or set()
        self.caps = caps

    def target_weights_asof(self, date, ctx=None):
        panel = (ctx or {}).get("panel")
        if panel is None:
            return dict(self.target_weights)
        dates = [d for d in panel.dates if d <= date][-(self.lookback + 1):]
        rets = []
        for t in self.universe:
            series = [panel.close(t, d) for d in dates]
            series = [c for c in series if c]
            if len(series) < 30:
                rets.append(None)
            else:
                rets.append([series[i] / series[i - 1] - 1.0 for i in range(1, len(series))])
        valid = [(t, r) for t, r in zip(self.universe, rets) if r is not None]
        if len(valid) < 2:
            return _normalize({t: 1.0 for t in self.universe})
        n = min(len(r) for _, r in valid)
        mat = np.array([r[-n:] for _, r in valid])           # assets × n
        mu = mat.mean(axis=1)
        cov = np.cov(mat) + np.eye(len(valid)) * 1e-6
        try:
            w = np.linalg.solve(cov, mu)                      # ∝ inv(cov)·mu (max-Sharpe direction)
        except np.linalg.LinAlgError:
            w = np.ones(len(valid))
        w = np.clip(w, 0, None)                               # long-only
        if w.sum() <= 0:
            w = np.ones(len(valid))
        weights = {valid[i][0]: float(w[i]) for i in range(len(valid))}
        return _clamp_caps(weights, self.leveraged, *self.caps)


# ---------------------------------------------------------------------------
# library entry point
# ---------------------------------------------------------------------------

def research_tactics(root: str | Path = ".") -> list[Tactic]:  # noqa: F821
    from pathlib import Path as _P
    from portfolio_automation.portfolio_sim.universe import resolve_simulable_universe
    import json

    root = _P(root)
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8", errors="replace"))
    except Exception:
        cfg = {}
    holdings = (cfg.get("portfolio", {}) or {}).get("holdings", []) or []
    leveraged = {str(h.get("symbol", "")).upper() for h in holdings if h.get("is_leveraged")}
    gm = cfg.get("growth_mode", {}) or {}
    caps = (float(gm.get("concentration_cap", 0.60)), float(gm.get("leverage_cap", 0.25)))
    universe = sorted(resolve_simulable_universe(root).keys())
    equities = [t for t in universe if t not in {"BND", "TLT", "AGG", "GLD", "IAU"}]

    out: list[Tactic] = [
        Tactic("research_sixty_forty", "60/40 SPY/BND", "strategy_profile",
               {"SPY": 0.60, "BND": 0.40},
               metadata={"academic_basis": "Classic balanced allocation benchmark.",
                         "materialization": {"rules": ["60% SPY / 40% BND"]}}),
        Tactic("research_factor_tilt", "Factor Tilt (quality/value/div)", "strategy_profile",
               _normalize({"SCHD": 0.4, "USMV": 0.3, "SPY": 0.3}),
               metadata={"academic_basis": "Fama-French (1993): tilt toward value/quality/"
                         "profitability factor proxies.",
                         "materialization": {"rules": ["SCHD/USMV/SPY factor-proxy tilt"]}}),
        MomentumRotation(equities or universe, leveraged=leveraged, caps=caps),
        DualMomentum(risk_on=[t for t in ("QQQ", "SPY") if t in universe] or universe[:1],
                     defensive=[t for t in ("BND", "GLD", "TLT") if t in universe] or ["BND"]),
        RiskParityLite(universe, leveraged=leveraged, caps=caps),
        MeanVarianceFrontier(universe, leveraged=leveraged, caps=caps),
    ]
    # Anchor = actual portfolio weights (shares proxy) for prior/base.
    base = _normalize({str(h.get("symbol", "")).upper(): float(h.get("shares", 0) or 0)
                       for h in holdings if h.get("symbol")})
    if base and leveraged:
        out.append(VolManaged(base, leveraged, caps=caps,
                              defensive=tuple(t for t in ("BND", "GLD") if t in universe) or ("BND",)))
    if base:
        # view: tilt toward the strongest broad-equity holding (a bounded research view)
        view_ticker = next((t for t in ("QQQ", "SPY") if t in universe), None)
        if view_ticker:
            out.append(BlackLittermanBlend(base, {view_ticker: 1.0}, confidence=0.2,
                                           leveraged=leveraged, caps=caps))
    return out
