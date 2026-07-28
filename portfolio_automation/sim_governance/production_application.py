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

# ---------------------------------------------------------------------------
# Durability is a property of the PROPOSAL TYPE, not of the workflow
# (operator decision 2026-07-28). The staleness rule follows the DATA, not the
# folder the type happens to be filed under.
#
#   * A MEMBERSHIP DECISION — "XOM belongs on the watchlist", "drop RIOT",
#     "rank it 12", "tag it defensive" — is a durable fact. It stays true until a
#     human reverses it, so it must survive runs in which its candidate is no
#     longer proposed. That is exactly what happens after a removal is applied and
#     the producer correctly self-suppresses.
#
#   * A STATE-DERIVED LABEL — "flock is confirmed on GOOGL", every advisory
#     context / ranking / strategy annotation — is only true while the state that
#     produced it holds. Its candidate_id is salted by that state, so persisting
#     one would keep a stale label alive past the state change: precisely the
#     staleness hazard that justifies advisory refresh. These are rebuilt from the
#     current pending set on every run.
#
# Deliberately EXCLUDED from the durable set (refresh semantics):
#   * PROPOSAL_FLOCK_WATCHLIST_LOGIC — filed under the watchlist workflow but
#     salted by flock STATE (simulation_lane.py, salt=state); a state-derived
#     label, not a membership decision.
#   * every advisory / crowd / flock advisory+risk+scoring type.
#   * PROPOSAL_DISCOVERY_PROMOTION — a membership add in shape, but it is not in
#     the operator's named durable set and no producer currently emits it, so it
#     keeps refresh semantics rather than silently widening the durable lane.
_DURABLE_PROPOSAL_TYPES = frozenset({
    S.PROPOSAL_WATCHLIST_ADD,
    S.PROPOSAL_WATCHLIST_REMOVE,
    S.PROPOSAL_WATCHLIST_RANK,
    S.PROPOSAL_WATCHLIST_TAG,
})

# Ops that decide whether a symbol IS or IS NOT on the watchlist. Two opposite
# directions cannot both be live for one symbol, so they are resolved against each
# other by recency (see _resolve_durable_ops step 2).
_MEMBERSHIP_PROPOSAL_TYPES = frozenset({
    S.PROPOSAL_WATCHLIST_ADD,
    S.PROPOSAL_WATCHLIST_REMOVE,
    S.PROPOSAL_DISCOVERY_PROMOTION,
})


def is_durable_proposal_type(proposal_type: object) -> bool:
    """True when an applied op of this type must persist across runs."""
    return isinstance(proposal_type, str) and proposal_type in _DURABLE_PROPOSAL_TYPES


def _symbol_of(op: dict) -> str:
    return str((op.get("change") or {}).get("symbol", "") or "").upper()


def _op_identity_key(proposal_type: object, candidate_id: object, change: dict | None) -> str:
    """Dedup identity for a durable op.

    ``candidate_id`` is the durable identity when present. When it is absent —
    every audit row written before 2026-07-28, and every legacy approval — fall
    back to the FACT, ``(proposal_type, symbol)``, and never to ``proposal_id``:
    that id is clock-salted and mints a new value on every run, so keying on it
    would file two applications of the SAME fact on two days as two separate ops.
    """
    if candidate_id:
        return f"cand:{candidate_id}"
    return f"fact:{proposal_type}:{str((change or {}).get('symbol', '') or '').upper()}"


def _conflict_key(op: dict) -> tuple[str, str]:
    """The fact an op asserts: one op may be live per (symbol, proposal_type)."""
    ptype = str(op.get("proposal_type"))
    sym = _symbol_of(op)
    if not sym:
        # No symbol → the overlay fold skips it anyway; keep such ops distinct
        # rather than collapsing unrelated ones onto a shared empty-symbol key.
        return ("\x00" + _op_identity_key(ptype, op.get("candidate_id"), op.get("change")), ptype)
    return (sym, ptype)


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


