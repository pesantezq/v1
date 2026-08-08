"""Capital-authority consistency gate — observe-only.

The system has two internally-coherent capital authorities that nothing
reconciled:

* ``outputs/latest/decision_plan.json`` — the declared decision source of truth.
  Each row carries ``recommended_amount`` (sizing math from ``adjustment.py``)
  rendered by ``decision_engine._build_legacy_capital_action`` into an
  IMPERATIVE sentence: "Scale existing position — add about $1,588."  That
  sentence is built from the decision and the amount alone. It has no reference
  to cash, reserve, pacing, or deployable capital.
* ``outputs/latest/daily_capital_plan.json`` — the funding authority, which
  applies the capital waterfall and pacing and may legitimately fund nothing.

Observed live 2026-08-08: VFH carried "add about $1,588." while the capital plan
carried ``funded_actions: []`` and "No capital is funded for deployment today
($0 available after pacing)". Any investor-facing consumer that rendered
``capital_action`` verbatim therefore issued a funded-sounding dollar
instruction the capital layer had already denied.

This module does NOT recompute decisions, does not resize anything, and does not
change protected score semantics. It reads both authorities and reports whether
they can be shown to the investor together. When they disagree it fails CLOSED
with ``BLOCKED_BY_CONSISTENCY`` rather than choosing which instruction the
operator should follow.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
SOURCE = "decision_authority"

STATUS_CONSISTENT = "CONSISTENT"
STATUS_BLOCKED = "BLOCKED_BY_CONSISTENCY"
STATUS_INSUFFICIENT = "INSUFFICIENT_DATA"

# Decisions whose capital_action consumes DEPLOYABLE capital. SELL/TRIM release
# capital rather than consuming it, and WAIT/HOLD/AVOID explicitly stand down —
# a sizing hint on a stand-down row is not an instruction to deploy. Live data
# carries recommended_amount=105.85 on WAIT rows whose rendered sentence is
# "Stand by — do not deploy capital until conditions improve."
DEPLOYING_DECISIONS = frozenset({"BUY", "SCALE"})

# capital_action renders whole dollars ("about $106" for 105.85), so a sub-dollar
# delta between the two authorities is presentation, not disagreement.
AMOUNT_TOLERANCE = 1.0

_DISCLAIMER = (
    "Observe-only consistency gate. Compares the decision plan's rendered capital "
    "instructions against the funding authority; never recomputes decisions, "
    "never resizes, never executes, and never writes decision_plan.json."
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _envelope(status: str, **extra: Any) -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "no_trade": True,
        "status": status,
        "conflicts": [],
        "funded_symbols": [],
        "instructed_symbols": [],
        "insufficient_reason": None,
        "disclaimer": _DISCLAIMER,
    }
    payload.update(extra)
    return payload


def _capital_plan_usable(capital_plan: Any) -> bool:
    """An absent, empty, or explicitly unavailable capital plan is NOT agreement.

    ``available: false`` means the funding authority could not compute — treating
    that as "nothing funded, so nothing conflicts" would turn a degraded run into
    a clean bill of health, which is the failure mode this module exists to stop.
    """
    if not isinstance(capital_plan, dict) or not capital_plan:
        return False
    return capital_plan.get("available") is not False


def _deploying_instructions(decision_plan: dict) -> list[dict]:
    """Rows whose rendered capital_action tells the investor to deploy money."""
    out: list[dict] = []
    for row in decision_plan.get("decisions") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("decision") or "").upper() not in DEPLOYING_DECISIONS:
            continue
        amount = _num(row.get("recommended_amount"))
        if not amount or amount <= 0:
            continue
        out.append({
            "symbol": row.get("symbol"),
            "decision": str(row.get("decision") or "").upper(),
            "amount": amount,
            "instruction": str(row.get("capital_action") or ""),
        })
    return out


def _funded_map(capital_plan: dict) -> dict[str, float]:
    funded: dict[str, float] = {}
    for row in capital_plan.get("funded_actions") or []:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        amount = _num(row.get("funded_capital")) or 0.0
        if symbol and amount > 0:
            funded[symbol] = funded.get(symbol, 0.0) + amount
    return funded


def reconcile_capital_authority(decision_plan: Any, capital_plan: Any) -> dict:
    """Compare the two capital authorities. Pure over its inputs.

    Returns ``BLOCKED_BY_CONSISTENCY`` when the decision plan tells the investor
    to deploy money the funding authority has not funded, or funds a materially
    different amount. Returns ``INSUFFICIENT_DATA`` — never ``CONSISTENT`` — when
    either authority is missing or degraded.
    """
    missing = []
    if not isinstance(decision_plan, dict) or not decision_plan:
        missing.append("decision_plan")
    if not _capital_plan_usable(capital_plan):
        missing.append("capital_plan")
    if missing:
        return _envelope(
            STATUS_INSUFFICIENT,
            insufficient_reason=f"missing_or_unavailable:{','.join(missing)}",
            provenance={"decision_plan_run_id": None,
                        "decision_plan_generated_at": None,
                        "capital_plan_generated_at": None},
        )

    instructions = _deploying_instructions(decision_plan)
    funded = _funded_map(capital_plan)

    conflicts: list[dict] = []
    for item in instructions:
        funded_amount = funded.get(item["symbol"], 0.0)
        if funded_amount <= 0:
            kind = "unfunded_capital_instruction"
        elif abs(funded_amount - item["amount"]) > AMOUNT_TOLERANCE:
            kind = "amount_disagreement"
        else:
            continue
        conflicts.append({
            "symbol": item["symbol"],
            "decision": item["decision"],
            "kind": kind,
            "decision_plan_amount": item["amount"],
            "capital_plan_funded": funded_amount,
            "instruction": item["instruction"],
            "detail": (
                f"decision_plan instructs deploying ${item['amount']:,.2f} to "
                f"{item['symbol']}, but the funding authority funded "
                f"${funded_amount:,.2f}"
            ),
        })

    return _envelope(
        STATUS_BLOCKED if conflicts else STATUS_CONSISTENT,
        conflicts=conflicts,
        funded_symbols=sorted(funded),
        instructed_symbols=sorted(i["symbol"] for i in instructions if i["symbol"]),
        capital_plan_bottom_line=capital_plan.get("bottom_line"),
        provenance={
            "decision_plan_run_id": decision_plan.get("run_id"),
            "decision_plan_generated_at": decision_plan.get("generated_at"),
            "capital_plan_generated_at": capital_plan.get("generated_at"),
        },
    )


def _read_json(path: Path) -> Any:
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_decision_authority(root: str) -> dict:
    """Read both live authorities from ``<root>/outputs/latest`` and reconcile."""
    latest = Path(root) / "outputs" / "latest"
    return reconcile_capital_authority(
        _read_json(latest / "decision_plan.json"),
        _read_json(latest / "daily_capital_plan.json"),
    )


def write_decision_authority(result: dict, root: str) -> str:
    """Write the JSON artifact via OutputNamespace.LATEST. Returns its path."""
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json

    return safe_write_json(
        OutputNamespace.LATEST, "decision_authority.json", result,
        base_dir=Path(root) / "outputs",
    )
