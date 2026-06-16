"""Flock Intelligence producer — orchestrates metrics -> classification ->
simulation artifacts.

Writes ONLY to the SIMULATION namespace (outputs/simulation/). It never touches
production; it changes simulation context/watchlist/advisory candidates that the
sim-governance lane consumes, and the GUI displays. Production behavior changes
only via the human-approved promotion workflow.

Artifacts written:
  * outputs/simulation/flock_intelligence.json          (full report: groups + tickers)
  * outputs/simulation/flock_watchlist_candidates.json  (sim watchlist adds/tags/ranks)
  * outputs/simulation/flock_advisory_context.json      (per-symbol advisory context)
  * outputs/simulation/flock_state_history.json         (prior-state ledger for next run)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.flock_intelligence import data_sources as ds
from portfolio_automation.flock_intelligence import metrics as M
from portfolio_automation.flock_intelligence.states import (
    FlockState, GroupFlock, GroupMetrics, TickerFlock, Thresholds, classify_group,
)

logger = logging.getLogger("stockbot.flock_intelligence.producer")

_DISCLAIMER = (
    "Flock Intelligence is simulation-only research context. It detects crowd "
    "flocking/dispersion across themes/sectors/tickers. It is NOT a buy/sell/hold "
    "recommendation and never changes production behavior; production may use it "
    "only after a human-approved promotion proposal."
)
_OBSERVE_FIELDS = {
    "observe_only": True, "no_trade": True, "not_recommendation": True,
    "sandbox_only": True, "simulation_only": True,
}


def build_group_metrics(group: str, kind: str, tickers: list[str],
                        crowd: dict[str, dict[str, float]],
                        returns: dict[str, dict[str, float]],
                        prior: dict[str, Any] | None) -> GroupMetrics:
    """Assemble all raw metrics + composite scores for one group (pure)."""
    tickers = [t.upper() for t in tickers]
    crowd_sub = {t: crowd.get(t, {}) for t in tickers}
    velocity_by = {t: c.get("velocity", 0.0) for t, c in crowd_sub.items() if c}
    breadth_by = {t: c.get("breadth", 0.0) for t, c in crowd_sub.items() if c}
    mention_by = {t: c.get("mentions", 0.0) for t, c in crowd_sub.items() if c}
    has_crowd = any(crowd_sub.values())

    aligned = ds.aligned_group_returns(returns, tickers)
    latest = ds.latest_returns(returns, tickers)
    n_with_returns = len(aligned)
    history_points = max((len(v) for v in aligned.values()), default=0)

    avg_corr = M.average_pairwise_correlation(aligned)
    ret_spread = M.return_spread(latest)
    momentum = M.group_momentum(latest)
    vol_now = M.group_volatility(aligned)
    prior_corr = (prior or {}).get("avg_correlation")
    prior_vol = (prior or {}).get("volatility")
    vol_change = ((vol_now - prior_vol) / prior_vol) if (prior_vol and prior_vol > 0) else 0.0

    velocity = M.crowd_velocity(velocity_by)
    breadth = M.crowd_breadth(velocity_by, len(tickers))
    src_breadth = M.source_breadth(breadth_by)
    concentration = M.mention_concentration(mention_by)

    fscore = M.flock_score(velocity=velocity, breadth=breadth, avg_corr=avg_corr,
                           momentum=momentum)
    dscore = M.dispersion_score(avg_corr=avg_corr, prior_avg_corr=prior_corr,
                                ret_spread=ret_spread, breadth=breadth,
                                concentration=concentration, vol_change=vol_change)
    escore = M.exhaustion_score(velocity=velocity, concentration=concentration,
                                breadth=breadth,
                                prior_breadth=(prior or {}).get("breadth"),
                                vol_change=vol_change, momentum=momentum)

    return GroupMetrics(
        group=group, group_kind=kind, tickers=tickers, n_tickers=len(tickers),
        n_with_returns=n_with_returns, history_points=history_points,
        has_crowd_data=has_crowd, crowd_velocity=velocity, crowd_breadth=breadth,
        source_breadth=src_breadth, mention_concentration=concentration,
        avg_correlation=avg_corr, prior_avg_correlation=prior_corr,
        return_spread=ret_spread, group_momentum=momentum, volatility_change=vol_change,
        flock_score=fscore, dispersion_score=dscore, exhaustion_score=escore,
    )


def _ticker_flocks(gm: GroupMetrics, gf: GroupFlock,
                   crowd: dict[str, dict[str, float]],
                   returns: dict[str, dict[str, float]]) -> list[TickerFlock]:
    """Per-ticker context: relationship of each ticker to its group's flock."""
    aligned = ds.aligned_group_returns(returns, gm.tickers)
    latest = ds.latest_returns(returns, gm.tickers)
    out: list[TickerFlock] = []
    for tk in gm.tickers:
        c = crowd.get(tk, {})
        # correlation of this ticker vs the group-average series
        corr_to_group: float | None = None
        if tk in aligned and len(aligned) >= 2:
            others = [s for t, s in aligned.items() if t != tk]
            n = min([len(aligned[tk])] + [len(s) for s in others])
            if n >= M.MIN_CORR_POINTS:
                group_avg = [sum(s[-n:][i] for s in others) / len(others) for i in range(n)]
                corr_to_group = M.pairwise_correlation(aligned[tk][-n:], group_avg)
        rs = (latest.get(tk) - gm.group_momentum) if tk in latest else None
        out.append(TickerFlock(
            ticker=tk, group=gm.group, group_kind=gm.group_kind,
            flock_state=gf.flock_state, flock_score=gf.flock_score,
            dispersion_score=gf.dispersion_score,
            crowd_velocity=round(float(c.get("velocity", 0.0)), 4),
            crowd_breadth=round(float(c.get("breadth", 0.0)), 4),
            mention_concentration=gm.mention_concentration,
            price_correlation_to_group=(round(corr_to_group, 4) if corr_to_group is not None else None),
            relative_strength_vs_group=(round(rs, 4) if rs is not None else None),
            volatility_change=round(gm.volatility_change, 4),
            confidence=gf.confidence,
            explanation=f"{tk} in '{gm.group}': {gf.flock_state}.",
            evidence_refs=["outputs/sandbox/discovery/crowd_multi_source_velocity.json",
                           "outputs/performance/signal_outcomes.csv"],
        ))
    return out


