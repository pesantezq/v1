"""
Active-strategy vs. top-ranked-tactic divergence — observe-only, sandbox-scoped.

WS5 (see .superpowers/audit/ws-04-05-14-18-health.md) found that a persistent gap
between the human-approved ``active_strategy_selection.json`` and the Strategy Lab
leaderboard's #1-ranked tactic is recorded NOWHERE: no artifact compares them, no
health check gates on the gap, and the operator has no way to learn *why* the
active strategy hasn't moved, whether the top tactic even carries OOS support, or
whether promoting it is currently possible at all.

This module is a pure, read-only PRODUCER: it never writes ``decision_plan.json``,
never touches ``config.json``/``signal_registry.yaml``, never calls
``record_strategy_decision``/``record_auto_strategy_anchor``, and never changes the
active strategy. It only reads existing sandbox/policy artifacts and emits one new
artifact, ``strategy_divergence.json`` (``OutputNamespace.SANDBOX``), that makes the
divergence -- and the evidence behind it -- explicit and inspectable.

Classification (exactly one, fail-closed toward the least flattering label when
evidence is ambiguous -- this module does NOT tune toward a more comfortable
verdict):

    EXPECTED_POLICY_DIVERGENCE  -- ranks agree (no real divergence), OR the top
                                   tactic is OOS_FAILED (retaining the active
                                   strategy is the correct call), OR an explicit
                                   recorded policy reason justifies the gap.
    STALE_ACTIVE_STRATEGY       -- the active_strategy_id no longer appears in the
                                   current review queue (mirrors
                                   strategy_lab_health.check_active_strategy_selection).
    INSUFFICIENT_EVIDENCE       -- the top-ranked tactic has not reached
                                   OOS_SUPPORTED (untested / data-blocked /
                                   insufficient folds / mixed) -- there isn't yet
                                   enough evidence to say the divergence SHOULD be
                                   resolved, only that it exists.
    PENDING_REVIEW              -- the top tactic IS OOS_SUPPORTED, is a member of
                                   the review-queue candidate universe, and a human
                                   decision on it is outstanding.
    UNEXPLAINED_DIVERGENCE      -- the top tactic IS OOS_SUPPORTED and nothing above
                                   explains why the active strategy hasn't moved --
                                   this is the "someone should look at this" label.

Today's real repo state (2026-07-28) resolves to INSUFFICIENT_EVIDENCE: the
top-ranked tactic (research_vol_managed / "Volatility-Managed") has never been
walk-forward tested (OOS_NOT_TESTED, like 25/26 leaderboard tactics -- see
oos_state.py). That is the honest answer; it is not tuned to look better.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.portfolio_sim.oos_state import OOSState, build_oos_evidence
from portfolio_automation.strategy.strategy_selection import resolve_anchor_tactic_id

_ARTIFACT_FILE = "strategy_divergence.json"

CLASSIFICATIONS = (
    "EXPECTED_POLICY_DIVERGENCE",
    "PENDING_REVIEW",
    "INSUFFICIENT_EVIDENCE",
    "STALE_ACTIVE_STRATEGY",
    "UNEXPLAINED_DIVERGENCE",
)

_DISCLAIMER = (
    "Sandbox observe-only artifact. Records a comparison between the operator-"
    "approved active strategy and the current Strategy Lab leaderboard; never a "
    "trade recommendation. Never feeds decision_plan.json or the production "
    "decision engine, and never changes the active strategy."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _degraded(reason: str, **extra: Any) -> dict[str, Any]:
    """Uniform degraded-state shape when required inputs are absent/unparsable --
    matches the observability-template convention: never fabricate a comparison
    from missing data, say plainly why one wasn't produced."""
    return {
        "schema_version": "1",
        "generated_at": _now_iso(),
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "artifact_only": True,
        "source": "strategy_divergence",
        "status": "degraded",
        "reason": reason,
        "disclaimer": _DISCLAIMER,
        **extra,
    }


