"""Strategy-lab approval → active-strategy selection (sandbox-only, human-gated).

The Strategy Lab surfaces a ranked review queue (``strategy_review_queue.json``).
A human operator may **approve** one strategy (selecting/activating it),
**reject** one, or **defer** one. The selection re-anchors the *sandbox*
projection/comparison only — it NEVER feeds ``decision_plan.json``,
``config.json``, or ``signal_registry.yaml``, and triggers no trade.

This module owns the two persisted artifacts (both ``OutputNamespace.POLICY``):
  - ``active_strategy_selection.json`` — single active selection (replace; a new
    approve supersedes the prior). Absent / ``active_strategy_id: null`` ⇒ none.
  - ``strategy_decisions.jsonl`` — append-only audit, one line per decision.

"AI cannot self-approve" is structural: every decision passes
``sim_governance.schemas.is_human_approver``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.sim_governance.schemas import is_human_approver

DECISIONS = ("approve", "reject", "defer")

_SELECTION_FILE = "active_strategy_selection.json"
_DECISIONS_FILE = "strategy_decisions.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_anchor_tactic_id(
    strategy_id: str | None, tactic_ids: Iterable[str]
) -> str | None:
    """Map a review-queue ``strategy_id`` to a projection tactic id.

    Strategy profiles are materialized as tactics named ``profile_<strategy_id>``
    (e.g. ``long_term_compounding`` → ``profile_long_term_compounding``). An exact
    match wins over the prefixed form. Returns ``None`` when nothing matches.
    """
    if not strategy_id:
        return None
    ids = set(tactic_ids or [])
    if strategy_id in ids:
        return strategy_id
    prefixed = f"profile_{strategy_id}"
    if prefixed in ids:
        return prefixed
    return None


def mark_operator_selected(
    rows: list[dict[str, Any]], active_strategy_id: str | None
) -> list[dict[str, Any]]:
    """Stamp ``operator_selected`` on each row by ``strategy_id`` match (in place)."""
    for r in rows:
        r["operator_selected"] = bool(
            active_strategy_id and r.get("strategy_id") == active_strategy_id
        )
    return rows


def load_active_selection(base_dir: Path | str = "outputs") -> dict[str, Any]:
    """Return the current active selection dict, or ``{}`` if none/absent.

    A cleared selection (``active_strategy_id: null``) is returned as written so
    callers can distinguish "explicitly cleared" from "never set" if needed;
    consumers that only care about the active id should check
    ``.get("active_strategy_id")``.
    """
    path = get_output_path(OutputNamespace.POLICY, _SELECTION_FILE, base_dir=base_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _append_decision(base_dir: Path | str, record: dict[str, Any]) -> None:
    path = get_output_path(OutputNamespace.POLICY, _DECISIONS_FILE, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def record_strategy_decision(
    strategy_id: str,
    decision: str,
    approver: str,
    *,
    valid_strategy_ids: Iterable[str],
    strategy_name: str | None = None,
    base_dir: Path | str = "outputs",
    write_files: bool = True,
) -> dict[str, Any]:
    """Record a human approve/reject/defer decision on a strategy.

    Returns ``{ok, reason, decision, strategy_id, active_strategy_id, prev_active,
    supersedes}``. On a guard failure ``ok=False`` and nothing is written.

    - **approve**: write ``active_strategy_selection.json`` (supersede prior),
      append the decision.
    - **reject**: append the decision; if ``strategy_id`` was the active one,
      clear the selection.
    - **defer**: append the decision only; selection unchanged.
    """
    valid = set(valid_strategy_ids or [])

    if decision not in DECISIONS:
        return {"ok": False, "reason": f"decision {decision!r} not in {DECISIONS}"}
    if not is_human_approver(approver):
        return {"ok": False,
                "reason": f"approver {approver!r} is not a valid human approver "
                          "(AI cannot self-approve)"}
    if strategy_id not in valid:
        return {"ok": False,
                "reason": f"strategy_id {strategy_id!r} is not in the review queue"}

    prev = load_active_selection(base_dir)
    prev_active = prev.get("active_strategy_id")

    record = {
        "ts": _now_iso(),
        "strategy_id": strategy_id,
        "decision": decision,
        "approver": approver,
        "prev_active": prev_active,
    }

    active_after = prev_active
    supersedes = None

    if write_files:
        _append_decision(base_dir, record)

    if decision == "approve":
        supersedes = prev_active if prev_active != strategy_id else None
        active_after = strategy_id
        if write_files:
            safe_write_json(
                OutputNamespace.POLICY, _SELECTION_FILE,
                {
                    "observe_only": True,
                    "no_trade": True,
                    "active_strategy_id": strategy_id,
                    "name": strategy_name or strategy_id,
                    "approved_by": approver,
                    "approved_at": record["ts"],
                    "status": "approved",
                    "supersedes": supersedes,
                },
                base_dir=base_dir,
            )
    elif decision == "reject" and prev_active == strategy_id:
        active_after = None
        if write_files:
            safe_write_json(
                OutputNamespace.POLICY, _SELECTION_FILE,
                {
                    "observe_only": True,
                    "no_trade": True,
                    "active_strategy_id": None,
                    "name": None,
                    "approved_by": approver,
                    "approved_at": record["ts"],
                    "status": "cleared",
                    "supersedes": prev_active,
                },
                base_dir=base_dir,
            )

    return {
        "ok": True,
        "reason": "ok",
        "decision": decision,
        "strategy_id": strategy_id,
        "active_strategy_id": active_after,
        "prev_active": prev_active,
        "supersedes": supersedes,
    }


# ---------------------------------------------------------------------------
# Bounded GPT auto-approval channel — SIMULATION ONLY (NOT human, NOT production).
#
# This is deliberately a SEPARATE function from record_strategy_decision so the
# auto-approval channel never travels a human-approval code path. The written
# selection carries approval_channel="auto_approval" + is_human_approved=False, so it
# is structurally distinguishable from a human selection and can never impersonate one.
# ---------------------------------------------------------------------------


def record_auto_strategy_anchor(
    strategy_id: str,
    *,
    valid_strategy_ids: Iterable[str],
    now: str,
    strategy_name: str | None = None,
    base_dir: Path | str = "outputs",
    write_files: bool = True,
) -> dict[str, Any]:
    """Anchor the active SIMULATION strategy via the auto-approval channel.

    Returns ``{ok, reason, active_strategy_id, prev_active, before_state, after_state}``.
    ``before_state`` is the exact prior selection (or None) for compare-and-swap rollback.
    """
    valid = set(valid_strategy_ids or [])
    if strategy_id not in valid:
        return {"ok": False,
                "reason": f"strategy_id {strategy_id!r} is not a valid simulation strategy"}

    prev = load_active_selection(base_dir)
    before_state = prev or None
    prev_active = prev.get("active_strategy_id")

    after_state = {
        "observe_only": True,
        "no_trade": True,
        "active_strategy_id": strategy_id,
        "name": strategy_name or strategy_id,
        "approval_channel": "auto_approval",
        "is_human_approved": False,
        "approved_by": "auto_approval",
        "approved_at": now,
        "status": "auto_anchored_simulation",
        "supersedes": prev_active if prev_active != strategy_id else None,
    }

    if write_files:
        _append_decision(base_dir, {
            "ts": now, "strategy_id": strategy_id, "decision": "auto_anchor",
            "approval_channel": "auto_approval", "is_human_approved": False,
            "prev_active": prev_active,
        })
        safe_write_json(OutputNamespace.POLICY, _SELECTION_FILE, after_state, base_dir=base_dir)

    return {"ok": True, "reason": "ok", "active_strategy_id": strategy_id,
            "prev_active": prev_active, "before_state": before_state,
            "after_state": after_state}


def restore_active_selection(
    prior: dict | None,
    *,
    base_dir: Path | str = "outputs",
    now: str | None = None,
    write_files: bool = True,
) -> None:
    """Rewrite the active-selection file to an exact prior state (or clear it if there
    was none). Used by the event-aware rollback AFTER its compare-and-swap check."""
    if not write_files:
        return
    if prior:
        safe_write_json(OutputNamespace.POLICY, _SELECTION_FILE, prior, base_dir=base_dir)
    else:
        safe_write_json(
            OutputNamespace.POLICY, _SELECTION_FILE,
            {"observe_only": True, "no_trade": True, "active_strategy_id": None,
             "name": None, "status": "cleared", "approved_at": now or _now_iso(),
             "supersedes": None},
            base_dir=base_dir,
        )
