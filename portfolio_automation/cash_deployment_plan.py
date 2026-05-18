"""
Cash Deployment Plan — observe-only advisor for excess cash + recurring
monthly contributions.

Reads:
    config.json                                — monthly_contribution,
                                                  target_cash_weight, cash_available
    outputs/latest/decision_plan.json          — ranked BUY/SCALE decisions
    outputs/latest/system_decision_summary.json (optional) — data health

Writes:
    outputs/latest/cash_deployment_plan.json
    outputs/latest/cash_deployment_plan.md

Hard guarantees:
    - observe_only=True hardcoded in every artifact.
    - Never mutates decision_plan or any score.
    - Never deploys when degraded_mode is true.
    - Caps suggested per-position deployment at allocation_engine
      max_position_cap (0.15).
    - Preserves a safety floor of 5% cash (matches config cash_reserve).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.cash_deployment_plan")

# ---------------------------------------------------------------------------
# Constants — mirror allocation_engine defaults; documented in spec.
# ---------------------------------------------------------------------------

_MAX_POSITION_PCT = 0.15          # mirrors allocation_engine max_position_cap
_SAFETY_FLOOR_PCT = 0.05          # never deploy below this cash level
_DEFAULT_TARGET_CASH = 0.05       # fallback when config.target_cash_weight missing

# Conviction-band sizing multipliers (mirror conviction.py DEFAULT_SIZING)
_BAND_MULTIPLIERS = {
    "high_conviction": 1.00,
    "normal":          0.50,
    "starter":         0.25,
    "observe":         0.00,
    "defer":           0.00,
}

# Decisions eligible for capital deployment from this layer
_ELIGIBLE_DECISIONS = frozenset({"BUY", "SCALE"})

# Max ranked decisions we'll consider for deployment in one cycle
_MAX_DECISIONS = 10

# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        result = float(v)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _load_json_safe(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Pure planning logic
# ---------------------------------------------------------------------------


def compute_available_cash(
    *,
    portfolio_value: float,
    cash_available: float,
    target_cash_pct: float,
    monthly_contribution: float,
    safety_floor_pct: float = _SAFETY_FLOOR_PCT,
) -> dict[str, Any]:
    """
    Return the deployable cash budget given the current state.

    Components:
      current_cash_pct  = cash / portfolio_value
      excess_cash_pct   = max(0, current_cash_pct - target_cash_pct)
      incoming_pct      = monthly_contribution / portfolio_value
      total_deployable_pct = max(0, excess + incoming - safety_floor_buffer)
                              when current_cash_pct already > safety_floor
      total_deployable_amount = total_deployable_pct * portfolio_value

    If portfolio_value <= 0 the plan reports zero deployable; safe for new
    accounts with no holdings yet.
    """
    if portfolio_value <= 0:
        return {
            "portfolio_value": portfolio_value,
            "cash_available": cash_available,
            "current_cash_pct": 0.0,
            "target_cash_pct": target_cash_pct,
            "excess_cash_pct": 0.0,
            "incoming_pct": 0.0,
            "total_deployable_pct": 0.0,
            "total_deployable_amount": 0.0,
            "below_safety_floor": False,
        }

    current_cash_pct = cash_available / portfolio_value
    excess_cash_pct = max(0.0, current_cash_pct - target_cash_pct)
    incoming_pct = monthly_contribution / portfolio_value if monthly_contribution > 0 else 0.0

    # Cash that would push us below safety floor cannot be deployed.
    below_safety_floor = current_cash_pct < safety_floor_pct
    if below_safety_floor:
        # Recurring contribution is still available net of the floor refill
        refill_needed = max(0.0, safety_floor_pct - current_cash_pct)
        total_deployable_pct = max(0.0, incoming_pct - refill_needed)
    else:
        total_deployable_pct = excess_cash_pct + incoming_pct

    return {
        "portfolio_value": round(portfolio_value, 2),
        "cash_available": round(cash_available, 2),
        "current_cash_pct": round(current_cash_pct, 4),
        "target_cash_pct": round(target_cash_pct, 4),
        "excess_cash_pct": round(excess_cash_pct, 4),
        "incoming_pct": round(incoming_pct, 4),
        "total_deployable_pct": round(total_deployable_pct, 4),
        "total_deployable_amount": round(total_deployable_pct * portfolio_value, 2),
        "below_safety_floor": below_safety_floor,
    }


def rank_deployable_decisions(
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Return BUY/SCALE rows sorted by priority desc, capped at _MAX_DECISIONS.

    Non-eligible decisions (HOLD/WAIT/AVOID/SELL) are filtered out so the
    output focuses only on capital deployment candidates.
    """
    eligible = [
        d for d in (decisions or [])
        if isinstance(d, dict)
        and _safe_str(d.get("decision")).upper() in _ELIGIBLE_DECISIONS
    ]
    eligible.sort(
        key=lambda d: _safe_float(d.get("priority")) or 0.0,
        reverse=True,
    )
    return eligible[:_MAX_DECISIONS]


