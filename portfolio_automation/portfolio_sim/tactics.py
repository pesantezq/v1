"""
Tactic interface + materializers.

A Tactic is a named target-weight vector (optionally time-varying via
`target_weights_asof`). Three materializers:
- shadow portfolios (reuse shadow_tracker — concrete weights from real holdings),
- strategy profiles (the 8 SEED_PROFILES → concrete weights via bounded tilts),
- benchmarks (SPY/QQQ).

All profile weights are derived from the operator's actual portfolio (the anchor)
and clamped to config concentration/leverage caps. The exact tilt map is recorded
in `metadata["materialization"]` for the strategy catalog (documentation rule).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_sim.tactics")

# Ticker → category, used by the tilt materializer. Holdings' asset_class is
# consulted first; this map covers the proxy ETFs + common cases.
_CATEGORY_MAP = {
    "BND": "bond", "TLT": "bond", "AGG": "bond", "BNDX": "bond",
    "SCHD": "dividend", "VYM": "dividend", "DVY": "dividend",
    "USMV": "low_vol", "SPLV": "low_vol",
    "GLD": "gold", "IAU": "gold", "GLDM": "gold",
}

_DEFAULT_CONC_CAP = 0.60
_DEFAULT_LEV_CAP = 0.25


@dataclass
class Tactic:
    tactic_id: str
    name: str
    source: str                      # shadow | strategy_profile | benchmark | baseline
    target_weights: dict[str, float]
    metadata: dict[str, Any] = field(default_factory=dict)
    approximate: bool = False

    def target_weights_asof(self, date: str, ctx: dict | None = None) -> dict[str, float]:
        """Static tactics return their constant vector regardless of date."""
        return dict(self.target_weights)


class TimeVaryingTactic(Tactic):
    """
    Base for tactics whose target weights depend on the as-of date (e.g. the
    crowd-signal tactic). Subclasses override ``target_weights_asof``. The engine
    calls it at t0 and on every rebalance day, so the vector can evolve over time.
    """

    def target_weights_asof(self, date: str, ctx: dict | None = None) -> dict[str, float]:  # pragma: no cover - overridden
        return dict(self.target_weights)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(v for v in weights.values() if v > 0)
    if total <= 0:
        return {}
    return {k: v / total for k, v in weights.items() if v > 0}


def _category(ticker: str, asset_class_map: dict[str, str]) -> str:
    if ticker in _CATEGORY_MAP:
        return _CATEGORY_MAP[ticker]
    ac = asset_class_map.get(ticker, "")
    if "leveraged" in ac:
        return "leveraged"
    if "commodity" in ac:
        return "gold"
    return "equity"


def _clamp_caps(weights: dict[str, float], leveraged: set[str],
                conc_cap: float, lev_cap: float) -> dict[str, float]:
    w = _normalize(weights)
    if not w:
        return w
    # Leverage cap: hold leveraged sum at lev_cap; move the freed weight to the
    # non-leveraged complement proportionally (keeps the total at 1.0; does NOT
    # uniformly renormalize, which would re-inflate the leveraged names).
    lev_sum = sum(w.get(t, 0.0) for t in leveraged)
    if lev_sum > lev_cap > 0:
        scale = lev_cap / lev_sum
        freed = lev_sum - lev_cap
        for t in leveraged:
            if t in w:
                w[t] *= scale
        non_lev = {k: v for k, v in w.items() if k not in leveraged and v > 0}
        nl_total = sum(non_lev.values())
        if nl_total > 0:
            for k in non_lev:
                w[k] += freed * (w[k] / nl_total)
    # Concentration cap: clip over-cap names, redistribute the excess to
    # under-cap names proportionally (preserves the total). Iterate to settle.
    for _ in range(8):
        over = {k: v for k, v in w.items() if v > conc_cap + 1e-12}
        if not over:
            break
        excess = sum(v - conc_cap for v in over.values())
        for k in over:
            w[k] = conc_cap
        # Exclude leveraged names from receiving the redistribution so the
        # leverage cap (applied above) is not silently breached.
        room = {k: v for k, v in w.items()
                if v < conc_cap - 1e-12 and k not in leveraged}
        room_total = sum(room.values())
        if room_total <= 0:
            break
        for k in room:
            w[k] += excess * (w[k] / room_total)
    return w


# ---------------------------------------------------------------------------
# materializers
# ---------------------------------------------------------------------------

def tactics_from_shadow_portfolios(root: str | Path, now_iso: str = "1970-01-01T00:00:00Z") -> list[Tactic]:
    """Reuse shadow_tracker's 6 weight-vectors built from the real portfolio."""
    from portfolio_automation.sandbox.shadow_tracker import build_shadow_portfolios
    try:
        payload = build_shadow_portfolios(Path(root), now_iso)
    except Exception as exc:
        logger.debug("portfolio_sim tactics: shadow build failed (%s)", exc)
        return []
    out: list[Tactic] = []
    for name, body in (payload.get("portfolios") or {}).items():
        weights = _normalize(body.get("weights") or {})
        if weights:
            out.append(Tactic(
                tactic_id=f"shadow_{name}", name=name.replace("_", " ").title(),
                source="baseline" if "baseline" in name else "shadow",
                target_weights=weights,
                metadata={"materialization": "shadow_tracker", "shadow_metrics": body.get("metrics", {})},
            ))
    return out


