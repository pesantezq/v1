# portfolio_automation/regime_coverage.py
"""regime_coverage — WS14 regime-concentration validity assessor (observe-only).

.superpowers/audit/ws-04-05-14-18-health.md (WS14) found that
``outputs/regime/regime_performance.json`` is purely observational: no
assessor anywhere (backtest_health, strategy_lab_health, quant_watch_probes)
reads its ``by_regime`` breakdown into a validity verdict. Confirmed live
(2026-07-28): 2211/2238 resolved signals (98.8%) are ``neutral``; only 27
(1.2%) are ``high_volatility``; return-weighted concentration is ~96%. The
2026-06-23 neutral-collapse guard (``semantic_liveness.py``'s
``detect_single_value_collapse``) catches a DIFFERENT failure — a
producer-ordering bug that collapses the column to a single value — and
explicitly whitelists ``"neutral"`` as a legitimate single state. It cannot
catch (and was never meant to catch) today's real, non-degenerate two-or-
three-label distribution that is nonetheless massively concentrated. This
module is the distributional-skew check that guard structurally cannot be.

Contract
--------
Pure function ``assess_regime_coverage(regime_perf)`` over the ALREADY
enriched ``by_regime`` shape produced by
``watchlist_scanner.performance_feedback.build_regime_performance_summary``
(count / effective_signals / return / excess_return / hit_rate / drawdown /
uncertainty / share_of_evidence / return_weighted_share per regime — added
alongside this module). Returns one or more of the four explicit states:

    REGIME_COVERAGE_BALANCED   — no single regime dominates evidence AND
                                 risk_off carries a sufficient effective
                                 sample. The only state that does NOT
                                 downgrade a claimed edge's credibility.
    REGIME_CONCENTRATED        — a single regime holds >= CONCENTRATION_
                                 SHARE_THRESHOLD of resolved evidence, by
                                 count share or |return-weighted share|.
    RISK_OFF_UNPROVEN          — the risk_off regime label is absent, or has
                                 fewer than MIN_REGIME_EFFECTIVE_N full-
                                 quality observations. A strategy's behavior
                                 in a genuine risk-off regime remains
                                 unproven regardless of how much neutral-
                                 regime evidence exists.
    REGIME_DATA_INSUFFICIENT  — fewer than MIN_RESOLVED_TOTAL resolved
                                 signals exist at all; too thin to assess
                                 coverage one way or the other. This state
                                 alone does NOT trigger a downgrade
                                 elsewhere (absence of evidence is not
                                 evidence of concentration).

States are not mutually exclusive at the data-sufficient tier — a window can
be simultaneously REGIME_CONCENTRATED and RISK_OFF_UNPROVEN (this is, in
fact, today's real state — the fix does not tune toward a friendlier label).

Consumer: ``portfolio_automation.portfolio_sim.strategy_lab_health`` reads
this assessor's ``states`` to downgrade its ``ranking_credibility`` and
``oos_validity`` dimensions with a stated reason (WS14 item 3) — a strategy
whose evidence is ~99% one regime must not read as generally validated.

Observe-only: never mutates decisions, scores, or portfolio state. Writes
only its own status artifact under OutputNamespace.LATEST.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

__all__ = [
    "REGIME_COVERAGE_BALANCED", "REGIME_CONCENTRATED", "RISK_OFF_UNPROVEN",
    "REGIME_DATA_INSUFFICIENT", "assess_regime_coverage", "run_regime_coverage",
]

REGIME_COVERAGE_BALANCED = "REGIME_COVERAGE_BALANCED"
REGIME_CONCENTRATED = "REGIME_CONCENTRATED"
RISK_OFF_UNPROVEN = "RISK_OFF_UNPROVEN"
REGIME_DATA_INSUFFICIENT = "REGIME_DATA_INSUFFICIENT"

# Below this many total resolved signals, the window is too thin to assess
# coverage at all (neither "balanced" nor "concentrated" is a supportable
# claim) — mirrors the existing semantic_liveness / quant_watch_probes
# min_sample convention used elsewhere in this codebase.
MIN_RESOLVED_TOTAL = 30

# A regime needs at least this many FULL-quality (not degraded/limited/
# partial) observations before its evidence counts as sufficient to prove
# behavior in that regime.
MIN_REGIME_EFFECTIVE_N = 30

# A single regime holding this share (by count OR by |return-weighted share|)
# of resolved evidence is "concentrated" — any claimed edge is really that
# one regime's performance wearing an all-regime label.
CONCENTRATION_SHARE_THRESHOLD = 0.80

RISK_OFF_LABEL = "risk_off"

_STATUS_REL = "regime_coverage_status.json"  # under outputs/latest/

_DISCLAIMER = (
    "Observe-only regime-coverage evidence-sufficiency check (WS14). Measures "
    "SHARE of resolved evidence per regime label, not label cardinality — "
    "distinct from the semantic_liveness neutral-collapse guard, which only "
    "catches a producer-ordering bug collapsing the column to one value. "
    "Never mutates decisions, scores, allocation, or portfolio state. A "
    "concentrated/unproven verdict is an evidence-sufficiency caveat on a "
    "claimed edge, not a market-timing signal."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def assess_regime_coverage(
    regime_perf: dict[str, Any] | None,
    *,
    min_resolved_total: int = MIN_RESOLVED_TOTAL,
    min_regime_effective_n: int = MIN_REGIME_EFFECTIVE_N,
    concentration_share_threshold: float = CONCENTRATION_SHARE_THRESHOLD,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Assess regime-coverage concentration from a
    ``build_regime_performance_summary``-shaped dict. Pure; never raises on
    malformed input (degrades to REGIME_DATA_INSUFFICIENT)."""
    now = now_iso or _now_iso()
    by_regime = dict((regime_perf or {}).get("by_regime") or {})
    resolved_total = (regime_perf or {}).get("resolved_signals")
    if not isinstance(resolved_total, (int, float)):
        resolved_total = sum(int((m or {}).get("total_signals") or 0) for m in by_regime.values())
    resolved_total = int(resolved_total or 0)

    if not by_regime or resolved_total < min_resolved_total:
        reason = (
            f"only {resolved_total} resolved signal(s) across {len(by_regime)} regime "
            f"label(s) (< {min_resolved_total}) — too thin to assess regime coverage "
            "either way"
        )
        return {
            "generated_at": now, "observe_only": True, "schema_version": "1",
            "source": "regime_coverage",
            "resolved_signals": resolved_total,
            "by_regime": {}, "concentration": {}, "risk_off": {"present": False, "effective_signals": 0},
            "states": [REGIME_DATA_INSUFFICIENT], "primary_state": REGIME_DATA_INSUFFICIENT,
            "reasons": [reason], "disclaimer": _DISCLAIMER,
        }

    states: list[str] = []
    reasons: list[str] = []

    count_shares = {r: float((m or {}).get("share_of_evidence") or 0.0) for r, m in by_regime.items()}
    rw_shares = {r: (m or {}).get("return_weighted_share") for r, m in by_regime.items()
                if (m or {}).get("return_weighted_share") is not None}

    max_count_regime = max(count_shares, key=lambda k: count_shares[k]) if count_shares else None
    max_count_share = count_shares.get(max_count_regime) if max_count_regime is not None else None
    max_rw_regime = max(rw_shares, key=lambda k: abs(rw_shares[k])) if rw_shares else None
    max_rw_share = rw_shares.get(max_rw_regime) if max_rw_regime is not None else None

    concentrated = (
        (max_count_share is not None and max_count_share >= concentration_share_threshold)
        or (max_rw_share is not None and abs(max_rw_share) >= concentration_share_threshold)
    )
    if concentrated:
        states.append(REGIME_CONCENTRATED)
        if max_rw_share is not None:
            reasons.append(
                f"regime '{max_count_regime}' holds {max_count_share:.1%} of resolved "
                f"evidence by count (return-weighted {max_rw_share:.1%} via "
                f"'{max_rw_regime}') — any claimed edge is concentrated in one regime")
        else:
            reasons.append(
                f"regime '{max_count_regime}' holds {max_count_share:.1%} of resolved "
                "evidence by count — any claimed edge is concentrated in one regime")

    risk_off_metrics = by_regime.get(RISK_OFF_LABEL)
    risk_off_present = risk_off_metrics is not None
    risk_off_effective = int((risk_off_metrics or {}).get("effective_signals") or 0)
    if not risk_off_present:
        states.append(RISK_OFF_UNPROVEN)
        reasons.append(
            "risk_off regime label never observed in resolved evidence — strategy/"
            "gauge behavior in a genuine risk-off regime is completely unproven")
    elif risk_off_effective < min_regime_effective_n:
        states.append(RISK_OFF_UNPROVEN)
        reasons.append(
            f"risk_off effective_signals={risk_off_effective} < {min_regime_effective_n} "
            "— insufficient full-quality evidence to claim risk-off validity")

    if not states:
        states.append(REGIME_COVERAGE_BALANCED)
        reasons.append(
            "no single regime dominates resolved evidence and risk_off carries a "
            "sufficient effective sample")

    priority = [REGIME_DATA_INSUFFICIENT, RISK_OFF_UNPROVEN, REGIME_CONCENTRATED, REGIME_COVERAGE_BALANCED]
    primary_state = next(s for s in priority if s in states)

    return {
        "generated_at": now, "observe_only": True, "schema_version": "1",
        "source": "regime_coverage",
        "resolved_signals": resolved_total,
        "by_regime": {
            r: {
                "total_signals": (m or {}).get("total_signals"),
                "effective_signals": (m or {}).get("effective_signals"),
                "avg_return_pct": (m or {}).get("avg_return_pct"),
                "excess_return_pct": (m or {}).get("excess_return_pct"),
                "win_rate": (m or {}).get("win_rate"),
                "drawdown_pct": (m or {}).get("drawdown_pct"),
                "hit_rate_uncertainty_pp": (m or {}).get("hit_rate_uncertainty_pp"),
                "share_of_evidence": (m or {}).get("share_of_evidence"),
                "return_weighted_share": (m or {}).get("return_weighted_share"),
            }
            for r, m in by_regime.items()
        },
        "concentration": {
            "max_share_regime": max_count_regime, "max_share": max_count_share,
            "return_weighted_max_share_regime": max_rw_regime,
            "return_weighted_max_share": max_rw_share,
            "threshold": concentration_share_threshold,
        },
        "risk_off": {
            "present": risk_off_present, "effective_signals": risk_off_effective,
            "min_required": min_regime_effective_n,
        },
        "states": states,
        "primary_state": primary_state,
        "reasons": reasons,
        "disclaimer": _DISCLAIMER,
    }