def _band_multiplier(band: str | None) -> float:
    if not band:
        return 0.50  # treat unknown as normal-band size
    return _BAND_MULTIPLIERS.get(_safe_str(band).lower(), 0.50)


def allocate_deployment(
    *,
    deployable_amount: float,
    portfolio_value: float,
    ranked_decisions: list[dict[str, Any]],
    max_position_pct: float = _MAX_POSITION_PCT,
) -> list[dict[str, Any]]:
    """
    Distribute *deployable_amount* across *ranked_decisions* respecting:
      - the decision's own recommended_allocation_pct ceiling
      - the per-position cap (max_position_pct of portfolio_value)
      - the conviction-band sizing multiplier
      - the running remaining budget

    Each decision either receives a positive allocation or appears with
    suggested_amount=0 and a `skipped_reason` string. No decision is
    dropped silently — caller can see why.
    """
    rows: list[dict[str, Any]] = []
    remaining = deployable_amount
    pos_cap_amount = max_position_pct * portfolio_value

    for d in ranked_decisions:
        symbol = _safe_str(d.get("symbol")).upper()
        decision = _safe_str(d.get("decision")).upper()
        priority = _safe_float(d.get("priority")) or 0.0
        recommended_pct = _safe_float(d.get("recommended_allocation_pct"))
        band = _safe_str((d.get("inputs_used") or {}).get("conviction_band") or
                         d.get("conviction_band"))

        if remaining <= 0:
            rows.append({
                "symbol": symbol,
                "decision": decision,
                "priority": priority,
                "suggested_amount": 0.0,
                "suggested_pct": 0.0,
                "skipped_reason": "budget exhausted",
            })
            continue

        # Start from the decision's own recommended allocation if provided,
        # otherwise fall back to band-derived sizing of 1% of portfolio.
        if recommended_pct is not None and recommended_pct > 0:
            base_amount = recommended_pct * portfolio_value
        else:
            base_amount = 0.01 * portfolio_value

        sized = base_amount * _band_multiplier(band)
        capped_amount = min(sized, pos_cap_amount, remaining)
        capped_amount = round(max(0.0, capped_amount), 2)

        if capped_amount <= 0:
            rows.append({
                "symbol": symbol,
                "decision": decision,
                "priority": priority,
                "suggested_amount": 0.0,
                "suggested_pct": 0.0,
                "skipped_reason": "sized to zero by band multiplier or cap",
            })
            continue

        rows.append({
            "symbol": symbol,
            "decision": decision,
            "priority": priority,
            "conviction_band": band or "unknown",
            "suggested_amount": capped_amount,
            "suggested_pct": round(capped_amount / portfolio_value, 4)
                             if portfolio_value > 0 else 0.0,
            "skipped_reason": None,
        })
        remaining -= capped_amount

    return rows


# ---------------------------------------------------------------------------
# Plan envelope
# ---------------------------------------------------------------------------


def build_plan(
    *,
    cash_summary: dict[str, Any],
    deployment_rows: list[dict[str, Any]],
    degraded_mode: bool,
    data_mode: str,
    notes: list[str],
) -> dict[str, Any]:
    total_deployed = round(
        sum(r.get("suggested_amount", 0.0) for r in deployment_rows), 2
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "degraded_mode": bool(degraded_mode),
        "data_mode": data_mode or "unknown",
        "cash_summary": cash_summary,
        "deployment_rows": deployment_rows,
        "total_deployed_amount": total_deployed,
        "remaining_budget": round(
            max(0.0, cash_summary.get("total_deployable_amount", 0.0) - total_deployed),
            2,
        ),
        "summary_line": (
            f"Cash deployment: ${total_deployed:.2f} across "
            f"{sum(1 for r in deployment_rows if r.get('suggested_amount', 0) > 0)} "
            f"position(s); budget ${cash_summary.get('total_deployable_amount', 0.0):.2f}"
        ),
        "notes": list(notes),
    }