def _apply_tilts(strategy_id: str, base: dict[str, float], universe_cats: dict[str, str],
                 leveraged: set[str]) -> tuple[dict[str, float], dict[str, Any]]:
    """Apply a profile's bounded tilts to the anchor weights. Returns (weights, tilt_map)."""
    w = dict(base)
    for t in universe_cats:
        w.setdefault(t, 0.0)
    cats = universe_cats
    tilt: dict[str, Any] = {"strategy_id": strategy_id, "rules": []}

    def mul(category: str, factor: float):
        for t, c in cats.items():
            if c == category:
                w[t] = w.get(t, 0.0) * factor
        tilt["rules"].append(f"{category} ×{factor}")

    def floor(category: str, value: float):
        for t, c in cats.items():
            if c == category:
                w[t] = max(w.get(t, 0.0), value)
        tilt["rules"].append(f"{category} floor {value}")

    if strategy_id == "aggressive_growth":
        mul("equity", 1.5); mul("leveraged", 1.4); mul("gold", 0.4); mul("bond", 0.2)
    elif strategy_id == "short_term_tactical":
        mul("equity", 1.3); mul("leveraged", 1.2); mul("gold", 0.5)  # approximate
    elif strategy_id in ("long_term_compounding", "tax_aware"):
        mul("equity", 1.2); mul("leveraged", 0.5); mul("gold", 0.8)
    elif strategy_id == "defensive_capital_preservation":
        mul("leveraged", 0.0); mul("equity", 0.6); mul("gold", 1.5)
        floor("bond", 0.20); floor("low_vol", 0.15)
    elif strategy_id == "income_dividend":
        mul("leveraged", 0.0); mul("equity", 0.7)
        floor("dividend", 0.30); floor("bond", 0.20)
    elif strategy_id == "balanced_core_satellite":
        mul("equity", 1.1); mul("leveraged", 0.8); mul("gold", 1.0)
    elif strategy_id == "boom_bucket":
        mul("leveraged", 1.5); mul("equity", 1.1); mul("gold", 0.6)
    return w, tilt


def tactics_from_strategy_profiles(root: str | Path) -> list[Tactic]:
    """Materialize the 8 SEED_PROFILES into concrete capped weight vectors."""
    from portfolio_automation.strategy.profiles import SEED_PROFILES
    root = Path(root)
    cfg = _load_json(root / "config.json") or {}
    holdings = (cfg.get("portfolio", {}) or {}).get("holdings", []) or []
    gm = cfg.get("growth_mode", {}) or {}
    conc_cap = float(gm.get("concentration_cap", _DEFAULT_CONC_CAP))
    lev_cap = float(gm.get("leverage_cap", _DEFAULT_LEV_CAP))

    asset_class_map = {str(h.get("symbol", "")).upper(): str(h.get("asset_class", "") or "")
                       for h in holdings if h.get("symbol")}
    leveraged = {str(h.get("symbol", "")).upper() for h in holdings if h.get("is_leveraged")}

    # Anchor = actual portfolio weights (shares as proxy).
    base = _normalize({str(h.get("symbol", "")).upper(): float(h.get("shares", 0) or 0)
                       for h in holdings if h.get("symbol")})

    from portfolio_automation.portfolio_sim.universe import resolve_simulable_universe
    universe = resolve_simulable_universe(root)
    universe_cats = {t: _category(t, asset_class_map) for t in universe}
    for t in leveraged:
        universe_cats[t] = "leveraged"

    out: list[Tactic] = []
    for sid, profile in SEED_PROFILES.items():
        raw, tilt = _apply_tilts(sid, base, universe_cats, leveraged)
        weights = _clamp_caps(raw, leveraged, conc_cap, lev_cap)
        if not weights:
            continue
        out.append(Tactic(
            tactic_id=f"profile_{sid}", name=profile.name, source="strategy_profile",
            target_weights=weights,
            metadata={"materialization": tilt, "objective": profile.objective,
                      "horizon": profile.horizon, "drawdown_tolerance": profile.drawdown_tolerance,
                      "caps": {"concentration": conc_cap, "leverage": lev_cap}},
            approximate=(sid == "short_term_tactical"),
        ))
    return out


def benchmark_tactics() -> list[Tactic]:
    return [
        Tactic("benchmark_spy", "S&P 500 (SPY)", "benchmark", {"SPY": 1.0},
               metadata={"materialization": "benchmark"}),
        Tactic("benchmark_qqq", "Nasdaq-100 (QQQ)", "benchmark", {"QQQ": 1.0},
               metadata={"materialization": "benchmark"}),
    ]


def all_static_tactics(root: str | Path, now_iso: str = "1970-01-01T00:00:00Z") -> list[Tactic]:
    return (tactics_from_shadow_portfolios(root, now_iso)
            + tactics_from_strategy_profiles(root)
            + benchmark_tactics())
