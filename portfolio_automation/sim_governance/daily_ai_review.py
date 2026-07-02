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
import os
from pathlib import Path
from typing import Callable

from portfolio_automation import ai_budget
from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.env import get_secret
from portfolio_automation.sim_governance import schemas as S
from portfolio_automation.sim_governance.ai_review_packet import (
    estimate_packet_tokens,
    render_packet_md,
)

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
# LLM-backed reviewer (optional; operator-approved 2026-07-02)
#
# When wired in, real model judgment classifies each candidate. It can ONLY
# recommend readiness for HUMAN review — it never approves a production change
# (required_human_review is forced True). On ANY failure (API unreachable,
# unparseable output) it degrades gracefully to the deterministic
# heuristic_reviewer so the daily governance run never loses its verdicts —
# never worse than the free steady state.
# ---------------------------------------------------------------------------

_LLM_REVIEW_INSTRUCTION = (
    "You are a conservative product/risk reviewer for an ADVISORY-ONLY portfolio "
    "system. You review simulation-lane promotion candidates. You may recommend "
    "readiness for HUMAN review only; you can NEVER approve a production change. "
    "For EACH candidate, classify `decision` as EXACTLY one of: "
    f"'{S.DECISION_READY}' (clean, high-confidence, low/medium risk, sim-ready), "
    f"'{S.DECISION_REJECT}' (confidence too low or a disqualifying risk), or "
    f"'{S.DECISION_CONTINUE_TESTING}' (promising but not decisive). "
    "Be conservative: when in doubt use continue_testing. "
    "Return ONLY a JSON array — one object per candidate — with keys: "
    "candidate_id, decision, reason, evidence_strength (weak|moderate|strong), "
    "risk_level (low|medium|high|unknown), missing_evidence (array of strings), "
    "rollback_readiness (ready|partial|none|unknown). No prose outside the JSON. "
    "Emit one object for EVERY candidate and keep each `reason` under 12 words."
)

_DECISION_SYNONYMS = {
    "ready": S.DECISION_READY,
    "ready_for_production": S.DECISION_READY,
    "promote": S.DECISION_READY,
    "recommend": S.DECISION_READY,
    "approve": S.DECISION_READY,
    "reject": S.DECISION_REJECT,
    "rejected": S.DECISION_REJECT,
    "drop": S.DECISION_REJECT,
    "discard": S.DECISION_REJECT,
    "continue": S.DECISION_CONTINUE_TESTING,
    "hold": S.DECISION_CONTINUE_TESTING,
    "wait": S.DECISION_CONTINUE_TESTING,
}


def _call_llm(*, provider: str, model: str, prompt: str,
              max_tokens: int = 900, timeout: int = 90) -> str:
    """Thin indirection over the shared LLM adapter (patch point for tests)."""
    from agent.llm_adapters import call_provider
    return call_provider(provider=provider, model=model, prompt=prompt,
                          max_tokens=max_tokens, timeout=timeout)


def _coerce_decision(raw: object) -> str:
    """Map a model-emitted decision string onto the allowed REVIEW_DECISIONS."""
    r = str(raw or "").strip().lower()
    if r in S.REVIEW_DECISIONS:
        return r
    if r in _DECISION_SYNONYMS:
        return _DECISION_SYNONYMS[r]
    return S.DECISION_CONTINUE_TESTING  # conservative default for anything unknown


def _salvage_objects(t: str) -> list[dict]:
    """Extract every complete top-level ``{...}`` object from ``t``.

    Recovers usable verdicts even when the enclosing array is truncated
    mid-object (e.g. the model hit its output-token limit).
    """
    objs: list[dict] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    o = json.loads(t[start : i + 1])
                    if isinstance(o, dict):
                        objs.append(o)
                except Exception:
                    pass
                start = None
    return objs


def _parse_verdict_json(text: str) -> list[dict]:
    """Best-effort parse of the model's reply into a list of verdict dicts."""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):  # strip markdown code fences
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        if "```" in t:
            t = t[: t.index("```")]
        t = t.strip()
    start, end = t.find("["), t.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidate = t[start : end + 1]
    else:
        candidate = t
    try:
        obj = json.loads(candidate)
    except Exception:
        # Truncated / malformed array → salvage whatever complete objects exist.
        return _salvage_objects(t)
    if isinstance(obj, dict):
        obj = obj.get("verdicts") or obj.get("reviews") or []
    return [o for o in obj if isinstance(o, dict)] if isinstance(obj, list) else []


