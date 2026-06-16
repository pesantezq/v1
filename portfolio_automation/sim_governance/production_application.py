"""
Production application of approved proposals (spec §7).

This is NOT a paperwork workflow. When a proposal is approved by a human, this
module materializes the change into the production overlay artifacts that the
live watchlist/advisory loaders consume:

  * outputs/latest/approved_watchlist_proposals.json
  * outputs/latest/approved_advisory_proposals.json

It IGNORES, by construction:
  * raw simulation artifacts
  * pending proposals
  * rejected proposals
  * invalid approvals (bad metadata / AI self-approval)

Every applied change carries the originating ``proposal_id`` and a rollback plan,
and every application event is appended to an audit trail. Before overwriting an
overlay, the prior version is snapshotted so a single-call rollback can restore
it (mirrors backtesting/registry_apply's snapshot-then-write discipline).

Writes:
  * outputs/latest/approved_watchlist_proposals.json     (consumed by prod loader)
  * outputs/latest/approved_advisory_proposals.json      (consumed by prod loader)
  * outputs/promotion_approvals/production_application_audit.jsonl  (append-only)
  * outputs/promotion_approvals/production_application_state.json   (current state)
  * outputs/promotion_approvals/snapshots/<overlay>.<stamp>.json    (rollback)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from portfolio_automation.data_governance import (
    OutputNamespace,
    ensure_output_dir,
    get_output_path,
    safe_write_json,
)
from portfolio_automation.sim_governance import promotion_approvals, promotion_proposals
from portfolio_automation.sim_governance import schemas as S

logger = logging.getLogger("stockbot.sim_governance.production_application")

WATCHLIST_OVERLAY = "approved_watchlist_proposals.json"
ADVISORY_OVERLAY = "approved_advisory_proposals.json"
_AUDIT_FILE = "production_application_audit.jsonl"
_STATE_FILE = "production_application_state.json"


def _stamp_from(now: str) -> str:
    """Filesystem-safe, lexically-sortable stamp derived from the ISO ts."""
    return "".join(ch for ch in (now or "") if ch.isdigit()) or "0"


def _snapshot_existing(filename: str, now: str, base_dir: str) -> str | None:
    """Snapshot the current LATEST overlay (if any) for rollback. Returns path."""
    src = Path(get_output_path(OutputNamespace.LATEST, filename, base_dir=base_dir))
    if not src.exists():
        return None
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return None
    snap_name = f"snapshots/{filename}.{_stamp_from(now)}.json"
    try:
        safe_write_json(OutputNamespace.PROMOTION_APPROVALS, snap_name, data, base_dir=base_dir)
        return str(get_output_path(OutputNamespace.PROMOTION_APPROVALS, snap_name, base_dir=base_dir))
    except Exception as exc:
        logger.warning("production_application: snapshot failed: %s", exc)
        return None


def _overlay_entry(proposal: dict) -> dict:
    """One overlay op carrying provenance + rollback metadata (spec §7)."""
    return {
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": proposal.get("candidate_id"),
        "proposal_type": proposal.get("proposal_type"),
        "change": proposal.get("proposed_production_change", {}),
        "rollback_plan": proposal.get("rollback_plan", ""),
        "applied_from": "human_approved_promotion_proposal",
    }


def apply_approved_proposals(
    now: str,
    *,
    base_dir: str,
    proposals: list[dict] | None = None,
    approved_ids: set[str] | None = None,
    rejected_ids: set[str] | None = None,
    write_files: bool = True,
) -> dict:
    """Apply only human-approved proposals into the production overlay artifacts.

    Args:
        now: ISO timestamp (caller-supplied).
        proposals: pending-proposal set (defaults to the persisted set).
        approved_ids / rejected_ids: effective human decisions (default: loaded
            from the validated approval log).
    """
    proposals = proposals if proposals is not None else promotion_proposals.load_pending_proposals(base_dir)
    approved = approved_ids if approved_ids is not None else promotion_approvals.approved_proposal_ids(base_dir)
    rejected = rejected_ids if rejected_ids is not None else promotion_approvals.rejected_proposal_ids(base_dir)

    watchlist_ops: list[dict] = []
    advisory_ops: list[dict] = []
    applied: list[dict] = []
    ignored: list[dict] = []

    for p in proposals:
        pid = p.get("proposal_id")
        ptype = p.get("proposal_type")
        if pid not in approved:
            reason = "rejected" if pid in rejected else "pending_or_unapproved"
            ignored.append({"proposal_id": pid, "reason": reason})
            continue
        if not S.is_valid_proposal_type(ptype):
            ignored.append({"proposal_id": pid, "reason": "invalid_proposal_type"})
            continue
        entry = _overlay_entry(p)
        if S.workflow_for_proposal_type(ptype) == S.WORKFLOW_WATCHLIST:
            watchlist_ops.append(entry)
        else:
            advisory_ops.append(entry)
        applied.append({"proposal_id": pid, "proposal_type": ptype,
                        "workflow": S.workflow_for_proposal_type(ptype)})

    watchlist_overlay = {
        "generated_at": now,
        "schema": "approved_watchlist_proposals.v1",
        "feeds_production": True,
        "source": "sim_governance.production_application",
        "applied_proposal_ids": [o["proposal_id"] for o in watchlist_ops],
        "ops": watchlist_ops,
    }
    advisory_overlay = {
        "generated_at": now,
        "schema": "approved_advisory_proposals.v1",
        "feeds_production": True,
        "source": "sim_governance.production_application",
        "applied_proposal_ids": [o["proposal_id"] for o in advisory_ops],
        "ops": advisory_ops,
    }

    snapshots: dict[str, str | None] = {}
    if write_files:
        snapshots[WATCHLIST_OVERLAY] = _snapshot_existing(WATCHLIST_OVERLAY, now, base_dir)
        snapshots[ADVISORY_OVERLAY] = _snapshot_existing(ADVISORY_OVERLAY, now, base_dir)
        try:
            safe_write_json(OutputNamespace.LATEST, WATCHLIST_OVERLAY, watchlist_overlay, base_dir=base_dir)
            safe_write_json(OutputNamespace.LATEST, ADVISORY_OVERLAY, advisory_overlay, base_dir=base_dir)
        except Exception as exc:
            logger.warning("production_application: overlay write failed: %s", exc)

        # Append one audit row per applied proposal (which approved proposal
        # affected production behavior + how to roll it back).
        try:
            ensure_output_dir(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE, base_dir=base_dir)
            audit_path = get_output_path(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE, base_dir=base_dir)
            with Path(audit_path).open("a", encoding="utf-8") as fh:
                for o in watchlist_ops + advisory_ops:
                    fh.write(json.dumps({
                        "ts": now,
                        "event": "applied_to_production",
                        "proposal_id": o["proposal_id"],
                        "proposal_type": o["proposal_type"],
                        "change": o["change"],
                        "rollback_plan": o["rollback_plan"],
                        "snapshots": snapshots,
                    }, default=str) + "\n")
        except Exception as exc:
            logger.warning("production_application: audit write failed: %s", exc)

    state = {
        "generated_at": now,
        "schema": "production_application_state.v1",
        "applied_count": len(applied),
        "ignored_count": len(ignored),
        "watchlist_applied": len(watchlist_ops),
        "advisory_applied": len(advisory_ops),
        "applied": applied,
        "ignored": ignored,
        "snapshots": snapshots,
        "overlays": {
            "watchlist": f"outputs/latest/{WATCHLIST_OVERLAY}",
            "advisory": f"outputs/latest/{ADVISORY_OVERLAY}",
        },
    }
    if write_files:
        try:
            safe_write_json(OutputNamespace.PROMOTION_APPROVALS, _STATE_FILE, state, base_dir=base_dir)
        except Exception as exc:
            logger.warning("production_application: state write failed: %s", exc)
            state["write_error"] = str(exc)

    logger.info("production_application: applied %d approved proposal(s) (%d watchlist, %d advisory); ignored %d",
                len(applied), len(watchlist_ops), len(advisory_ops), len(ignored))
    return state


def rollback_last(filename: str, base_dir: str, now: str) -> dict:
    """Restore the most recent snapshot of an overlay artifact.

    Returns {"ok": bool, "restored_from": path|None}.
    """
    snap_dir = Path(get_output_path(OutputNamespace.PROMOTION_APPROVALS, "snapshots", base_dir=base_dir))
    if not snap_dir.exists():
        return {"ok": False, "restored_from": None, "reason": "no_snapshots"}
    candidates = sorted(snap_dir.glob(f"{filename}.*.json"))
    if not candidates:
        return {"ok": False, "restored_from": None, "reason": "no_snapshot_for_overlay"}
    latest = candidates[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        safe_write_json(OutputNamespace.LATEST, filename, data, base_dir=base_dir)
    except Exception as exc:
        return {"ok": False, "restored_from": None, "reason": str(exc)}

    try:
        ensure_output_dir(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE, base_dir=base_dir)
        audit_path = get_output_path(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE, base_dir=base_dir)
        with Path(audit_path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now, "event": "rolled_back", "overlay": filename,
                                 "restored_from": str(latest)}, default=str) + "\n")
    except Exception:
        pass
    return {"ok": True, "restored_from": str(latest)}