_TAG_BY_STATE = {
    FlockState.FLOCK_FORMING.value: "flock_forming",
    FlockState.FLOCK_CONFIRMED.value: "flock_confirmed",
    FlockState.FLOCK_EXHAUSTION.value: "crowded_trade",
    FlockState.FLOCK_DISPERSING.value: "dispersion_risk",
    FlockState.FLOCK_BROKEN.value: "dispersion_risk",
    FlockState.INSUFFICIENT_DATA.value: "insufficient_flock_data",
}


def _watchlist_candidates(groups: list[GroupFlock], watchlist: set[str]) -> list[dict[str, Any]]:
    """Derive simulation watchlist adds / tags / rank deltas from flock states."""
    cands: list[dict[str, Any]] = []
    for gf in groups:
        tag = _TAG_BY_STATE.get(gf.flock_state, "insufficient_flock_data")
        for tk in gf.tickers:
            on_wl = tk in watchlist
            if gf.flock_state == FlockState.FLOCK_FORMING.value and not on_wl:
                cands.append({"ticker": tk, "group": gf.group, "action": "add",
                              "tags": [tag, "rotation_candidate"], "flock_state": gf.flock_state,
                              "flock_score": gf.flock_score, "confidence": gf.confidence,
                              "sim_rank_delta": 0,
                              "rationale": f"Emerging flock in '{gf.group}': {gf.explanation}"})
            elif on_wl and gf.flock_state in (FlockState.FLOCK_DISPERSING.value,
                                              FlockState.FLOCK_BROKEN.value):
                cands.append({"ticker": tk, "group": gf.group, "action": "rank",
                              "tags": [tag], "flock_state": gf.flock_state,
                              "flock_score": gf.flock_score, "confidence": gf.confidence,
                              "sim_rank_delta": +1,  # lower priority (higher rank number)
                              "rationale": f"Flock breaking in '{gf.group}': {gf.explanation}"})
            elif on_wl and gf.flock_state in (FlockState.FLOCK_CONFIRMED.value,
                                              FlockState.FLOCK_EXHAUSTION.value):
                cands.append({"ticker": tk, "group": gf.group, "action": "tag",
                              "tags": [tag], "flock_state": gf.flock_state,
                              "flock_score": gf.flock_score, "confidence": gf.confidence,
                              "sim_rank_delta": 0,
                              "rationale": f"'{gf.group}' {gf.flock_state}: {gf.explanation}"})
    return cands


_MEANING = {
    FlockState.FLOCK_FORMING.value: "Crowd/price cohesion is building; early-stage interest.",
    FlockState.FLOCK_CONFIRMED.value: "Broad crowd/price structure supports continued monitoring.",
    FlockState.FLOCK_EXHAUSTION.value: "Support exists but attention is concentrated; crowd may be late.",
    FlockState.FLOCK_DISPERSING.value: "Shared movement is breaking down; less broad than prior run.",
    FlockState.FLOCK_BROKEN.value: "Prior flock has dissolved; leaders and laggards have split.",
    FlockState.INSUFFICIENT_DATA.value: "Needs more daily history to classify crowd structure.",
}


