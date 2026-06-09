"""Artifact-based approval queues (Phase 4, spec §13).

Builds the operator review queues and records decisions — all append-only, all
executing nothing. Approving a market opportunity at most promotes it to
watchlist review; approving a system improvement at most generates a Claude Code
prompt. Rejections/deferrals enforce a cooldown so the same item does not spam.

Writes:
* ``outputs/latest/operator_action_queue.json``            (market-opportunity review)
* ``outputs/latest/system_improvement_action_queue.json``  (improvement review)
* ``outputs/policy/user_decisions.jsonl``                  (append-only)
* ``outputs/policy/system_improvement_decisions.jsonl``    (append-only)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.next_stage.contracts import observe_only_envelope
from portfolio_automation import event_store

_COOLDOWN_DAYS = 14

_OPPORTUNITY_DECISIONS = {"approve_to_watchlist_review", "reject", "keep_watching",
                          "request_deeper_research", "send_to_sandbox", "add_to_boom_bucket_review"}
_IMPROVEMENT_DECISIONS = {"approve_for_implementation", "reject", "defer",
                          "request_more_detail", "mark_duplicate", "mark_completed",
                          "create_claude_code_prompt"}

_QUEUES = {
    "opportunity": ("user_decisions.jsonl", _OPPORTUNITY_DECISIONS, "user_action_log.jsonl"),
    "system_improvement": ("system_improvement_decisions.jsonl", _IMPROVEMENT_DECISIONS,
                           "user_action_log.jsonl"),
}
# decisions that suppress an item from re-surfacing (with cooldown for defer/reject)
_SUPPRESS = {"reject", "defer", "mark_completed", "mark_duplicate", "approve_for_implementation",
             "approve_to_watchlist_review"}
_COOLDOWN_DECISIONS = {"reject", "defer"}


def _load_json_safe(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return out


def record_decision(root: Path, queue: str, item_id: str, decision: str,
                    note: str = "", now: datetime | None = None) -> dict[str, Any]:
    """Append an operator decision (append-only) + mirror to the event log. Executes nothing."""
    now = now or datetime.now(timezone.utc)
    if queue not in _QUEUES:
        raise ValueError(f"unknown queue: {queue!r}")
    decisions_file, allowed, _ = _QUEUES[queue]
    if decision not in allowed:
        raise ValueError(f"decision {decision!r} not allowed for queue {queue!r}")
    cooldown_until = None
    if decision in _COOLDOWN_DECISIONS:
        cooldown_until = (now.date() + timedelta(days=_COOLDOWN_DAYS)).isoformat()
    rec = {"item_id": item_id, "queue": queue, "decision": decision, "note": note,
           "timestamp": now.isoformat(), "cooldown_until": cooldown_until,
           "executes_nothing": True, "observe_only": True}
    try:
        path = root / "outputs" / "policy" / decisions_file
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass
    # mirror to the learning-loop user-action stream (non-fatal)
    event_store.record_user_action(root, ticker_or_theme=item_id,
                                   recommendation_or_action_or_status=decision,
                                   user_decision=decision, timestamp=now.isoformat())
    return rec


def _suppressed(decisions: list[dict[str, Any]], today: date) -> set[str]:
    out: set[str] = set()
    for rec in decisions:
        d = rec.get("decision")
        if d not in _SUPPRESS:
            continue
        cu = rec.get("cooldown_until")
        if d in _COOLDOWN_DECISIONS and cu:
            try:
                if date.fromisoformat(cu[:10]) < today:
                    continue  # cooldown elapsed → may resurface
            except Exception:
                pass
        out.add(rec.get("item_id"))
    return out


def build_action_queues(root: Path, now: datetime | None = None) -> dict[str, Any]:
    """Build both operator review queues from sources minus suppressed items. Non-fatal."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    base = root / "outputs"
    today = now.date()
    try:
        # ── market-opportunity review queue ──
        opp_src = _load_json_safe(base / "sandbox" / "opportunity_approval_queue.json") or {}
        opp_supp = _suppressed(_read_jsonl(base / "policy" / "user_decisions.jsonl"), today)
        opp_items = [it for it in (opp_src.get("queue", []) or [])
                     if it.get("id") not in opp_supp]
        opp_q = observe_only_envelope(now_iso, source="approval_layer", executes_nothing=True)
        opp_q["queue"] = opp_items
        opp_q["queue_count"] = len(opp_items)
        safe_write_json(OutputNamespace.LATEST, "operator_action_queue.json", opp_q, base_dir=base)

        # ── system-improvement review queue ──
        ideas = (_load_json_safe(base / "latest" / "system_improvement_ideas.json") or {}).get("ideas", [])
        imp_supp = _suppressed(_read_jsonl(base / "policy" / "system_improvement_decisions.jsonl"), today)
        imp_items = [{
            "id": i.get("id"), "title": i.get("title"), "category": i.get("category"),
            "priority": i.get("priority"), "final_rank_score": i.get("final_rank_score"),
            "allowed_actions": sorted(_IMPROVEMENT_DECISIONS),
            "blocked_actions": ["place_trade", "submit_order", "modify_real_holdings",
                                "auto_apply_code_change"],
            "observe_only": True,
        } for i in ideas if i.get("id") not in imp_supp and i.get("status") != "duplicate"]
        imp_q = observe_only_envelope(now_iso, source="approval_layer", executes_nothing=True)
        imp_q["queue"] = imp_items
        imp_q["queue_count"] = len(imp_items)
        safe_write_json(OutputNamespace.LATEST, "system_improvement_action_queue.json", imp_q, base_dir=base)

        return {"opportunity_open": len(opp_items), "improvement_open": len(imp_items),
                "degraded": False}
    except Exception as exc:
        for fn in ("operator_action_queue.json", "system_improvement_action_queue.json"):
            deg = observe_only_envelope(now_iso, source="approval_layer",
                                        degraded_mode=True, degraded_reason=str(exc),
                                        executes_nothing=True)
            deg["queue"] = []
            try:
                safe_write_json(OutputNamespace.LATEST, fn, deg, base_dir=base)
            except Exception:
                pass
        return {"opportunity_open": 0, "improvement_open": 0, "degraded": True}
