"""Work orders — append-only, registry-validated operator work items.

A *work order* records an operator's request to investigate or (in a future
phase) repair a probe via an allowlisted skill. Storage is **append-only**
event-sourced JSONL: each create/transition appends a full snapshot line to
``outputs/operator_control/work_orders.jsonl``; readers fold the log by
``work_order_id`` (last line wins). The file is never rewritten — it only grows.

Phase 1 boundaries:
  * ``create`` validates against the probe + skill registries and the repair
    policy, then appends a record. Nothing is executed.
  * ``requested_action`` is composed only from the registries — there is no
    field through which a caller can store an arbitrary/executable command.
  * Status transitions are validated against the policy graph.

Also exposes a small ``argparse`` CLI:

    python -m operator_control.work_orders list
    python -m operator_control.work_orders create --probe-id data_quality.warnings \\
        --skill-id diagnose_data_quality_warnings --mode diagnose --created-by enrique_cli
    python -m operator_control.work_orders show --id <work_order_id>
    python -m operator_control.work_orders generate-prompt --id <work_order_id>
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from operator_control import work_orders_path
from operator_control import audit_log
from operator_control.probe_registry import get_probe
from operator_control.skill_registry import get_skill
from operator_control import repair_policies as policy

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

# Safety constraints stamped onto every work order. These are data, not
# behavior — the worker prompt repeats them; the dashboard never executes them.
_SAFETY_CONSTRAINTS: tuple[str, ...] = (
    "observe_only",
    "no_trade_execution",
    "no_broker_orders",
    "no_scoring_or_decision_logic_changes",
    "no_secrets_exposure",
    "no_arbitrary_shell_from_web",
    "human_review_required_before_any_apply",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_work_order_id() -> str:
    # Timestamp prefix for sortability + a short random suffix for uniqueness.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"wo_{stamp}_{secrets.token_hex(3)}"


# ---------------------------------------------------------------------------
# Append-only storage primitives
# ---------------------------------------------------------------------------


def _append_record(root: Path | str, record: dict[str, Any]) -> None:
    path = work_orders_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _load_all_records(root: Path | str) -> list[dict[str, Any]]:
    path = work_orders_path(root)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _fold_latest(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fold event-sourced records into latest-state-per-id (last line wins)."""
    latest: dict[str, dict[str, Any]] = {}
    for rec in records:
        wid = rec.get("work_order_id")
        if wid:
            latest[wid] = rec
    return latest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_work_order(
    root: Path | str,
    *,
    probe_id: str,
    skill_id: str,
    mode: str,
    created_by: str,
    source_view: str | None = None,
) -> dict[str, Any]:
    """Validate and append a new work order. Returns the created record.

    Raises :class:`operator_control.repair_policies.WorkOrderValidationError`
    on any inadmissible request (and records a ``validation_rejected`` audit
    event before raising).
    """
    try:
        policy.validate_combination(probe_id, skill_id, mode)
    except policy.WorkOrderValidationError as exc:
        audit_log.record_event(
            root,
            event_type="validation_rejected",
            actor=created_by,
            probe_id=probe_id,
            skill_id=skill_id,
            mode=mode,
            details={"reason": str(exc)},
            safety_result=f"rejected: {exc}",
        )
        raise

    probe = get_probe(probe_id)
    skill = get_skill(skill_id)
    assert probe is not None and skill is not None  # validate_combination ensured

    approval_required = policy.requires_approval(probe_id, skill_id, mode)
    risk_level = policy.effective_risk_level(probe_id, skill_id, mode)
    status = policy.initial_status(approval_required)
    now = _now_iso()
    wid = _new_work_order_id()

    record: dict[str, Any] = {
        "work_order_id": wid,
        "created_at": now,
        "created_by": created_by,
        "source_view": source_view or probe.source_view,
        "probe_id": probe_id,
        "skill_id": skill_id,
        "mode": mode,
        "risk_level": risk_level,
        "approval_required": approval_required,
        "status": status,
        "status_history": [
            {"status": status, "at": now, "actor": created_by, "note": "created"}
        ],
        "source_artifacts": [probe.source_artifact],
        "requested_action": policy.derive_requested_action(probe_id, skill_id, mode),
        "safety_constraints": list(_SAFETY_CONSTRAINTS),
        "generated_prompt_path": None,
        "result_report_path": None,
        "observe_only": True,
    }
    _append_record(root, record)
    audit_log.record_event(
        root,
        event_type="work_order_created",
        actor=created_by,
        work_order_id=wid,
        probe_id=probe_id,
        skill_id=skill_id,
        mode=mode,
        details={
            "status": status,
            "risk_level": risk_level,
            "approval_required": approval_required,
        },
    )
    return record


def list_work_orders(
    root: Path | str, *, status: str | None = None
) -> list[dict[str, Any]]:
    """Return current work orders (folded), newest-created first."""
    latest = _fold_latest(_load_all_records(root))
    orders = list(latest.values())
    if status is not None:
        orders = [o for o in orders if o.get("status") == status]
    orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    return orders


def get_work_order(root: Path | str, work_order_id: str) -> dict[str, Any] | None:
    return _fold_latest(_load_all_records(root)).get(work_order_id)


