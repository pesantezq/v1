"""
Crowd-signal tactic: a capped sleeve toward useful crowd states + an avoid
overlay on caution states. Time-varying — weights depend on the as-of date.

Two state sources:
- live (forward): crowd states from the Crowd Radar artifact (or a provided map),
- proxy (historical): volume/momentum pseudo-states from the price panel, used
  only for the clearly-labeled proxy backtest. Look-ahead-safe (data ≤ date).
"""
from __future__ import annotations

import statistics
from typing import Any

from portfolio_automation.portfolio_sim.tactics import TimeVaryingTactic

USEFUL_STATES = frozenset({"emerging_dd", "crowd_validation", "contrarian_neglect"})
CAUTION_STATES = frozenset({"hype_acceleration", "reflexive_squeeze_risk", "crowd_exhaustion"})

DEFAULT_SLEEVE_TOTAL = 0.15
DEFAULT_PER_IDEA = 0.05
OVERLAY_TRIM = 0.8   # ×0.8 trim of a core holding flagged caution


def build_crowd_sleeve(
    core_weights: dict[str, float],
    crowd_states: list[dict[str, Any]],
    *,
    sleeve_total: float = DEFAULT_SLEEVE_TOTAL,
    per_idea: float = DEFAULT_PER_IDEA,
    priority_weighted: bool = True,
) -> tuple[dict[str, float], list[str], list[str]]:
    """
    Returns (weights, underweight_flags, sleeve_names).

    - sleeve: useful-state names, priority-weighted, capped ≤ per_idea each and
      ≤ sleeve_total in aggregate; top-N until filled.
    - overlay: caution-state core holdings trimmed ×0.8 (flagged); freed weight
      redistributed across the remaining core. Caution names never enter sleeve.
    """
    core = {k: float(v) for k, v in core_weights.items() if v > 0}
    caution = {s["ticker"] for s in crowd_states if s.get("crowd_state") in CAUTION_STATES}
    underweight_flags: list[str] = []

    # Avoid-overlay on core holdings in a caution state.
    freed = 0.0
    for t in list(core):
        if t in caution:
            cut = core[t] * (1 - OVERLAY_TRIM)
            core[t] -= cut
            freed += cut
            underweight_flags.append(t)
    # Redistribute freed weight across remaining non-caution core.
    recipients = {k: v for k, v in core.items() if k not in caution and v > 0}
    rtot = sum(recipients.values())
    if freed > 0 and rtot > 0:
        for k in recipients:
            core[k] += freed * (core[k] / rtot)

    # Sleeve from useful-state names (exclude any in caution).
    cands = [s for s in crowd_states
             if s.get("crowd_state") in USEFUL_STATES and s["ticker"] not in caution]
    cands.sort(key=lambda s: s.get("crowd_research_priority_score", 0.0), reverse=True)
    sleeve: dict[str, float] = {}
    if cands:
        if priority_weighted:
            scores = [max(s.get("crowd_research_priority_score", 0.0), 0.0) for s in cands]
            stot = sum(scores) or float(len(cands))
            raw = {s["ticker"]: (sc / stot) * sleeve_total for s, sc in zip(cands, scores)}
        else:
            per = sleeve_total / len(cands)
            raw = {s["ticker"]: per for s in cands}
        # cap per idea, accumulate up to sleeve_total
        used = 0.0
        for t, w in sorted(raw.items(), key=lambda kv: kv[1], reverse=True):
            w = min(w, per_idea)
            if used + w > sleeve_total:
                w = sleeve_total - used
            if w <= 0:
                break
            sleeve[t] = w
            used += w

    sleeve_used = sum(sleeve.values())
    # Core scaled to the remaining (1 - sleeve_used).
    ctot = sum(core.values()) or 1.0
    scaled_core = {k: (v / ctot) * (1 - sleeve_used) for k, v in core.items()}

    weights: dict[str, float] = dict(scaled_core)
    for t, w in sleeve.items():
        weights[t] = weights.get(t, 0.0) + w
    # normalize defensively
    tot = sum(weights.values())
    if tot > 0:
        weights = {k: v / tot for k, v in weights.items()}
    return weights, underweight_flags, list(sleeve.keys())


