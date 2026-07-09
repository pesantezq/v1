"""Today cockpit: answers system-healthy? decision-core OK? review needed? what changed? memo?"""
from __future__ import annotations
from pathlib import Path
from gui_v2.data.shared import card, _read_json, weekly_deployment_view


def collect_today_view(root: Path) -> dict:
    latest = Path(root) / "outputs/latest"
    drs = _read_json(latest / "daily_run_status.json") or {}
    dp = _read_json(latest / "decision_plan.json")
    risk = _read_json(latest / "risk_delta.json") or {}
    cash = _read_json(latest / "cash_deployment_plan.json") or {}
    cards = []

    # System health card
    drs_status = (drs.get("overall_status") or "unknown")
    if drs_status == "ok":
        health_status = "ok"
    elif drs_status == "ok_with_warnings":
        health_status = "warning"
    elif drs_status in ("failed", "partial"):
        health_status = "red"
    else:
        health_status = "unknown"

    stage_ok = (drs.get("stage_summary") or {}).get("ok", "?")
    cards.append(card(
        "System health",
        status=health_status,
        label=drs_status,
        summary=f"{stage_ok} stages OK",
        source_artifacts=["daily_run_status.json"],
        updated_at=drs.get("generated_at"),
    ))

    # Decision core card — check is not None (an empty dict {} is still "present")
    dp_present = dp is not None
    cards.append(card(
        "Decision core",
        status="ok" if dp_present else "red",
        label="present" if dp_present else "MISSING",
        summary=(
            "decision_plan present"
            if dp_present
            else "decision_plan.json absent — decisions not trustworthy"
        ),
        source_artifacts=["decision_plan.json"],
    ))

    # Risk card
    risk_overall = risk.get("overall_status") or "unknown"
    cards.append(card(
        "Risk",
        status="ok" if risk_overall == "ok" else "warning",
        label=risk_overall,
        summary="see Portfolio view",
        source_artifacts=["risk_delta.json"],
    ))

    # Capital card — the one deployable-this-week figure at a glance (feature
    # 2026-07-07). Observe-only; reads the envelope, never recomputes.
    wd = weekly_deployment_view(cash)
    if wd.get("available"):
        weekly = wd.get("weekly_remaining")
        cadence = wd.get("deploy_cadence")
        if cadence and cadence != "monthly" and weekly is not None:
            cap_label = f"${weekly:,.2f} this week"
            cap_summary = (
                f"of ${wd.get('weekly_tranche') or 0:,.2f} tranche · "
                f"cycle ${wd.get('net_investable') or 0:,.2f} net-investable"
            )
        else:
            cap_label = f"${wd.get('net_investable') or 0:,.2f} this cycle"
            cap_summary = f"reserve ${wd.get('reserve') or 0:,.2f} protected"
        cap_status = "warning" if wd.get("history_status") == "unavailable" else "info"
    else:
        cap_label = "unavailable"
        cap_summary = "no monthly capital envelope in current plan"
        cap_status = "unknown"
    cards.append(card(
        "Deployable capital",
        status=cap_status,
        label=cap_label,
        summary=cap_summary,
        source_artifacts=["cash_deployment_plan.json"],
        updated_at=cash.get("generated_at") if cash else None,
    ))

    # Decision triage — verb-free workload summary at a glance. Action verbs
    # (SCALE/BUY/...) stay on the Portfolio advisory decision queue per the
    # observe-only contract; this card surfaces only the bucket counts.
    triage = _read_json(latest / "decision_triage.json")
    if triage and triage.get("available"):
        bc = triage.get("bucket_counts") or {}
        crit = int(bc.get("critical_action") or 0)
        act = int(bc.get("action_candidate") or 0)
        if crit > 0:
            t_status = "red"
        elif act > 0:
            t_status = "warning"
        else:
            t_status = "ok"
        total = triage.get("total_decisions") or 0
        cards.append(card(
            "Decision triage",
            status=t_status,
            label=f"{total} decisions",
            summary=triage.get("summary_line") or (
                f"{crit} critical, {act} action candidate(s), "
                f"{int(bc.get('monitor') or 0)} monitor, "
                f"{int(bc.get('ignore_for_now') or 0)} ignore"),
            source_artifacts=["decision_triage.json"],
            updated_at=triage.get("generated_at"),
        ))

    return {"cards": cards, "persona": "today", "observe_only": True}
