"""Sandbox shadow tracking + shadow portfolios (Phase 7, spec §10).

The sandbox proving ground: track discovered opportunities forward (proxies only)
and maintain *simulated* shadow portfolios so the operator can see, before
promoting anything, whether an idea would have helped. **Nothing here holds a
real position, trades, or writes the official decision plan.**

Writes (SANDBOX namespace, observe_only):
* ``shadow_opportunity_tracking.json`` — per-candidate forward tracking
* ``shadow_portfolios.json``            — 6 simulated portfolios + metrics
* ``candidate_promotion_review.json``   — radar items eligible for human review

``strategy_comparison.json`` is owned by the strategy comparator (§23.13), not
this module.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import (
    observe_only_envelope, OpportunityStatus as S, CandidateType,
    BOOM_BUCKET_TOTAL_CAP, BOOM_BUCKET_PER_IDEA_CAP,
)

_REVIEW_STATUSES = {S.QUALIFIED.value, S.APPROVED_WATCHLIST_REVIEW.value}


def _load_json_safe(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _config(root: Path) -> dict[str, Any]:
    return _load_json_safe(root / "config.json") or {}


# ---------------------------------------------------------------------------
# Shadow opportunity tracking
# ---------------------------------------------------------------------------


def build_shadow_tracking(root: Path, now_iso: str) -> dict[str, Any]:
    """Track radar candidates forward (proxies only). Continuity from prior file."""
    radar = _load_json_safe(root / "outputs" / "sandbox" / "opportunity_radar.json") or {}
    prior = _load_json_safe(root / "outputs" / "sandbox" / "shadow_opportunity_tracking.json") or {}
    prior_by = {r.get("candidate"): r for r in (prior.get("records", []) or [])}

    records: list[dict[str, Any]] = []
    for opp in radar.get("opportunities", []) or []:
        cand = opp.get("candidate")
        if not cand:
            continue
        existing = prior_by.get(cand, {})
        rec = {
            "candidate": cand,
            "theme": opp.get("theme", ""),
            "candidate_type": opp.get("candidate_type", ""),
            "discovered_date": existing.get("discovered_date", now_iso[:10]),
            "proxy_tickers": existing.get("proxy_tickers", []),
            # entry reference price only meaningful for public proxies; left None
            # until a price feed wires in (degrades gracefully).
            "entry_reference_price": existing.get("entry_reference_price"),
            "fwd_perf": existing.get("fwd_perf", {}),   # {"1d","3d","7d","30d"}
            "volatility": existing.get("volatility"),
            "drawdown": existing.get("drawdown"),
            "news_followthrough": opp.get("evidence_score"),
            "catalyst_persistence": opp.get("catalyst_strength"),
            "diversification_value": opp.get("portfolio_fit_score"),
            "would_have_helped_portfolio": None,  # filled when perf data exists
            "current_status": opp.get("final_status"),
            "observe_only": True,
        }
        records.append(rec)
    payload = observe_only_envelope(now_iso, source="shadow_tracker")
    payload["records"] = records
    payload["record_count"] = len(records)
    return payload


# ---------------------------------------------------------------------------
# Shadow portfolios (simulated weight vectors — never real positions)
# ---------------------------------------------------------------------------


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(v for v in weights.values() if v > 0)
    if total <= 0:
        return {}
    return {k: round(v / total, 4) for k, v in weights.items() if v > 0}


def _metrics(weights: dict[str, float], speculative: set[str]) -> dict[str, Any]:
    if not weights:
        return {"positions": 0, "max_weight": 0.0, "speculative_exposure": 0.0,
                "concentration_flag": False}
    spec_exp = round(sum(w for s, w in weights.items() if s in speculative), 4)
    return {
        "positions": len(weights),
        "max_weight": round(max(weights.values()), 4),
        "speculative_exposure": spec_exp,
        "concentration_flag": max(weights.values()) > 0.40,
        "within_boom_cap": spec_exp <= BOOM_BUCKET_TOTAL_CAP + 1e-9,
    }


def build_shadow_portfolios(root: Path, now_iso: str) -> dict[str, Any]:
    """Build the 6 simulated shadow portfolios (advisory weight vectors)."""
    cfg = _config(root)
    holdings = (cfg.get("portfolio", {}) or {}).get("holdings", []) or []
    radar = _load_json_safe(root / "outputs" / "sandbox" / "opportunity_radar.json") or {}

    # actual baseline — from config holdings (shares as proxy weight)
    actual = _normalize({str(h.get("symbol", "")).upper(): float(h.get("shares", 0) or 0)
                         for h in holdings if h.get("symbol")})
    # target allocation baseline — from config target weights
    target = _normalize({str(h.get("symbol", "")).upper(): float(h.get("target_weight", 0) or 0)
                         for h in holdings if h.get("symbol") and h.get("target_weight")})

    # engine-followed — actual unless a decision_plan exists (read-only reference)
    dp = _load_json_safe(root / "outputs" / "latest" / "decision_plan.json") or {}
    engine = dict(actual)  # advisory: we do not synthesize trades from the plan

    # lower-risk — trim the largest position toward equal weight (simulation)
    lower_risk = dict(actual)
    if lower_risk:
        mx = max(lower_risk, key=lower_risk.get)
        lower_risk[mx] = lower_risk[mx] * 0.7
        lower_risk = _normalize(lower_risk)

    # discovery-enhanced — core (90%) + top qualified radar names as a 10% sleeve
    qualified = [o["candidate"] for o in radar.get("opportunities", []) or []
                 if o.get("final_status") in _REVIEW_STATUSES
                 and o.get("candidate_type") != CandidateType.PRIVATE_IPO.value][:5]
    disc = {k: v * 0.9 for k, v in actual.items()}
    if qualified:
        per = 0.10 / len(qualified)
        for q in qualified:
            disc[q] = disc.get(q, 0.0) + per
    discovery_enhanced = _normalize(disc)

    # boom-bucket — core (≥85%) + capped speculative sleeve (≤15% total, ≤5%/idea)
    boom_names = [o["candidate"] for o in radar.get("opportunities", []) or []
                  if (o.get("boom_score") or 0) >= 0.4
                  and o.get("candidate_type") != CandidateType.PRIVATE_IPO.value][:4]
    boom = {k: v * (1 - BOOM_BUCKET_TOTAL_CAP) for k, v in actual.items()}
    speculative: set[str] = set()
    if boom_names:
        per = min(BOOM_BUCKET_PER_IDEA_CAP, BOOM_BUCKET_TOTAL_CAP / len(boom_names))
        for b in boom_names:
            boom[b] = boom.get(b, 0.0) + per
            speculative.add(b)
    boom_bucket = _normalize(boom)

    portfolios = {
        "actual_baseline": {"weights": actual, "metrics": _metrics(actual, set())},
        "target_allocation_baseline": {"weights": target, "metrics": _metrics(target, set())},
        "engine_followed": {"weights": engine, "metrics": _metrics(engine, set())},
        "lower_risk": {"weights": lower_risk, "metrics": _metrics(lower_risk, set())},
        "discovery_enhanced": {"weights": discovery_enhanced,
                               "metrics": _metrics(discovery_enhanced, set(qualified))},
        "boom_bucket": {"weights": boom_bucket, "metrics": _metrics(boom_bucket, speculative)},
    }
    payload = observe_only_envelope(now_iso, source="shadow_tracker",
                                    boom_total_cap=BOOM_BUCKET_TOTAL_CAP,
                                    boom_per_idea_cap=BOOM_BUCKET_PER_IDEA_CAP)
    payload["portfolios"] = portfolios
    return payload


def build_promotion_review(root: Path, now_iso: str) -> dict[str, Any]:
    """Radar items at QUALIFIED/APPROVED → human review surface (promote to watchlist only)."""
    radar = _load_json_safe(root / "outputs" / "sandbox" / "opportunity_radar.json") or {}
    cands = [{
        "candidate": o.get("candidate"), "candidate_type": o.get("candidate_type"),
        "theme": o.get("theme", ""), "final_status": o.get("final_status"),
        "opportunity_score": o.get("opportunity_score"), "boom_score": o.get("boom_score"),
        "risk_score": o.get("risk_score"), "investability_score": o.get("investability_score"),
        "allowed_actions": ["approve_to_watchlist_review", "send_to_sandbox",
                            "keep_watching", "request_deeper_research", "reject"],
        "blocked_actions": ["place_trade", "submit_order", "modify_real_holdings"],
    } for o in radar.get("opportunities", []) or []
        if o.get("final_status") in _REVIEW_STATUSES]
    payload = observe_only_envelope(now_iso, source="shadow_tracker")
    payload["candidates"] = cands
    payload["candidate_count"] = len(cands)
    return payload


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_shadow_artifacts(root: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    base = root / "outputs"
    try:
        tracking = build_shadow_tracking(root, now_iso)
        portfolios = build_shadow_portfolios(root, now_iso)
        review = build_promotion_review(root, now_iso)
        safe_write_json(OutputNamespace.SANDBOX, "shadow_opportunity_tracking.json", tracking, base_dir=base)
        safe_write_json(OutputNamespace.SANDBOX, "shadow_portfolios.json", portfolios, base_dir=base)
        safe_write_json(OutputNamespace.SANDBOX, "candidate_promotion_review.json", review, base_dir=base)
        return {"tracked": tracking["record_count"], "review_count": review["candidate_count"],
                "degraded": False}
    except Exception as exc:
        for fn, key in (("shadow_opportunity_tracking.json", "records"),
                        ("shadow_portfolios.json", "portfolios"),
                        ("candidate_promotion_review.json", "candidates")):
            deg = observe_only_envelope(now_iso, source="shadow_tracker",
                                        degraded_mode=True, degraded_reason=str(exc))
            deg[key] = [] if key != "portfolios" else {}
            try:
                safe_write_json(OutputNamespace.SANDBOX, fn, deg, base_dir=base)
            except Exception:
                pass
        return {"tracked": 0, "review_count": 0, "degraded": True}