def _verdict_from_model(obj: dict, candidate: dict) -> dict:
    return S.ReviewVerdict(
        candidate_id=candidate.get("candidate_id"),
        workflow=candidate.get("workflow"),
        decision=_coerce_decision(obj.get("decision")),
        reason=str(obj.get("reason") or "")[:600],
        evidence_strength=str(obj.get("evidence_strength") or "unknown"),
        risk_level=str(obj.get("risk_level") or candidate.get("risk_impact") or "unknown"),
        missing_evidence=[str(m) for m in (obj.get("missing_evidence") or [])],
        required_human_review=True,  # AI can NEVER self-approve — hard invariant.
        rollback_readiness=str(
            obj.get("rollback_readiness")
            or ("ready" if candidate.get("workflow") in S.WORKFLOWS else "unknown")
        ),
    ).to_dict()


def make_openai_reviewer(
    *,
    provider: str = "openai",
    model: str = "gpt-4o-mini",
    max_tokens: int | None = None,
    timeout: int = 90,
    fallback: Reviewer = heuristic_reviewer,
) -> Reviewer:
    """Build a `Reviewer` that classifies candidates with a real model call.

    Degrades to `fallback` (the free heuristic) on any error so verdicts are
    never lost; fallback verdicts are tagged in their `reason` for transparency.
    ``max_tokens`` defaults to a budget that scales with the candidate count
    (each verdict object is ~90 tokens) so a large packet's JSON is not
    truncated mid-array.
    """

    def _reviewer(packet: dict) -> list[dict]:
        candidates = ((packet.get("advisory_candidates") or [])
                      + (packet.get("watchlist_candidates") or []))
        if not candidates:
            return []
        by_id = {c.get("candidate_id"): c for c in candidates}
        mt = max_tokens or min(12000, 600 + 120 * len(candidates))
        prompt = f"{_LLM_REVIEW_INSTRUCTION}\n\n{render_packet_md(packet)}\n\nReturn the JSON array now."
        try:
            text = _call_llm(provider=provider, model=model, prompt=prompt,
                             max_tokens=mt, timeout=timeout)
            parsed = _parse_verdict_json(text)
            if not parsed:
                raise ValueError("model returned no parseable verdicts")
        except Exception as exc:  # graceful degrade — never lose the run's verdicts
            logger.warning("daily_ai_review: OpenAI reviewer failed (%s); using heuristic", exc)
            degraded = fallback(packet)
            for v in degraded:
                v["reason"] = "[llm-fallback:heuristic] " + v.get("reason", "")
            return degraded

        verdicts: list[dict] = []
        seen: set = set()
        for obj in parsed:
            cand = by_id.get(obj.get("candidate_id"))
            if cand is None:
                continue
            verdicts.append(_verdict_from_model(obj, cand))
            seen.add(cand.get("candidate_id"))

        # Any candidate the model skipped is still covered — conservatively, via
        # the heuristic — so no candidate silently drops out of the review.
        if len(seen) < len(candidates):
            heur = {v["candidate_id"]: v for v in fallback(packet)}
            for c in candidates:
                cid = c.get("candidate_id")
                if cid not in seen and cid in heur:
                    v = heur[cid]
                    v["reason"] = "[llm-omitted:heuristic] " + v.get("reason", "")
                    verdicts.append(v)
        return verdicts

    return _reviewer


def build_configured_reviewer(ai_cfg: dict) -> Reviewer | None:
    """Return an LLM reviewer when config + environment allow it, else None.

    Gate (all must hold): ``ai_review.llm_enabled`` is true, the
    ``STOCKBOT_SIM_GOV_LLM_DISABLED`` kill-switch is not set, and an
    ``OPENAI_API_KEY`` is resolvable. When the gate fails we return None so the
    caller keeps the free heuristic fallback and ``review_method`` stays honest.
    """
    if not ai_cfg.get("llm_enabled", False):
        return None
    if os.environ.get("STOCKBOT_SIM_GOV_LLM_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        logger.info("daily_ai_review: LLM reviewer disabled by STOCKBOT_SIM_GOV_LLM_DISABLED")
        return None
    if not (get_secret("OPENAI_API_KEY") or "").strip():
        logger.info("daily_ai_review: llm_enabled but no OPENAI_API_KEY resolvable; using heuristic")
        return None
    return make_openai_reviewer(
        provider=ai_cfg.get("provider", "openai"),
        model=ai_cfg.get("model", "gpt-4o-mini"),
    )


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
