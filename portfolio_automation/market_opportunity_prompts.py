"""Market-opportunity prompt integration (Phase 8, spec §9).

Connects the existing opportunity radar to the operator review surface WITHOUT
duplicating the theme/LLM layer: it reads ``opportunity_radar.json``, generates
research review cards + research-prompt records (deterministic template, with an
optional LLM summarizer hook), and seeds the opportunity approval queue.

It NEVER writes official recommendations or buy/sell orders. All output is
SANDBOX, observe_only. Appears in the dashboard under the *Market Opportunity*
category, distinct from system-improvement (Type C).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import observe_only_envelope, OpportunityStatus as S

# Statuses worth surfacing to the operator for research/review. Private/access-
# limited items ARE included — they warrant access-route research — while
# REJECTED / HYPE_NOISE / thin DISCOVERED are not.
_REVIEWABLE = {S.QUALIFIED.value, S.APPROVED_WATCHLIST_REVIEW.value,
               S.WATCHING.value, S.SANDBOX_TRACKING.value,
               S.PRIVATE_WATCH_ONLY.value, S.ACCESS_LIMITED.value}
_APPROVE_ACTIONS = ["approve_to_watchlist_review", "reject", "keep_watching",
                    "request_deeper_research", "send_to_sandbox", "add_to_boom_bucket_review"]
_BLOCKED_ACTIONS = ["place_trade", "submit_order", "move_money",
                    "broker_write_action", "modify_real_holdings"]


def _load_json_safe(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _research_question(opp: dict[str, Any]) -> str:
    cand = opp.get("candidate", "?")
    theme = opp.get("theme") or "the detected theme"
    ctype = opp.get("candidate_type", "")
    base = (f"Research the {theme} opportunity around {cand} ({ctype}). "
            f"Assess catalyst durability, fundamental support, valuation, crowding/hype risk, "
            f"liquidity, and portfolio-fit/diversification value. ")
    if opp.get("final_status") == S.PRIVATE_WATCH_ONLY.value:
        base += ("This is a PRIVATE/watch-only candidate — evaluate access routes "
                 "(IPO watch, public suppliers, ETFs, proxies) only; it is not directly tradeable. ")
    base += "Output is research evidence only — never a buy/sell recommendation."
    return base


def build_market_opportunity_outputs(root: Path, now_iso: str,
                                     summarizer: Callable[[str], str] | None = None) -> dict[str, Any]:
    radar = _load_json_safe(root / "outputs" / "sandbox" / "opportunity_radar.json") or {}
    opps = [o for o in radar.get("opportunities", []) or []
            if o.get("final_status") in _REVIEWABLE]

    prompts, cards, queue = [], [], []
    for o in opps:
        cand = o.get("candidate", "")
        q = _research_question(o)
        if summarizer is not None:
            try:
                polished = summarizer(q)
                if isinstance(polished, str) and polished.strip():
                    q = polished
            except Exception:
                pass
        oid = "mo-" + "".join(ch for ch in str(cand).lower() if ch.isalnum())[:40]
        prompts.append({"id": oid, "candidate": cand, "theme": o.get("theme", ""),
                        "status": o.get("final_status"), "prompt_text": q,
                        "evidence": o.get("evidence", []), "observe_only": True})
        cards.append({"id": oid, "candidate": cand, "theme": o.get("theme", ""),
                      "candidate_type": o.get("candidate_type"),
                      "final_status": o.get("final_status"),
                      "opportunity_score": o.get("opportunity_score"),
                      "boom_score": o.get("boom_score"), "risk_score": o.get("risk_score"),
                      "investability_score": o.get("investability_score"),
                      "summary": q, "observe_only": True})
        queue.append({"id": oid, "candidate": cand, "theme": o.get("theme", ""),
                      "final_status": o.get("final_status"),
                      "allowed_actions": list(_APPROVE_ACTIONS),
                      "blocked_actions": list(_BLOCKED_ACTIONS), "observe_only": True})

    def _wrap(key, items):
        p = observe_only_envelope(now_iso, source="market_opportunity_prompts")
        p[key] = items
        p[key.rstrip("s") + "_count" if not key.endswith("queue") else "queue_count"] = len(items)
        return p

    return {
        "market_opportunity_prompts.json": _wrap("prompts", prompts),
        "market_opportunity_review_cards.json": _wrap("cards", cards),
        "opportunity_approval_queue.json": _wrap("queue", queue),
    }


def write_market_opportunity_artifacts(root: Path, now: datetime | None = None,
                                       summarizer: Callable[[str], str] | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    base = root / "outputs"
    try:
        outs = build_market_opportunity_outputs(root, now_iso, summarizer)
        for fn, payload in outs.items():
            safe_write_json(OutputNamespace.SANDBOX, fn, payload, base_dir=base)
        return {"reviewable": len(outs["market_opportunity_prompts.json"]["prompts"]),
                "degraded": False}
    except Exception as exc:
        for fn, key in (("market_opportunity_prompts.json", "prompts"),
                        ("market_opportunity_review_cards.json", "cards"),
                        ("opportunity_approval_queue.json", "queue")):
            deg = observe_only_envelope(now_iso, source="market_opportunity_prompts",
                                        degraded_mode=True, degraded_reason=str(exc))
            deg[key] = []
            try:
                safe_write_json(OutputNamespace.SANDBOX, fn, deg, base_dir=base)
            except Exception:
                pass
        return {"reviewable": 0, "degraded": True}