def transition_work_order(
    root: Path | str,
    work_order_id: str,
    *,
    new_status: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    """Append a transition record for an existing work order.

    Validates the transition against the policy graph. Returns the new record.
    """
    current = get_work_order(root, work_order_id)
    if current is None:
        raise policy.WorkOrderValidationError(
            f"unknown work_order_id: {work_order_id!r}"
        )
    policy.validate_transition(current.get("status", ""), new_status)

    now = _now_iso()
    new_record = dict(current)  # snapshot
    new_record["status"] = new_status
    history = list(current.get("status_history") or [])
    history.append({"status": new_status, "at": now, "actor": actor, "note": note})
    new_record["status_history"] = history
    _append_record(root, new_record)

    event_type = {
        "approved": "approval_granted",
        "rejected": "approval_rejected",
        "cancelled": "work_order_cancelled",
    }.get(new_status, "work_order_status_changed")
    audit_log.record_event(
        root,
        event_type=event_type,
        actor=actor,
        work_order_id=work_order_id,
        probe_id=current.get("probe_id"),
        skill_id=current.get("skill_id"),
        mode=current.get("mode"),
        details={"from": current.get("status"), "to": new_status, "note": note},
    )
    return new_record


def attach_prompt_path(
    root: Path | str, work_order_id: str, prompt_path: str, actor: str
) -> dict[str, Any]:
    """Record that a worker prompt file was generated for this work order.

    This does NOT change status — generating a prompt is preparation, not
    execution. Appends a snapshot with ``generated_prompt_path`` set.
    """
    current = get_work_order(root, work_order_id)
    if current is None:
        raise policy.WorkOrderValidationError(
            f"unknown work_order_id: {work_order_id!r}"
        )
    new_record = dict(current)
    new_record["generated_prompt_path"] = prompt_path
    _append_record(root, new_record)
    audit_log.record_event(
        root,
        event_type="prompt_generated",
        actor=actor,
        work_order_id=work_order_id,
        probe_id=current.get("probe_id"),
        skill_id=current.get("skill_id"),
        mode=current.get("mode"),
        details={"prompt_path": prompt_path},
    )
    return new_record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m operator_control.work_orders",
        description="Operator-control work orders (observe-only; no execution).",
    )
    p.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="Repo root (default: package parent).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp_list = sub.add_parser("list", help="List current work orders.")
    sp_list.add_argument("--status", default=None, help="Filter by status.")
    sp_list.add_argument("--json", action="store_true", help="Emit JSON.")

    sp_create = sub.add_parser("create", help="Create a work order.")
    sp_create.add_argument("--probe-id", required=True)
    sp_create.add_argument("--skill-id", required=True)
    sp_create.add_argument("--mode", required=True, choices=("diagnose", "propose_fix", "safe_repair"))
    sp_create.add_argument("--created-by", required=True)

    sp_show = sub.add_parser("show", help="Show one work order.")
    sp_show.add_argument("--id", required=True)

    sp_prompt = sub.add_parser("generate-prompt", help="Generate the worker prompt file.")
    sp_prompt.add_argument("--id", required=True)
    sp_prompt.add_argument("--actor", default="cli")

    sp_probes = sub.add_parser("probes", help="List known probes.")
    sp_skills = sub.add_parser("skills", help="List allowlisted skills.")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root)

    if args.command == "list":
        orders = list_work_orders(root, status=args.status)
        if args.json:
            print(json.dumps(orders, indent=2, default=str))
        elif not orders:
            print("No work orders.")
        else:
            for o in orders:
                print(
                    f"{o['work_order_id']}  [{o['status']:>17}]  "
                    f"{o['probe_id']} → {o['skill_id']} ({o['mode']})  "
                    f"risk={o['risk_level']}  by={o['created_by']}"
                )
        return 0

    if args.command == "create":
        try:
            rec = create_work_order(
                root,
                probe_id=args.probe_id,
                skill_id=args.skill_id,
                mode=args.mode,
                created_by=args.created_by,
            )
        except policy.WorkOrderValidationError as exc:
            print(f"REJECTED: {exc}", file=sys.stderr)
            return 2
        print(f"Created {rec['work_order_id']} (status={rec['status']})")
        print(f"  requested_action: {rec['requested_action']}")
        if rec["approval_required"]:
            print("  approval required before any worker action.")
        return 0

    if args.command == "show":
        rec = get_work_order(root, args.id)
        if rec is None:
            print(f"Not found: {args.id}", file=sys.stderr)
            return 1
        print(json.dumps(rec, indent=2, default=str))
        return 0

    if args.command == "generate-prompt":
        rec = get_work_order(root, args.id)
        if rec is None:
            print(f"Not found: {args.id}", file=sys.stderr)
            return 1
        from operator_control.worker_prompts import generate_prompt

        path = generate_prompt(root, args.id, actor=args.actor)
        print(f"Wrote prompt: {path}")
        return 0

    if args.command == "probes":
        from operator_control.probe_registry import list_probes

        for p in list_probes():
            print(f"{p.probe_id:<35} [{p.source_view:<9}] → {p.recommended_skill_id}")
        return 0

    if args.command == "skills":
        from operator_control.skill_registry import list_skills

        for s in list_skills():
            print(f"{s.skill_id:<32} modes={','.join(s.allowed_modes)}")
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "create_work_order",
    "list_work_orders",
    "get_work_order",
    "transition_work_order",
    "attach_prompt_path",
    "main",
]