def proxy_pseudo_state(volume_z: float, momentum: float) -> str:
    """
    Map a (volume z-score, trailing momentum) pair to a pseudo crowd state.
    PROXY ONLY — captures attention+price, not real evidence/sentiment.
    """
    if volume_z >= 3.0 and momentum > 0.05:
        return "hype_acceleration"
    if volume_z >= 1.5 and momentum <= -0.02:
        return "crowd_exhaustion"
    if volume_z >= 1.0 and 0.0 <= momentum <= 0.08:
        return "emerging_dd"
    return "dormant_noise"


def _panel_proxy_states(panel, date: str, tickers: list[str], lookback: int = 20) -> list[dict[str, Any]]:
    """Compute proxy pseudo-states for tickers at `date` using data ≤ date only."""
    dates = [d for d in panel.dates if d <= date]
    if len(dates) < lookback + 2:
        return []
    window = dates[-(lookback + 1):]
    out: list[dict[str, Any]] = []
    for t in tickers:
        vols = [panel.volume(t, d) for d in window]
        vols = [v for v in vols if v is not None]
        c_prev = panel.close(t, window[0])
        c_now = panel.close(t, window[-1])
        if len(vols) < lookback // 2 or not (c_prev and c_now):
            continue
        mean_v = statistics.fmean(vols[:-1]) if len(vols) > 1 else vols[-1]
        std_v = statistics.pstdev(vols[:-1]) if len(vols) > 2 else 0.0
        vz = (vols[-1] - mean_v) / std_v if std_v > 0 else 0.0
        mom = (c_now / c_prev - 1.0) if c_prev > 0 else 0.0
        state = proxy_pseudo_state(vz, mom)
        out.append({"ticker": t, "crowd_state": state,
                    "crowd_research_priority_score": max(vz, 0.0)})
    return out


class CrowdTactic(TimeVaryingTactic):
    """Time-varying crowd-signal tactic (live or proxy mode)."""

    def __init__(self, core_weights: dict[str, float], *, mode: str = "proxy",
                 states_by_date: dict[str, list[dict]] | None = None,
                 sleeve_total: float = DEFAULT_SLEEVE_TOTAL, per_idea: float = DEFAULT_PER_IDEA,
                 priority_weighted: bool = True, proxy_universe: list[str] | None = None):
        super().__init__(
            tactic_id="crowd_signal_tactic",
            name="Crowd-Signal Tilt",
            source="crowd",
            target_weights=dict(core_weights),
            metadata={"mode": mode, "sleeve_total": sleeve_total, "per_idea": per_idea,
                      "priority_weighted": priority_weighted,
                      "materialization": {"rules": [
                          "capped sleeve toward emerging_dd/crowd_validation/contrarian_neglect",
                          "avoid-overlay ×0.8 trim on hype/squeeze/exhaustion core holdings"]}},
            approximate=(mode == "proxy"),
        )
        self.core = dict(core_weights)
        self.mode = mode
        self.states_by_date = states_by_date or {}
        self.sleeve_total = sleeve_total
        self.per_idea = per_idea
        self.priority_weighted = priority_weighted
        self.proxy_universe = proxy_universe or []
        self.last_flags: list[str] = []

    def _states_asof(self, date: str, ctx: dict | None) -> list[dict[str, Any]]:
        if self.mode == "live":
            # nearest snapshot ≤ date
            keys = sorted(k for k in self.states_by_date if k <= date)
            return self.states_by_date.get(keys[-1], []) if keys else []
        # proxy: derive from panel
        panel = (ctx or {}).get("panel")
        if panel is None:
            return []
        return _panel_proxy_states(panel, date, self.proxy_universe or panel.tickers)

    def target_weights_asof(self, date: str, ctx: dict | None = None) -> dict[str, float]:
        states = self._states_asof(date, ctx)
        weights, flags, _sleeve = build_crowd_sleeve(
            self.core, states, sleeve_total=self.sleeve_total,
            per_idea=self.per_idea, priority_weighted=self.priority_weighted)
        self.last_flags = flags
        return weights
