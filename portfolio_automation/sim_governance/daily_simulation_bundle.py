"""
Daily Simulation Bundle (spec §3 Step 3).

Consolidates the active simulation lane's output into ONE evidence bundle:
``outputs/simulation/daily_simulation_bundle.json``.

The bundle is the single source the AI/product review reads. It carries the
before/after comparison against the production baseline, plus aggregate risk,
confidence, and data-quality summaries so the downstream review packet can be
compressed without losing the decision-relevant signal.
"""
from __future__ import annotations

import logging
from pathlib import Path

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.sim_governance import schemas as S

logger = logging.getLogger("stockbot.sim_governance.daily_simulation_bundle")

_BUNDLE_FILENAME = "daily_simulation_bundle.json"


def _bucket(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _summarize_unified_crowd(base_dir: str) -> dict:
    """Compact unified-crowd evidence for the daily AI/product review packet.

    Read-only; kept SMALL (ticker lists only, capped) so it adds negligible tokens
    to the single consolidated review and never threatens the $0.50/day cap. No
    extra AI call — this is packet context, embedded in the one existing review.
    """
    import json as _json
    from pathlib import Path as _Path

    def _tickers(rows, n=8):
        return [str(r.get("ticker")) for r in (rows or [])[:n] if r.get("ticker")]

    try:
        path = _Path(base_dir) / "latest" / "unified_crowd_intelligence_status.json"
        st = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    return {
        "available": True,
        "overall_status": st.get("overall_status"),
        "total_tickers": st.get("total_tickers"),
        "lane_a_tickers": st.get("lane_a_tickers"),
        "lane_b_tickers": st.get("lane_b_tickers"),
        "overlap_tickers": st.get("overlap_tickers"),
        "social_sentiment_status": st.get("social_sentiment_status"),
        "crowd_confidence_avg": st.get("crowd_confidence_avg"),
        "state_counts": st.get("state_counts", {}),
        "top_confirmed_attention": _tickers(st.get("top_confirmed_attention")),
        "top_divergent_attention": _tickers(st.get("top_divergent_attention")),
        "top_retail_only_attention": _tickers(st.get("top_retail_only_attention")),
        "top_institutional_context_only": _tickers(st.get("top_institutional_context_only")),
        # confirmed cross-source attention = the candidates with the strongest
        # evidence for a future production-readiness review (the reviewer may
        # recommend; human approval is required for any production change).
        "candidates_ready_for_production_review": _tickers(st.get("top_confirmed_attention")),
        "note": "Simulation-active crowd evidence; never feeds the production "
                "decision engine. Human approval required for any production change.",
    }


def build_daily_simulation_bundle(
    lane_result: dict,
    now: str,
    *,
    base_dir: str,
    write_files: bool = True,
) -> dict:
    """Build (and optionally write) the consolidated daily simulation bundle.

    Args:
        lane_result: the dict returned by simulation_lane.run_simulation_lane.
        now: ISO timestamp (caller-supplied).
        base_dir: outputs base dir.
    """
    candidates = lane_result.get("candidates", []) or []

    advisory = [c for c in candidates if c.get("workflow") == S.WORKFLOW_ADVISORY]
    watchlist = [c for c in candidates if c.get("workflow") == S.WORKFLOW_WATCHLIST]
    crowd = [c for c in candidates if c.get("proposal_type") == S.PROPOSAL_CROWD_CONTEXT]
    discovery = [c for c in candidates
                 if c.get("proposal_type") in (S.PROPOSAL_DISCOVERY_PROMOTION, S.PROPOSAL_WATCHLIST_ADD)]

    baseline = lane_result.get("production_baseline", {}) or {}
    prod_wl = [str(t).upper() for t in baseline.get("watchlist", [])]
    sim_wl = lane_result.get("simulated_watchlist", []) or []
    added = sorted(set(sim_wl) - set(prod_wl))
    removed = sorted(set(prod_wl) - set(sim_wl))

    confidences = [float(c.get("confidence", 0.0)) for c in candidates]
    avg_conf = round(sum(confidences) / len(confidences), 4) if confidences else 0.0

    bundle = {
        "generated_at": now,
        "schema": "daily_simulation_bundle.v1",
        "lane": "simulation",
        "lane_active": True,
        "candidate_count": len(candidates),
        "ready_count": sum(1 for c in candidates if c.get("ready_for_production_review")),
        # ── experiment results, split by workflow/category ──────────────────
        "advisory_experiment_results": advisory,
        "watchlist_experiment_results": watchlist,
        "crowd_experiment_results": crowd,
        "discovery_candidates": discovery,
        # ── comparison against production baseline ──────────────────────────
        "comparison_vs_production_baseline": {
            "watchlist": {
                "production": prod_wl,
                "simulated": sim_wl,
                "added": added,
                "removed": removed,
                "changed": bool(added or removed),
            },
            "advisory": {
                "production_count": len(baseline.get("advisory", []) or []),
                "simulated_count": len(lane_result.get("simulated_advisory", []) or []),
            },
        },
        # ── aggregate summaries ─────────────────────────────────────────────
        # ── unified crowd evidence (compact; embedded in the one AI review) ──
        "unified_crowd_summary": _summarize_unified_crowd(base_dir),
        "data_quality": _bucket([str(c.get("data_quality", "unknown")) for c in candidates]),
        "risk_summary": _bucket([str(c.get("risk_impact", "unknown")) for c in candidates]),
        "confidence_summary": {
            "average": avg_conf,
            "min": round(min(confidences), 4) if confidences else 0.0,
            "max": round(max(confidences), 4) if confidences else 0.0,
        },
        # ── artifact refs (provenance) ──────────────────────────────────────
        "artifact_refs": [
            "outputs/sandbox/sim_governance/simulation_candidates.json",
            "outputs/sandbox/sim_governance/simulated_watchlist.json",
            "outputs/sandbox/sim_governance/simulated_advisory.json",
        ],
    }

    if write_files:
        try:
            safe_write_json(OutputNamespace.SIMULATION, _BUNDLE_FILENAME, bundle, base_dir=base_dir)
        except Exception as exc:
            logger.warning("daily_simulation_bundle: write failed: %s", exc)
            bundle["write_error"] = str(exc)

    return bundle