def _drop_rolled_back(seen: dict[str, tuple[str, dict]], row: dict) -> None:
    """Apply a ``rolled_back`` audit event to the chronological carry-forward fold.

    ``rollback_last`` restores a prior overlay snapshot. Without honouring the
    event here the carry-forward would put the rolled-back ops straight back on the
    next run, silently undoing the operator's rollback.

    Rows written by ``rollback_last`` carry ``rolled_back_ids`` (the ids that were
    live before the restore and are absent from the restored snapshot). A legacy
    row for the watchlist overlay has no such field; it is treated as reverting
    every durable op applied so far, which is the NO-RESURRECTION direction: a
    still-wanted op can be re-approved, whereas an op re-applied behind the
    operator's back cannot be un-applied.
    """
    overlay = row.get("overlay")
    if overlay is not None and overlay != WATCHLIST_OVERLAY:
        return
    ids = row.get("rolled_back_ids")
    if not isinstance(ids, list):
        seen.clear()
        return
    targets = {str(i) for i in ids if i}
    if not targets:
        return
    for key, (_ts, op) in list(seen.items()):
        cid = op.get("candidate_id")
        if str(op.get("proposal_id")) in targets or (cid and str(cid) in targets):
            seen.pop(key, None)


def _prior_durable_ops(
    base_dir: str,
    *,
    approved: set[str],
    approved_cands: set[str],
    rejected: set[str],
    rejected_cands: set[str],
    revoked: set[str],
) -> list[tuple[str, dict]]:
    """Previously-applied DURABLE ops, from the append-only audit log.

    Returns ``[(applied_ts, op), ...]`` — the timestamp is the recency signal the
    conflict resolver needs.

    Durable ops are membership state: once applied they must survive runs in which
    their candidate is no longer proposed. State-derived labels (advisory context,
    flock candidate logic) are deliberately NOT included — see
    ``_DURABLE_PROPOSAL_TYPES``.

    An audit row is not authority on its own. A prior op is carried forward only
    while it is still backed by a valid human approval and has not been rejected,
    revoked, or rolled back. The authority filter runs AFTER the chronological
    fold so that a rollback recorded before a later re-application does not
    outlive it.

    Tolerant: a missing or partly-corrupt log yields whatever rows parse.
    """
    path = Path(get_output_path(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE,
                                base_dir=base_dir))
    if not path.exists():
        return []

    seen: dict[str, tuple[str, dict]] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("event") == "rolled_back":
                _drop_rolled_back(seen, row)
                continue
            if row.get("event") != "applied_to_production":
                continue
            ptype = row.get("proposal_type")
            if not is_durable_proposal_type(ptype):
                continue
            cid = row.get("candidate_id")
            key = _op_identity_key(ptype, cid, row.get("change"))
            seen[key] = (str(row.get("ts") or ""), {
                "proposal_id": row.get("proposal_id"),
                "candidate_id": cid,
                "proposal_type": ptype,
                "change": row.get("change", {}),
                "rollback_plan": row.get("rollback_plan", ""),
                "applied_from": "human_approved_promotion_proposal",
            })
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("production_application: prior-ops read failed: %s", exc)
        return []

    out: list[tuple[str, dict]] = []
    for ts, op in seen.values():
        pid = op.get("proposal_id")
        cid = op.get("candidate_id")
        if pid in revoked or (cid and cid in revoked):
            continue
        if pid in rejected or (cid and cid in rejected_cands):
            continue
        if pid not in approved and not (cid and cid in approved_cands):
            continue
        out.append((ts, op))
    return out


def _resolve_durable_ops(today_ops: list[dict],
                         prior_ops: list[tuple[str, dict]]) -> list[dict]:
    """Collapse durable ops so the most recently approved op wins per fact.

    ``production_overlays.apply_approved_watchlist`` is a sequential
    last-writer-wins fold, so merely appending carried ops after today's would let
    a months-old op override a fresh human approval — and the effective order
    would flip between runs as an op moves from the "today" list into the carried
    list. Conflicts are therefore resolved HERE, explicitly, before the overlay is
    written:

      1. one op per ``(symbol, proposal_type)`` — the most recent wins;
      2. one membership direction per symbol — add and remove cannot both be live,
         so the most recent of the two wins and the other is dropped;
      3. the survivors are emitted oldest-first, so even the fold's
         last-writer-wins order agrees with recency.

    Today's op always outranks a carried one for the same fact: it was matched
    against a live human approval on this run, whereas a carried op's recency is
    its application time (an op is only ever applied after approval, so
    application order is approval order).
    """
    ranked: list[tuple[tuple[int, str, int], dict]] = []
    for i, op in enumerate(today_ops):
        ranked.append(((1, "", i), op))
    for i, (ts, op) in enumerate(prior_ops):
        ranked.append(((0, str(ts or ""), i), op))

    # 1. one op per asserted fact
    best: dict[tuple[str, str], tuple[tuple[int, str, int], dict]] = {}
    for rank, op in ranked:
        key = _conflict_key(op)
        cur = best.get(key)
        if cur is None or rank > cur[0]:
            best[key] = (rank, op)
    winners = list(best.values())

    # 2. one membership direction per symbol
    membership: dict[str, tuple[tuple[int, str, int], dict]] = {}
    for rank, op in winners:
        if str(op.get("proposal_type")) in _MEMBERSHIP_PROPOSAL_TYPES:
            sym = _symbol_of(op)
            cur = membership.get(sym)
            if cur is None or rank > cur[0]:
                membership[sym] = (rank, op)
    resolved = [
        (rank, op) for rank, op in winners
        if str(op.get("proposal_type")) not in _MEMBERSHIP_PROPOSAL_TYPES
        or membership[_symbol_of(op)][1] is op
    ]

    # 3. oldest-first, so the downstream fold's last writer is the newest op
    resolved.sort(key=lambda t: t[0])
    return [op for _rank, op in resolved]