def run_regime_coverage(*, root: str | Path = ".", now_iso: str | None = None,
                        write_files: bool = True) -> dict[str, Any]:
    """Load ``outputs/regime/regime_performance.json`` → assess → (optionally)
    write ``outputs/latest/regime_coverage_status.json``. Never raises —
    degrades to a REGIME_DATA_INSUFFICIENT-shaped status on any error."""
    root_path = Path(root).resolve()
    now = now_iso or _now_iso()
    try:
        perf_path = root_path / "outputs" / "regime" / "regime_performance.json"
        regime_perf = json.loads(perf_path.read_text(encoding="utf-8")) if perf_path.exists() else None
        status = assess_regime_coverage(regime_perf, now_iso=now)
        if write_files:
            safe_write_json(OutputNamespace.LATEST, _STATUS_REL, status,
                            base_dir=root_path / "outputs")
        return status
    except Exception as exc:
        return {
            "generated_at": now, "observe_only": True, "schema_version": "1",
            "source": "regime_coverage", "resolved_signals": 0,
            "by_regime": {}, "concentration": {}, "risk_off": {"present": False, "effective_signals": 0},
            "states": [REGIME_DATA_INSUFFICIENT], "primary_state": REGIME_DATA_INSUFFICIENT,
            "reasons": [f"error computing regime coverage: {exc}"],
            "disclaimer": _DISCLAIMER,
        }
