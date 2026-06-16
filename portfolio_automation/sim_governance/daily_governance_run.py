"""
Daily simulation-governance orchestrator (spec §3, §12).

Runs the full daily lane AFTER the production baseline artifacts already exist:

  baseline snapshot
    -> active simulation lane (may change simulation outputs)
    -> daily simulation bundle (evidence)
    -> consolidated AI/product review packet (advisory + watchlist together)
    -> ONE gated AI/product review (<= $0.50/day, else deferred)
    -> pending production proposals for READY candidates
    -> apply already-human-approved proposals to the production overlays

Every step is wrapped so a failure in one stage never sinks the pipeline
(non-blocking integration). Reads its knobs from config.json ``sim_governance``.

Writes a compact status artifact for the GUI / daily-tool-analysis:
  * outputs/promotion_review/daily_governance_status.json
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.sim_governance import (
    ai_review_packet,
    daily_ai_review,
    daily_simulation_bundle,
    production_application,
    promotion_approvals,
    promotion_proposals,
    simulation_lane,
)

logger = logging.getLogger("stockbot.sim_governance.daily_governance_run")

_STATUS_FILE = "daily_governance_status.json"

_DEFAULTS = {
    "enabled": True,
    "simulation_lane": {"enabled": True},
    "ai_review": {
        "enabled": True,
        "daily_cost_cap_usd": 0.50,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "max_calls_per_day": 1,
    },
    "production_application": {
        "apply_watchlist_overlay": False,
        "apply_advisory_overlay": False,
    },
}


def load_sim_governance_config(root: Path) -> dict:
    """Read the sim_governance config block from config.json (with defaults)."""
    try:
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
        block = cfg.get("sim_governance", {}) or {}
    except Exception:
        block = {}
    merged = {**_DEFAULTS, **block}
    # shallow-merge nested dicts
    for k in ("simulation_lane", "ai_review", "production_application"):
        merged[k] = {**_DEFAULTS[k], **(block.get(k, {}) or {})}
    return merged


def _enrich_baseline(root: Path, baseline: dict) -> dict:
    """Best-effort pull of discovery candidates + crowd context for experiments."""
    root = Path(root)

    def _read(p: Path):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    # Discovery promotion candidates (sandbox).
    promo = _read(root / "outputs" / "sandbox" / "discovery" / "automatic_promotion_candidates.json")
    cands: list[dict] = []
    rows = (promo or {}).get("candidates", []) if isinstance(promo, dict) else []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        cands.append({
            "symbol": r.get("ticker") or r.get("symbol"),
            "score": r.get("corroboration_score", r.get("evidence_score", 0.0)),
            "reason": "Discovery promotion-governance candidate",
            "tags": r.get("catalyst_flags", []),
            "evidence": ["outputs/sandbox/discovery/automatic_promotion_candidates.json"],
            "risk_impact": "medium" if r.get("risk_flags") else "low",
            "data_quality": "ok",
        })
    baseline["discovery_candidates"] = [c for c in cands if c.get("symbol")]
    baseline.setdefault("crowd", {})
    baseline.setdefault("watchlist_ranked", [])
    return baseline


def run_daily_governance(
    root: Path | str,
    now: str | None = None,
    *,
    config: dict | None = None,
    reviewer: Callable[[dict], list[dict]] | None = None,
    write_files: bool = True,
) -> dict:
    """Run the full daily simulation-governance lane. Never raises."""
    root = Path(root)
    now = now or datetime.now(timezone.utc).isoformat()
    base_dir = str(root / "outputs")
    cfg = config or load_sim_governance_config(root)

    status: dict = {
        "generated_at": now,
        "schema": "daily_governance_status.v1",
        "enabled": bool(cfg.get("enabled", True)),
        "simulation_lane_active": bool(cfg.get("simulation_lane", {}).get("enabled", True)),
        "stages": {},
    }

    if not cfg.get("enabled", True):
        status["note"] = "sim_governance disabled in config"
        if write_files:
            _safe_status(status, base_dir)
        return status

    # ── Step 1: Flock Intelligence producer (writes simulation artifacts that
    #            the lane's baseline then consumes). Non-blocking. ────────────
    try:
        from portfolio_automation.flock_intelligence.producer import run_flock_intelligence
        flock = run_flock_intelligence(root, now, base_dir=base_dir, write_files=write_files)
        rpt = flock.get("report", {})
        status["stages"]["flock_intelligence"] = {
            "ok": True, "group_count": rpt.get("group_count", 0),
            "data_quality": rpt.get("data_quality_status", "unknown"),
            "watchlist_candidate_count": len(
                (flock.get("watchlist_candidates") or {}).get("candidates", [])),
        }
    except Exception as exc:
        logger.warning("daily_governance: flock_intelligence failed: %s", exc)
        status["stages"]["flock_intelligence"] = {"ok": False, "error": str(exc)}

    # ── Step 2: active simulation lane (after production baseline exists) ────
    try:
        baseline = _enrich_baseline(root, simulation_lane.load_production_baseline(root))
        lane = simulation_lane.run_simulation_lane(
            root, now, baseline=baseline, write_files=write_files, base_dir=base_dir)
        status["stages"]["simulation_lane"] = {
            "ok": True, "candidate_count": lane.get("candidate_count", 0),
            "watchlist_changed": bool(set(lane.get("simulated_watchlist", []))
                                      != set(baseline.get("watchlist", []))),
        }
    except Exception as exc:
        logger.warning("daily_governance: simulation lane failed: %s", exc)
        status["stages"]["simulation_lane"] = {"ok": False, "error": str(exc)}
        if write_files:
            _safe_status(status, base_dir)
        return status

    # ── Step 3: daily simulation bundle ─────────────────────────────────────
    try:
        bundle = daily_simulation_bundle.build_daily_simulation_bundle(
            lane, now, base_dir=base_dir, write_files=write_files)
        status["stages"]["bundle"] = {"ok": True, "candidate_count": bundle.get("candidate_count", 0)}
    except Exception as exc:
        logger.warning("daily_governance: bundle failed: %s", exc)
        status["stages"]["bundle"] = {"ok": False, "error": str(exc)}
        bundle = {}

    # ── Step 4: consolidated review packet ──────────────────────────────────
    try:
        packet = ai_review_packet.build_review_packet(bundle, now)
        if write_files:
            packet = ai_review_packet.write_review_packet(packet, base_dir=base_dir)
        status["stages"]["packet"] = {"ok": True, "candidate_count": packet.get("candidate_count", 0)}
    except Exception as exc:
        logger.warning("daily_governance: packet failed: %s", exc)
        status["stages"]["packet"] = {"ok": False, "error": str(exc)}
        packet = {}

    # ── Step 5: gated AI/product review (<= $0.50/day) ──────────────────────
    ai_cfg = cfg.get("ai_review", {})
    review: dict = {"status": "skipped", "verdicts": []}
    if ai_cfg.get("enabled", True) and packet:
        try:
            review = daily_ai_review.run_daily_ai_review(
                packet, now, base_dir=base_dir,
                daily_cost_cap_usd=float(ai_cfg.get("daily_cost_cap_usd", 0.50)),
                provider=ai_cfg.get("provider", "openai"),
                model=ai_cfg.get("model", "gpt-4o-mini"),
                reviewer=reviewer, write_files=write_files,
            )
            status["stages"]["ai_review"] = {
                "ok": True, "status": review.get("status"),
                "estimated_cost_usd": review.get("estimated_cost_usd"),
                "ready": (review.get("counts", {}) or {}).get("ready_for_production_review", 0),
            }
        except Exception as exc:
            logger.warning("daily_governance: ai review failed: %s", exc)
            status["stages"]["ai_review"] = {"ok": False, "error": str(exc)}
    else:
        status["stages"]["ai_review"] = {"ok": True, "status": "disabled"}

    # ── Step 6: pending proposals for READY candidates ──────────────────────
    try:
        cbi = {c["candidate_id"]: c for c in lane.get("candidates", [])}
        pend = promotion_proposals.generate_proposals(
            cbi, review, now, base_dir=base_dir, write_files=write_files)
        status["stages"]["proposals"] = {"ok": True, "pending_count": pend.get("pending_count", 0)}
    except Exception as exc:
        logger.warning("daily_governance: proposals failed: %s", exc)
        status["stages"]["proposals"] = {"ok": False, "error": str(exc)}

    # ── Step 7: apply already human-approved proposals to production overlays ─
    try:
        app_state = production_application.apply_approved_proposals(
            now, base_dir=base_dir, write_files=write_files)
        status["stages"]["production_application"] = {
            "ok": True, "applied_count": app_state.get("applied_count", 0),
            "ignored_count": app_state.get("ignored_count", 0),
        }
    except Exception as exc:
        logger.warning("daily_governance: application failed: %s", exc)
        status["stages"]["production_application"] = {"ok": False, "error": str(exc)}

    # ── roll-up counts for the GUI / daily check ────────────────────────────
    try:
        status["approved_proposal_count"] = len(promotion_approvals.approved_proposal_ids(base_dir))
        status["rejected_proposal_count"] = len(promotion_approvals.rejected_proposal_ids(base_dir))
        status["pending_proposal_count"] = len(promotion_proposals.load_pending_proposals(base_dir))
        prod_cfg = cfg.get("production_application", {})
        status["production_overlay_live"] = {
            "watchlist": bool(prod_cfg.get("apply_watchlist_overlay", False)),
            "advisory": bool(prod_cfg.get("apply_advisory_overlay", False)),
        }
    except Exception:
        pass

    if write_files:
        _safe_status(status, base_dir)
    return status


def _safe_status(status: dict, base_dir: str) -> None:
    try:
        safe_write_json(OutputNamespace.PROMOTION_REVIEW, _STATUS_FILE, status, base_dir=base_dir)
    except Exception as exc:
        logger.debug("daily_governance: status write failed: %s", exc)


# Convenience entry point for the pipeline stage.
def run(root: Path | str = ".") -> dict:
    return run_daily_governance(root)