def _read_overlay_for_rollback(filename: str, base_dir: str) -> dict:
    """The overlay currently on disk ({} when absent/unreadable)."""
    try:
        path = Path(get_output_path(OutputNamespace.LATEST, filename, base_dir=base_dir))
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _overlay_op_ids(data: dict) -> set[str]:
    """Every identity (proposal_id + candidate_id) carried by an overlay's ops."""
    out: set[str] = set()
    for op in (data.get("ops") or []):
        if not isinstance(op, dict):
            continue
        for field in ("proposal_id", "candidate_id"):
            val = op.get(field)
            if val:
                out.add(str(val))
    return out


def _live_overlay_op_count(filename: str, base_dir: str) -> int:
    """Number of ops in the overlay currently on disk (0 when absent/unreadable)."""
    return len(_read_overlay_for_rollback(filename, base_dir).get("ops") or [])


def apply_approved_proposals(
    now: str,
    *,
    base_dir: str,
    proposals: list[dict] | None = None,
    approved_ids: set[str] | None = None,
    rejected_ids: set[str] | None = None,
    approved_candidate_ids: set[str] | None = None,
    rejected_candidate_ids: set[str] | None = None,
    write_files: bool = True,
) -> dict:
    """Apply only human-approved proposals into the production overlay artifacts.

    Args:
        now: ISO timestamp (caller-supplied).
        proposals: pending-proposal set (defaults to the persisted set).
        approved_ids / rejected_ids: effective human decisions (default: loaded
            from the validated approval log).
        approved_candidate_ids / rejected_candidate_ids: effective human decisions
            keyed on the durable candidate_id (default: loaded from the
            validated approval log).
    """
    # FAIL CLOSED on an unreadable approvals log. "Degrade to empty" is safe for an
    # advisory refresh but wrong for durable state: an empty `approved` set drops
    # every carried op and writes `ops: []`, which SILENTLY REVERSES established
    # production membership. An ABSENT log still means "no approvals yet"; a log
    # that exists but cannot be parsed means the authority is unreadable, and the
    # only safe action is to leave the existing overlay exactly as it is.
    unreadable = promotion_approvals.approvals_log_unreadable(base_dir)
    if unreadable:
        logger.error(
            "production_application: REFUSING overlay rebuild — approvals log is "
            "unreadable (%s); the existing overlays are left untouched", unreadable)
        state = {
            "generated_at": now,
            "schema": "production_application_state.v1",
            "overlay_rebuild_skipped": True,
            "approvals_log_unreadable": unreadable,
            "applied_count": 0,
            "applied_today_count": 0,
            "ignored_count": 0,
            # Report what is STILL live on disk, not zero — the overlays were not
            # touched, so claiming nothing is applied would be a lie.
            "watchlist_applied": _live_overlay_op_count(WATCHLIST_OVERLAY, base_dir),
            "watchlist_applied_today": 0,
            "watchlist_carried_forward": _live_overlay_op_count(WATCHLIST_OVERLAY, base_dir),
            "durably_live_count": _live_overlay_op_count(WATCHLIST_OVERLAY, base_dir),
            "advisory_applied": _live_overlay_op_count(ADVISORY_OVERLAY, base_dir),
            "applied": [],
            "ignored": [],
            "snapshots": {},
            "overlays": {
                "watchlist": f"outputs/latest/{WATCHLIST_OVERLAY}",
                "advisory": f"outputs/latest/{ADVISORY_OVERLAY}",
            },
        }
        if write_files:
            try:
                safe_write_json(OutputNamespace.PROMOTION_APPROVALS, _STATE_FILE, state,
                                base_dir=base_dir)
            except Exception as exc:
                logger.warning("production_application: state write failed: %s", exc)
                state["write_error"] = str(exc)
        return state

    proposals = proposals if proposals is not None else promotion_proposals.load_pending_proposals(base_dir)
    approved = approved_ids if approved_ids is not None else promotion_approvals.approved_proposal_ids(base_dir)
    rejected = rejected_ids if rejected_ids is not None else promotion_approvals.rejected_proposal_ids(base_dir)
    # Durable identity: proposal_id is clock-salted and churns every run, so an
    # unchanged fact would otherwise need re-approval daily. candidate_id is
    # stable-when-unchanged, so an approval filed against it stays effective.
    approved_cands = (approved_candidate_ids if approved_candidate_ids is not None
                      else promotion_approvals.approved_candidate_ids(base_dir))
    rejected_cands = (rejected_candidate_ids if rejected_candidate_ids is not None
                      else promotion_approvals.rejected_candidate_ids(base_dir))

    # Revocations are resolved BEFORE the per-proposal loop, not after it: a
    # revoked target that is still sitting in today's pending set must not be
    # re-applied (and thereby re-durabilized) just because the loop only knew about
    # rejections.
    revoked = promotion_approvals.revoked_ids(base_dir)

    watchlist_ops: list[dict] = []
    advisory_ops: list[dict] = []
    applied: list[dict] = []
    ignored: list[dict] = []

    for p in proposals:
        pid = p.get("proposal_id")
        ptype = p.get("proposal_type")
        cid = p.get("candidate_id")
        # Reject/revoke win over approve on BOTH identities — the gate never loosens.
        if pid in revoked or (cid and cid in revoked):
            ignored.append({"proposal_id": pid, "reason": "revoked"})
            continue
        if pid in rejected or (cid and cid in rejected_cands):
            ignored.append({"proposal_id": pid, "reason": "rejected"})
            continue
        if pid not in approved and not (cid and cid in approved_cands):
            ignored.append({"proposal_id": pid, "reason": "pending_or_unapproved"})
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

    # Watchlist membership is durable: carry forward previously-applied DURABLE ops
    # that are still approved and not rejected/revoked/rolled-back, then resolve
    # every conflict explicitly so the most recent human decision wins regardless of
    # list order. Advisory ops are untouched — they must refresh from the current
    # pending set.
    _prior = _prior_durable_ops(
        base_dir,
        approved=approved, approved_cands=approved_cands,
        rejected=rejected, rejected_cands=rejected_cands,
        revoked=revoked,
    )
    _today_op_ids = {id(o) for o in watchlist_ops}
    watchlist_ops = _resolve_durable_ops(watchlist_ops, _prior)
    watchlist_today = sum(1 for o in watchlist_ops if id(o) in _today_op_ids)
    watchlist_carried = len(watchlist_ops) - watchlist_today

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
                        "candidate_id": o.get("candidate_id"),
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
        "overlay_rebuild_skipped": False,
        # applied_count counts only THIS run's approvals. It is NOT the number of
        # ops live in production — a durable op stays live without being re-applied,
        # so this reads 0 on a quiet day while membership ops are still in force.
        # Operator-facing surfaces must show durably_live_count alongside it.
        "applied_count": len(applied),
        "applied_today_count": len(applied),
        "ignored_count": len(ignored),
        "watchlist_applied": len(watchlist_ops),
        "watchlist_applied_today": watchlist_today,
        "watchlist_carried_forward": watchlist_carried,
        "durably_live_count": len(watchlist_ops),
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
    # Which ops does this rollback actually revert? Whatever is live now and is not
    # in the snapshot being restored. Recorded on the audit event so the durable
    # carry-forward can honour the rollback instead of re-applying those ops on the
    # next run (_drop_rolled_back).
    live_before = _overlay_op_ids(_read_overlay_for_rollback(filename, base_dir))
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        safe_write_json(OutputNamespace.LATEST, filename, data, base_dir=base_dir)
    except Exception as exc:
        return {"ok": False, "restored_from": None, "reason": str(exc)}

    rolled_back_ids = sorted(live_before - _overlay_op_ids(data if isinstance(data, dict) else {}))
    try:
        ensure_output_dir(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE, base_dir=base_dir)
        audit_path = get_output_path(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE, base_dir=base_dir)
        with Path(audit_path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now, "event": "rolled_back", "overlay": filename,
                                 "restored_from": str(latest),
                                 "rolled_back_ids": rolled_back_ids}, default=str) + "\n")
    except Exception:
        pass
    return {"ok": True, "restored_from": str(latest), "rolled_back_ids": rolled_back_ids}
