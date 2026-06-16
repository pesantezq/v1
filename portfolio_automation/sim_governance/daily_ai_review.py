"""
Daily consolidated AI/Product review (spec §3 Step 5, §4).

Exactly ONE consolidated review per day covering BOTH advisory and watchlist
candidates. The cost of the call is *estimated before* it is made:

  * estimated cost <= daily cap  -> run the single consolidated review
  * estimated cost  > daily cap  -> SKIP the call, write a deferred-review artifact

A once-per-day guard prevents a second call on the same date. The review
classifies each candidate as reject / continue_testing / ready_for_production_review
and can only *recommend* readiness — it never approves production.

Writes:
  * outputs/promotion_review/daily_ai_review_result.json   (verdicts)
  * outputs/promotion_review/daily_ai_review_deferred.json  (only when skipped)
And records an AI usage event so spend shows in the AI budget summary + GUI.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from portfolio_automation import ai_budget
from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.sim_governance import schemas as S
from portfolio_automation.sim_governance.ai_review_packet import estimate_packet_tokens

logger = logging.getLogger("stockbot.sim_governance.daily_ai_review")

_RESULT_FILE = "daily_ai_review_result.json"
_DEFERRED_FILE = "daily_ai_review_deferred.json"

# A reviewer maps a packet -> list of verdict dicts (one per candidate).
Reviewer = Callable[[dict], list[dict]]


# ---------------------------------------------------------------------------
# Default (free, deterministic) reviewer — heuristic stand-in for the LLM.
# Plug an LLM-backed reviewer in via the `reviewer=` argument to use a model.
# ---------------------------------------------------------------------------


def heuristic_reviewer(packet: dict) -> list[dict]:
    """Deterministic classifier over advisory + watchlist candidates together.

    Conservative by construction: only clean, high-confidence, low/medium-risk
    candidates the simulation already flagged ready are recommended for
    production review. Everything else keeps testing or is rejected.
    """
    verdicts: list[dict] = []
    candidates = (packet.get("advisory_candidates") or []) + (packet.get("watchlist_candidates") or [])
    for c in candidates:
        conf = float(c.get("confidence", 0.0) or 0.0)
        dq = str(c.get("data_quality", "unknown"))
        risk = str(c.get("risk_impact", "unknown"))
        ready_hint = bool(c.get("sim_ready_hint"))
        missing: list[str] = []
        if dq not in ("ok",):
            missing.append(f"data_quality={dq}")
        if risk == "unknown":
            missing.append("risk_impact unspecified")

        if conf < 0.30:
            decision = S.DECISION_REJECT
            reason = f"Confidence {conf:.2f} below the action threshold."
        elif ready_hint and conf >= 0.80 and dq == "ok" and risk in ("low", "medium"):
            decision = S.DECISION_READY
            reason = (f"Clean evidence (conf {conf:.2f}, data {dq}, risk {risk}); "
                      "recommend human production review.")
        else:
            decision = S.DECISION_CONTINUE_TESTING
            reason = "Promising but not yet decisive; keep accumulating evidence."

        verdicts.append(S.ReviewVerdict(
            candidate_id=c.get("candidate_id"),
            workflow=c.get("workflow"),
            decision=decision,
            reason=reason,
            evidence_strength=("strong" if conf >= 0.8 else "moderate" if conf >= 0.5 else "weak"),
            risk_level=risk,
            missing_evidence=missing,
            required_human_review=True,
            rollback_readiness=("ready" if c.get("workflow") in S.WORKFLOWS else "unknown"),
        ).to_dict())
    return verdicts


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_existing_result(base_dir: str) -> dict | None:
    path = get_output_path(OutputNamespace.PROMOTION_REVIEW, _RESULT_FILE, base_dir=base_dir)
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _date_of(now: str) -> str:
    return (now or "")[:10]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_daily_ai_review(
    packet: dict,
    now: str,
    *,
    base_dir: str,
    daily_cost_cap_usd: float = 0.50,
    provider: str = "openai",
    model: str = "gpt-4o-mini",
    estimated_completion_tokens: int = 600,
    reviewer: Reviewer | None = None,
    force: bool = False,
    write_files: bool = True,
) -> dict:
    """Run (or defer) the single daily consolidated AI/product review.

    Args:
        packet: the review packet from ai_review_packet.build_review_packet.
        now: ISO timestamp (caller-supplied; its date gates one-call-per-day).
        daily_cost_cap_usd: hard cap; estimated cost above this skips the call.
        reviewer: optional injected reviewer (tests / LLM). Defaults to the free
            deterministic heuristic_reviewer.
        force: bypass the once-per-day guard (manual re-runs).
    """
    today = _date_of(now)

    # ── Once-per-day guard ──────────────────────────────────────────────────
    if not force:
        existing = _load_existing_result(base_dir)
        if existing and existing.get("review_date") == today and existing.get("status") == "reviewed":
            existing = {**existing, "status": "already_reviewed_today",
                        "note": "A consolidated review already ran today; skipping second call."}
            return existing

    prompt_tokens = int(packet.get("estimated_prompt_tokens") or estimate_packet_tokens(packet))
    estimated_cost = ai_budget.estimate_ai_cost(provider, model, prompt_tokens, estimated_completion_tokens)

    advisory_n = len(packet.get("advisory_candidates") or [])
    watchlist_n = len(packet.get("watchlist_candidates") or [])

    # ── Cost gate: defer if the estimate exceeds the cap ────────────────────
    if estimated_cost > daily_cost_cap_usd:
        deferred = {
            "generated_at": now,
            "review_date": today,
            "status": "deferred",
            "reason": "estimated_cost_exceeds_daily_cap",
            "estimated_cost_usd": round(estimated_cost, 6),
            "daily_cost_cap_usd": daily_cost_cap_usd,
            "provider": provider,
            "model": model,
            "estimated_prompt_tokens": prompt_tokens,
            "advisory_candidates_reviewed": 0,
            "watchlist_candidates_reviewed": 0,
            "covers_workflows": [S.WORKFLOW_ADVISORY, S.WORKFLOW_WATCHLIST],
            "verdicts": [],
        }
        if write_files:
            try:
                safe_write_json(OutputNamespace.PROMOTION_REVIEW, _DEFERRED_FILE, deferred, base_dir=base_dir)
            except Exception as exc:
                logger.warning("daily_ai_review: deferred write failed: %s", exc)
        logger.info("daily_ai_review: DEFERRED — est $%.4f > cap $%.2f", estimated_cost, daily_cost_cap_usd)
        return deferred

    # ── Within budget: run the single consolidated review ───────────────────
    rv = reviewer or heuristic_reviewer
    review_method = "llm" if reviewer is not None else "heuristic_fallback"
    try:
        verdicts = rv(packet) or []
    except Exception as exc:
        logger.warning("daily_ai_review: reviewer failed: %s", exc)
        verdicts = []
        review_method += "_error"

    # Record AI spend (for the budget summary + GUI). Heuristic fallback is free,
    # so its recorded cost is 0; an LLM reviewer records the estimated spend.
    actual_cost = round(estimated_cost, 6) if reviewer is not None else 0.0
    try:
        ai_budget.record_ai_usage_event(
            ai_budget.AIUsageEvent(
                timestamp=now,
                task_name="daily_sim_governance_review",
                provider=provider if reviewer is not None else "local",
                model=model if reviewer is not None else "heuristic",
                prompt_tokens=prompt_tokens if reviewer is not None else 0,
                completion_tokens=estimated_completion_tokens if reviewer is not None else 0,
                total_tokens=(prompt_tokens + estimated_completion_tokens) if reviewer is not None else 0,
                estimated_cost_usd=actual_cost,
                allowed=True,
                metadata={"daily_cost_cap_usd": daily_cost_cap_usd, "review_method": review_method},
            ),
            base_dir=base_dir,
        )
    except Exception as exc:
        logger.debug("daily_ai_review: usage-event record failed: %s", exc)

    ready_ids = [v["candidate_id"] for v in verdicts if v.get("decision") == S.DECISION_READY]
    result = {
        "generated_at": now,
        "review_date": today,
        "status": "reviewed",
        "review_method": review_method,
        "provider": provider,
        "model": model,
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
        "actual_cost_usd": actual_cost,
        "daily_cost_cap_usd": daily_cost_cap_usd,
        "calls_made_today": 1,
        "covers_workflows": [S.WORKFLOW_ADVISORY, S.WORKFLOW_WATCHLIST],
        "advisory_candidates_reviewed": advisory_n,
        "watchlist_candidates_reviewed": watchlist_n,
        "counts": {
            S.DECISION_REJECT: sum(1 for v in verdicts if v.get("decision") == S.DECISION_REJECT),
            S.DECISION_CONTINUE_TESTING: sum(1 for v in verdicts if v.get("decision") == S.DECISION_CONTINUE_TESTING),
            S.DECISION_READY: len(ready_ids),
        },
        "ready_candidate_ids": ready_ids,
        "ai_can_approve_production": False,
        "verdicts": verdicts,
    }
    if write_files:
        try:
            safe_write_json(OutputNamespace.PROMOTION_REVIEW, _RESULT_FILE, result, base_dir=base_dir)
        except Exception as exc:
            logger.warning("daily_ai_review: result write failed: %s", exc)
            result["write_error"] = str(exc)
    logger.info("daily_ai_review: reviewed %d candidates (%d ready) est $%.4f",
                advisory_n + watchlist_n, len(ready_ids), estimated_cost)
    return result