def classify_divergence(
    *,
    rank_difference: int | None,
    active_in_queue: bool,
    top_tactic_oos_state: str,
    top_tactic_in_queue: bool,
    has_pending_promotion_proposal: bool,
    explicit_policy_reason: str | None,
) -> tuple[str, list[str]]:
    """Pure decision function -- exactly one of CLASSIFICATIONS, plus reasons.

    Precedence (checked in this order; the FIRST match wins):
      1. active selection is stale (not in the current review queue) -- fix that
         before any ranking comparison is even meaningful.
      2. ranks agree (rank_difference == 0) -- nothing to explain.
      3. an explicit recorded policy reason exists -- the divergence is deliberate
         and documented.
      4. the top tactic is OOS_FAILED -- retaining the active strategy is the
         evidence-backed call.
      5. the top tactic has not reached OOS_SUPPORTED -- not enough evidence to
         judge the divergence either way (this is the fail-closed default when
         evidence is ambiguous, per WS5/WS2/WS3 -- do NOT skip this to reach a
         more flattering label).
      6. the top tactic IS OOS_SUPPORTED but isn't in the review-queue candidate
         universe -- structurally can't be promoted through the existing decide
         route; nothing "pending" can be true of a candidate that can't be
         submitted, so this is unexplained (attention-worthy), not pending.
      7. the top tactic IS OOS_SUPPORTED, IS in the queue, and a human decision is
         outstanding -- pending review.
      8. otherwise -- genuinely unexplained: validated evidence exists, promotion
         is possible, and nothing accounts for the active strategy not moving.
    """
    reasons: list[str] = []

    if not active_in_queue:
        reasons.append(
            "stale_active_strategy_selection: the active strategy no longer "
            "appears in the current review queue")
        return "STALE_ACTIVE_STRATEGY", reasons

    if rank_difference == 0:
        reasons.append("active strategy is already the top-ranked tactic; no divergence")
        return "EXPECTED_POLICY_DIVERGENCE", reasons

    if explicit_policy_reason:
        reasons.append(f"explicit recorded policy reason: {explicit_policy_reason}")
        return "EXPECTED_POLICY_DIVERGENCE", reasons

    if top_tactic_oos_state == OOSState.OOS_FAILED.value:
        reasons.append(
            "top-ranked tactic failed OOS validation (OOS_FAILED); retaining the "
            "active strategy is justified by the evidence")
        return "EXPECTED_POLICY_DIVERGENCE", reasons

    if top_tactic_oos_state != OOSState.OOS_SUPPORTED.value:
        reasons.append(
            f"top-ranked tactic has not reached OOS_SUPPORTED (state={top_tactic_oos_state}); "
            "there is not yet enough evidence to judge whether the divergence should "
            "be resolved (see oos_state.py) -- 'ranked #1' is not the same as 'validated'")
        return "INSUFFICIENT_EVIDENCE", reasons

    # From here: top_tactic_oos_state == OOS_SUPPORTED.
    if not top_tactic_in_queue:
        reasons.append(
            "top-ranked tactic is OOS_SUPPORTED but is not a member of the "
            "review-queue candidate universe -- it cannot currently be promoted "
            "through the existing human decide-route (structural unpromotability); "
            "the divergence cannot be explained as 'pending' a decision that "
            "cannot be submitted")
        return "UNEXPLAINED_DIVERGENCE", reasons

    if has_pending_promotion_proposal:
        reasons.append("top-ranked tactic is OOS_SUPPORTED, promotable, and a human "
                        "decision on it is outstanding")
        return "PENDING_REVIEW", reasons

    reasons.append(
        "top-ranked tactic is OOS_SUPPORTED and promotable via the existing queue, "
        "yet nothing explains why the active strategy has not been re-reviewed")
    return "UNEXPLAINED_DIVERGENCE", reasons


