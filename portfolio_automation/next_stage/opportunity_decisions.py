"""Market-opportunity approval decisions (human-gated, sandbox/observe-only).

The Strategy Lab surfaces a market-opportunity queue (``operator_action_queue.json``)
of candidates, each carrying ``allowed_actions`` (routing verbs like
``approve_to_watchlist_review`` / ``reject`` / ``keep_watching``) and
``blocked_actions`` (trade verbs — never invocable). A human operator may act on
an item; the decision is appended to ``user_decisions.jsonl`` (POLICY). The one
active effect is ``approve_to_watchlist_review``, which routes the candidate to
the extended-watchlist operator-promotion path (capacity-gated) — handled by the
GUI route, not here. This module owns validation + the audit sink only.

"AI cannot self-approve" is structural via
``sim_governance.schemas.is_human_approver``. Actions are restricted to the
item's own ``allowed_actions``, so the ``blocked_actions`` trade verbs are
un-invocable by construction.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from portfolio_automation.data_governance import OutputNamespace, get_output_path
from portfolio_automation.sim_governance.schemas import is_human_approver

_DECISIONS_FILE = "user_decisions.jsonl"

#: The one routing action that also triggers an extended-watchlist promotion.
PROMOTE_ACTION = "approve_to_watchlist_review"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_opportunity_action(
    opportunity_id: str,
    action: str,
    approver: str,
    queue_items: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Validate a market-opportunity decision against the live queue.

    Returns ``{ok, reason, candidate, allowed_actions, should_promote}``. A
    decision is valid only when the approver is human, the ``opportunity_id``
    exists, and the ``action`` is one of that item's ``allowed_actions``.
    """
    if not is_human_approver(approver):
        return {"ok": False,
                "reason": f"approver {approver!r} is not a valid human approver "
                          "(AI cannot self-approve)"}

    item = next((i for i in (queue_items or []) if i.get("id") == opportunity_id), None)
    if item is None:
        return {"ok": False, "reason": f"opportunity_id {opportunity_id!r} not in queue"}

    allowed = list(item.get("allowed_actions") or [])
    if action not in allowed:
        return {"ok": False,
                "reason": f"action {action!r} not in allowed_actions for {opportunity_id}"}

    return {
        "ok": True,
        "reason": "ok",
        "candidate": item.get("candidate"),
        "allowed_actions": allowed,
        "should_promote": action == PROMOTE_ACTION,
    }


def append_opportunity_decision(
    opportunity_id: str,
    candidate: str | None,
    action: str,
    approver: str,
    *,
    base_dir: Path | str = "outputs",
    promote_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one market-opportunity decision to ``user_decisions.jsonl``."""
    record = {
        "ts": _now_iso(),
        "opportunity_id": opportunity_id,
        "candidate": candidate,
        "action": action,
        "approver": approver,
        "promote_result": promote_result,
    }
    path = get_output_path(OutputNamespace.POLICY, _DECISIONS_FILE, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return record