def _render_markdown(plan: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Cash Deployment Plan")
    lines.append("")
    lines.append(f"_Generated: {plan.get('generated_at')}_")
    lines.append("")
    lines.append("Observe-only. No trades are executed.")
    lines.append("")
    lines.append(plan.get("summary_line", ""))
    lines.append("")
    cs = plan.get("cash_summary", {})
    lines.append("## Cash budget")
    lines.append("")
    lines.append(f"- Portfolio value: ${cs.get('portfolio_value', 0):,.2f}")
    lines.append(f"- Cash available: ${cs.get('cash_available', 0):,.2f} "
                 f"({(cs.get('current_cash_pct') or 0):.1%})")
    lines.append(f"- Target cash %: {(cs.get('target_cash_pct') or 0):.1%}")
    lines.append(f"- Excess cash %: {(cs.get('excess_cash_pct') or 0):.1%}")
    lines.append(f"- Incoming 30d %: {(cs.get('incoming_pct') or 0):.1%}")
    lines.append(f"- Deployable: ${(cs.get('total_deployable_amount') or 0):,.2f}")
    lines.append("")
    if plan.get("notes"):
        lines.append("## Notes")
        for n in plan["notes"]:
            lines.append(f"- {n}")
        lines.append("")
    lines.append("## Deployment plan")
    lines.append("")
    if not plan.get("deployment_rows"):
        lines.append("_No eligible BUY/SCALE decisions in current plan._")
    else:
        lines.append("| Symbol | Decision | Priority | Band | Amount | % | Note |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in plan["deployment_rows"]:
            amt = r.get("suggested_amount", 0)
            lines.append(
                "| {sym} | {dec} | {pri:.3f} | {band} | ${amt:,.2f} | {pct} | {note} |".format(
                    sym=r.get("symbol", "?"),
                    dec=r.get("decision", "?"),
                    pri=r.get("priority", 0) or 0,
                    band=r.get("conviction_band", "—"),
                    amt=amt,
                    pct=(f"{r.get('suggested_pct', 0):.1%}" if amt > 0 else "—"),
                    note=r.get("skipped_reason") or "",
                )
            )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def _portfolio_value_from_context(
    decision_plan_payload: dict[str, Any],
    cfg: dict[str, Any],
) -> float:
    # 1) Preferred: top-level portfolio_context on the decision_plan envelope
    #    (main.py writes this since 2026-05-15).
    top_pc = decision_plan_payload.get("portfolio_context") or {}
    v = _safe_float(top_pc.get("total_portfolio_value"))
    if v and v > 0:
        return v
    # 2) Fallback: inputs_used.portfolio_context on any decision row (older runs)
    for d in decision_plan_payload.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        iu = d.get("inputs_used") or {}
        pc = iu.get("portfolio_context") or {}
        v = _safe_float(pc.get("total_portfolio_value"))
        if v and v > 0:
            return v
    # 3) Last resort: cash-only fallback. Calling code treats current_cash_pct
    #    as 100% in this branch, which is correct given we have no other info.
    cash = _safe_float((cfg.get("portfolio") or {}).get("cash_available")) or 0.0
    return cash


def run_cash_deployment_plan(
    repo_root: Path | str,
    *,
    base_dir: Path | str = "outputs",
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    base_dir = Path(base_dir)

    cfg = _load_json_safe(repo_root / "config.json")
    decision_plan_path = base_dir / "latest" / "decision_plan.json"
    decision_plan_payload = _load_json_safe(decision_plan_path)
    system_summary = _load_json_safe(base_dir / "latest" / "system_decision_summary.json")

    portfolio_cfg = cfg.get("portfolio") or {}
    monthly_contribution = _safe_float(portfolio_cfg.get("monthly_contribution")) or 0.0
    cash_available = _safe_float(portfolio_cfg.get("cash_available")) or 0.0
    target_cash_pct = _safe_float(portfolio_cfg.get("target_cash_weight")) or _DEFAULT_TARGET_CASH

    portfolio_value = _portfolio_value_from_context(decision_plan_payload, cfg)

    cash_summary = compute_available_cash(
        portfolio_value=portfolio_value,
        cash_available=cash_available,
        target_cash_pct=target_cash_pct,
        monthly_contribution=monthly_contribution,
    )

    data_health = (system_summary.get("data_health") or {})
    degraded_mode = bool(data_health.get("degraded_mode", False))
    data_mode = _safe_str(data_health.get("data_mode")) or "unknown"

    notes: list[str] = []
    deployment_rows: list[dict[str, Any]] = []

    if degraded_mode:
        notes.append("degraded_mode active — deployment suspended this cycle")
    elif cash_summary["total_deployable_amount"] <= 0:
        notes.append(
            "no deployable cash this cycle "
            "(below target_cash_weight or safety floor)"
        )
    elif portfolio_value <= 0:
        notes.append("portfolio_value unavailable — cannot size positions")
    else:
        ranked = rank_deployable_decisions(
            decision_plan_payload.get("decisions") or []
        )
        if not ranked:
            notes.append("no BUY/SCALE decisions in current decision_plan")
        deployment_rows = allocate_deployment(
            deployable_amount=cash_summary["total_deployable_amount"],
            portfolio_value=portfolio_value,
            ranked_decisions=ranked,
        )

    plan = build_plan(
        cash_summary=cash_summary,
        deployment_rows=deployment_rows,
        degraded_mode=degraded_mode,
        data_mode=data_mode,
        notes=notes,
    )

    try:
        safe_write_json(
            OutputNamespace.LATEST,
            "cash_deployment_plan.json",
            plan,
            base_dir=base_dir,
        )
        safe_write_text(
            OutputNamespace.LATEST,
            "cash_deployment_plan.md",
            _render_markdown(plan),
            base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning(
            "cash_deployment_plan: failed to write artifacts (non-fatal): %s", exc
        )

    return plan