def compute_strategy_divergence(root: str | Path = ".", now: datetime | None = None) -> dict[str, Any]:
    """Read-only comparison of the active strategy vs. the Strategy Lab leaderboard.

    Reads (never writes): outputs/policy/active_strategy_selection.json,
    outputs/policy/strategy_decisions.jsonl, outputs/sandbox/strategy_leaderboard.json,
    outputs/sandbox/walk_forward_results.json, outputs/latest/strategy_review_queue.json.

    Returns a degraded-status dict (never raises) if the leaderboard or active
    selection cannot be read -- there is nothing to compare without them.
    """
    root = Path(root)
    outputs = root / "outputs"
    now = now or datetime.now(timezone.utc)

    sel = _load_json(outputs / "policy" / "active_strategy_selection.json") or {}
    active_id = sel.get("active_strategy_id")

    lb = _load_json(outputs / "sandbox" / "strategy_leaderboard.json")
    if lb is None:
        return _degraded("strategy_leaderboard.json absent/unparsable -- nothing to compare",
                          active_strategy_id=active_id)
    rows = lb.get("leaderboard") or []
    if not rows:
        return _degraded("strategy_leaderboard.json present but empty (looks_fresh_but_empty)",
                          active_strategy_id=active_id)

    if not active_id:
        return _degraded("no active strategy selection recorded (active_strategy_id is null/absent)")

    wf = _load_json(outputs / "sandbox" / "walk_forward_results.json") or {}
    wf_results = wf.get("results") or {}

    queue = _load_json(outputs / "latest" / "strategy_review_queue.json") or {}
    queue_rows = queue.get("queue") or []
    queue_ids = {r.get("strategy_id") for r in queue_rows if r.get("strategy_id")}
    active_in_queue = active_id in queue_ids

    decisions_path = outputs / "policy" / "strategy_decisions.jsonl"
    last_decision: dict[str, Any] | None = None
    try:
        lines = [ln for ln in decisions_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            last_decision = json.loads(lines[-1])
    except Exception:
        last_decision = None

    top = rows[0]
    top_tactic_id = top.get("tactic_id")
    top_rank = 1
    top_score = top.get("strategy_score")

    all_tactic_ids = {r.get("tactic_id") for r in rows if r.get("tactic_id")}
    active_tactic_id = resolve_anchor_tactic_id(active_id, all_tactic_ids)

    active_row = None
    active_rank = None
    if active_tactic_id:
        for i, r in enumerate(rows):
            if r.get("tactic_id") == active_tactic_id:
                active_row = r
                active_rank = i + 1
                break

    rank_difference = (active_rank - top_rank) if active_rank is not None else None
    active_score = active_row.get("strategy_score") if active_row else None
    score_difference = (
        round(top_score - active_score, 6)
        if isinstance(top_score, (int, float)) and isinstance(active_score, (int, float))
        else None
    )

    top_oos = build_oos_evidence(top_tactic_id, wf_results.get(top_tactic_id))
    top_oos_state = top_oos["state"]

    # Ranking confidence: qualitative, driven by the same evidence gaps WS4 surfaced
    # (never fabricated as a number -- there is no calibrated confidence model here).
    conf_reasons: list[str] = []
    if top_oos_state != OOSState.OOS_SUPPORTED.value:
        conf_reasons.append(f"top tactic OOS state is {top_oos_state}, not OOS_SUPPORTED")
    if "overfit_unknown" in (top.get("flags") or []):
        conf_reasons.append("top tactic carries flags=['overfit_unknown'] (overfit component missing)")
    tested_count = sum(
        1 for r in rows
        if build_oos_evidence(r.get("tactic_id"), wf_results.get(r.get("tactic_id")))["state"]
        == OOSState.OOS_SUPPORTED.value
    )
    conf_reasons.append(f"{tested_count}/{len(rows)} leaderboard tactics reach OOS_SUPPORTED")
    ranking_confidence = {
        "level": "low" if conf_reasons[:-1] else "moderate",
        "reasons": conf_reasons,
    }

    def _component(row: dict | None, name: str) -> Any:
        if not row:
            return None
        comp = ((row.get("score_decomposition") or {}).get("components") or {}).get(name) or {}
        return comp.get("raw")

    turnover_impact = {
        "active_turnover": _component(active_row, "turnover"),
        "top_turnover": _component(top, "turnover"),
        "note": "turnover component raw value from score_decomposition (lower_better); "
                "no live rebalance-cost simulation is run by this comparison",
    }
    tax_impact = {
        "active_tax_drag": _component(active_row, "tax_drag"),
        "top_tax_drag": _component(top, "tax_drag"),
        "note": "tax_drag component raw value from score_decomposition; both tactics carry "
                "tax_note='gross_until_cost_model' (pre-tax-cost-model, per WS2 audit) unless "
                "stated otherwise on the row",
    }
    active_dd = active_row.get("worst_max_drawdown") if active_row else None
    top_dd = top.get("worst_max_drawdown")
    drawdown_comparison = {
        "active_worst_max_drawdown": active_dd,
        "top_worst_max_drawdown": top_dd,
        "delta": (
            round(top_dd - active_dd, 6)
            if isinstance(top_dd, (int, float)) and isinstance(active_dd, (int, float))
            else None
        ),
    }

    # No regime dimension is attached to leaderboard rows anywhere in the Strategy
    # Lab today (confirmed absent, not merely unread) -- report that honestly
    # rather than fabricate a suitability judgement.
    regime_suitability = {
        "status": "not_computed",
        "reason": "strategy_leaderboard.json rows carry no regime dimension; "
                  "outputs/regime/regime_performance.json is a separate, unjoined "
                  "artifact (see WS14) and is not cross-referenced by the Strategy Lab",
    }

    top_tactic_in_queue = top_tactic_id in {
        resolve_anchor_tactic_id(sid, all_tactic_ids) for sid in queue_ids
    } or top_tactic_id in queue_ids

    # No promotion-proposal mechanism exists today for research/shadow tactics
    # outside the 8 fixed SEED_PROFILES (confirmed by the WS5 audit) -- so a
    # "pending" proposal can only be true for a queue-member tactic that has not
    # yet been decided on by a human.
    decided_strategy_ids: set[str] = set()
    try:
        for ln in decisions_path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                rec = json.loads(ln)
                sid = rec.get("strategy_id")
                if sid:
                    decided_strategy_ids.add(sid)
    except Exception:
        pass
    has_pending_promotion_proposal = (
        top_tactic_in_queue and top_tactic_id not in decided_strategy_ids
    )

    explicit_policy_reason = sel.get("policy_reason")  # not populated by any writer today

    classification, classification_reasons = classify_divergence(
        rank_difference=rank_difference,
        active_in_queue=active_in_queue,
        top_tactic_oos_state=top_oos_state,
        top_tactic_in_queue=top_tactic_in_queue,
        has_pending_promotion_proposal=has_pending_promotion_proposal,
        explicit_policy_reason=explicit_policy_reason,
    )

    should_consider_promotion = classification in ("UNEXPLAINED_DIVERGENCE", "PENDING_REVIEW")
    if classification == "INSUFFICIENT_EVIDENCE":
        promotion_rationale = (
            "Not yet -- a promotion proposal would be premature. The top-ranked "
            f"tactic's OOS state is {top_oos_state}; extend walk-forward testing to "
            "it before considering promotion (see run_strategy_lab.py's "
            "_walk_forward_results, which today only tests one hardcoded tactic key)."
        )
    elif classification == "STALE_ACTIVE_STRATEGY":
        promotion_rationale = (
            "Resolve the stale active-strategy selection first (re-approve a current "
            "review-queue profile); a promotion comparison against a stale anchor "
            "isn't meaningful."
        )
    elif should_consider_promotion:
        promotion_rationale = (
            "Yes -- the top-ranked tactic carries OOS_SUPPORTED evidence and nothing "
            "explains the active strategy's persistence; route to human review."
        )
    else:
        promotion_rationale = (
            "No -- the divergence is explained (ranks agree, an explicit policy "
            "reason exists, or the top tactic failed OOS validation)."
        )

    structural_unpromotability = {
        "blocked": not top_tactic_in_queue,
        "reason": (
            f"'{top.get('name')}' (tactic_id={top_tactic_id}) is a Strategy-Lab "
            "research/shadow tactic, not a member of the 8 fixed SEED_PROFILES in "
            "strategy_review_queue.json. A human cannot approve it as the active "
            "strategy via the existing GUI decide-route "
            "(POST /dashboard/strategy-lab/decide) without first widening the "
            "review queue's candidate set to include Strategy-Lab research tactics."
            if not top_tactic_in_queue
            else f"'{top.get('name')}' is a member of the review-queue candidate "
                 "universe and can be approved via the existing decide-route."
        ),
        "review_queue_profiles": sorted(queue_ids),
    }

    return {
        "schema_version": "1",
        "generated_at": now.isoformat(),
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "artifact_only": True,
        "source": "strategy_divergence",
        "status": "ok",
        "disclaimer": _DISCLAIMER,
        "active_strategy": {
            "strategy_id": active_id,
            "tactic_id": active_tactic_id,
            "name": sel.get("name"),
            "rank": active_rank,
            "score": active_score,
        },
        "top_ranked_tactic": {
            "tactic_id": top_tactic_id,
            "name": top.get("name"),
            "rank": top_rank,
            "score": top_score,
        },
        "rank_difference": rank_difference,
        "score_difference": score_difference,
        "ranking_confidence": ranking_confidence,
        "top_tactic_oos": top_oos,
        "regime_suitability": regime_suitability,
        "turnover_impact": turnover_impact,
        "tax_impact": tax_impact,
        "drawdown_comparison": drawdown_comparison,
        "reason_active_unchanged": classification_reasons[0] if classification_reasons else None,
        "promotion_consideration": {
            "should_consider": should_consider_promotion,
            "rationale": promotion_rationale,
        },
        "structural_unpromotability": structural_unpromotability,
        "last_human_decision": (
            {
                "ts": last_decision.get("ts"),
                "strategy_id": last_decision.get("strategy_id"),
                "decision": last_decision.get("decision"),
                "approver": last_decision.get("approver"),
                "source": "outputs/policy/strategy_decisions.jsonl",
            }
            if last_decision
            else None
        ),
        "classification": classification,
        "classification_reasons": classification_reasons,
    }


def write_strategy_divergence(
    root: str | Path = ".",
    now: datetime | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Compute + persist the divergence artifact to
    ``outputs/sandbox/strategy_divergence.json`` (``OutputNamespace.SANDBOX``).

    ``base_dir`` defaults to ``<root>/outputs`` (the convention ``get_output_path``
    expects); pass it explicitly to redirect writes away from *root* (e.g. tests).
    Never raises -- callers wrap pipeline integration in try/except per the repo's
    non-blocking convention regardless.
    """
    root = Path(root)
    result = compute_strategy_divergence(root=root, now=now)
    out_base = Path(base_dir) if base_dir is not None else root / "outputs"
    safe_write_json(OutputNamespace.SANDBOX, _ARTIFACT_FILE, result, base_dir=out_base)
    return result