def _advisory_context(tickers: list[TickerFlock]) -> dict[str, Any]:
    """Per-symbol advisory flock context (best/most-confident group per ticker)."""
    by_symbol: dict[str, Any] = {}
    for tf in sorted(tickers, key=lambda t: t.confidence, reverse=True):
        if tf.ticker in by_symbol:
            continue
        by_symbol[tf.ticker] = {
            "flock_state": tf.flock_state, "group": tf.group,
            "flock_score": tf.flock_score, "dispersion_score": tf.dispersion_score,
            "confidence": tf.confidence,
            "label": f"{tf.group}: {tf.flock_state.replace('_', ' ')}",
            "meaning": _MEANING.get(tf.flock_state, ""),
        }
    return by_symbol


def run_flock_intelligence(root: Path | str, now: str, *, base_dir: str | None = None,
                           write_files: bool = True,
                           watchlist: list[str] | None = None,
                           groups_override: list[tuple[str, str, list[str]]] | None = None,
                           crowd_override: dict[str, dict[str, float]] | None = None,
                           returns_override: dict[str, dict[str, float]] | None = None,
                           th: Thresholds | None = None) -> dict[str, Any]:
    """Build + (optionally) write the Flock Intelligence simulation artifacts.

    All inputs are injectable for tests. Never raises; returns a degraded report
    with ``data_quality_status='insufficient_data'`` when there is nothing to classify.
    """
    root = Path(root)
    base_dir = base_dir or str(root / "outputs")
    th = th or Thresholds()

    universe = ds.load_universe(root)
    if groups_override is not None:
        groups_in = groups_override
    else:
        groups_in = ds.load_theme_groups(root)
        seen = {tk for _, _, tks in groups_in for tk in tks}
        for g in ds.load_sector_groups(root, [t for t in universe if t not in seen]):
            groups_in.append(g)
    crowd = crowd_override if crowd_override is not None else ds.load_crowd_metrics(root)
    returns = returns_override if returns_override is not None else ds.load_returns(root)
    prior = ds.load_prior_states(root)
    wl = {t.upper() for t in (watchlist if watchlist is not None else universe)}

    group_flocks: list[GroupFlock] = []
    ticker_flocks: list[TickerFlock] = []
    history: dict[str, Any] = {}
    for name, kind, tickers in groups_in:
        gm = build_group_metrics(name, kind, tickers, crowd, returns,
                                 prior.get(name))
        gf = classify_group(gm, prior_state=(prior.get(name) or {}).get("state"), th=th)
        group_flocks.append(gf)
        ticker_flocks.extend(_ticker_flocks(gm, gf, crowd, returns))
        history[name] = {"state": gf.flock_state, "avg_correlation": gm.avg_correlation,
                         "volatility": M.group_volatility(ds.aligned_group_returns(returns, tickers)),
                         "breadth": gm.crowd_breadth}

    def _members(state: str) -> list[str]:
        return [g.group for g in group_flocks if g.flock_state == state]

    data_quality = "ok" if any(g.flock_state != FlockState.INSUFFICIENT_DATA.value
                               for g in group_flocks) else "insufficient_data"
    report = {
        "source": "flock_intelligence", "schema_version": "1", "generated_at": now,
        **_OBSERVE_FIELDS, "data_quality_status": data_quality,
        "group_count": len(group_flocks), "ticker_count": len(ticker_flocks),
        "groups": [g.to_dict() for g in group_flocks],
        "tickers": [t.to_dict() for t in ticker_flocks],
        "summary": {
            "forming": _members(FlockState.FLOCK_FORMING.value),
            "confirmed": _members(FlockState.FLOCK_CONFIRMED.value),
            "exhaustion": _members(FlockState.FLOCK_EXHAUSTION.value),
            "dispersing": _members(FlockState.FLOCK_DISPERSING.value),
            "broken": _members(FlockState.FLOCK_BROKEN.value),
            "insufficient": _members(FlockState.INSUFFICIENT_DATA.value),
        },
        "disclaimer": _DISCLAIMER,
    }
    wl_candidates = {
        "source": "flock_watchlist_candidates", "generated_at": now, **_OBSERVE_FIELDS,
        "candidates": _watchlist_candidates(group_flocks, wl), "disclaimer": _DISCLAIMER,
    }
    adv_context = {
        "source": "flock_advisory_context", "generated_at": now, **_OBSERVE_FIELDS,
        "by_symbol": _advisory_context(ticker_flocks), "disclaimer": _DISCLAIMER,
    }
    state_history = {"source": "flock_state_history", "generated_at": now, "groups": history}

    if write_files:
        for name, payload in (("flock_intelligence.json", report),
                              ("flock_watchlist_candidates.json", wl_candidates),
                              ("flock_advisory_context.json", adv_context),
                              ("flock_state_history.json", state_history)):
            try:
                safe_write_json(OutputNamespace.SIMULATION, name, payload, base_dir=base_dir)
            except Exception as exc:  # telemetry must never break the pipeline
                logger.warning("flock_intelligence: write %s failed: %s", name, exc)
                report.setdefault("write_errors", []).append(f"{name}: {exc}")

    return {"report": report, "watchlist_candidates": wl_candidates,
            "advisory_context": adv_context, "state_history": state_history}
