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
# The decision artifact carries unconstrained sizing, but NO investor-facing
# surface presents it as deploy-now capital. This is the architecturally correct
# steady state while `capital_action` remains a legacy sizing field: the number
# exists as research/rebalance context and is never rendered as an instruction.
# Graded GREEN, but the fact is surfaced rather than swallowed — verified
# 2026-08-08 that daily_memo labels it "NOT a spend-today budget".
STATUS_CONSISTENT_UNCONSTRAINED = "CONSISTENT_WITH_UNCONSTRAINED_SIZING"
STATUS_BLOCKED = "BLOCKED_BY_CONSISTENCY"
STATUS_INSUFFICIENT = "INSUFFICIENT_DATA"

GREEN_STATUSES = frozenset({STATUS_CONSISTENT, STATUS_CONSISTENT_UNCONSTRAINED})

# Funded-sounding imperative money language. A rendered investor surface matching
# any of these for an unfunded symbol is the actual defect: the artifact holding
# a sizing number is not, by itself, an instruction to the operator.
_INSTRUCTION_PATTERNS = (
    r"deploy about \$",
    r"add about \$",
    r"trim about \$",
    r"deploy \$[\d,]",
    r"buy \$[\d,]",
    r"scale .{0,20}by \$[\d,]",
    r"invest \$[\d,]",
)

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


def find_rendered_instructions(surfaces: Any) -> list[dict]:
    """Funded-sounding money language found in investor-facing rendered output.

    `surfaces` is an iterable of ``{"name": str, "text": str}``. This is the
    CONSUMER boundary — the only place a number becomes an instruction to the
    operator. Grading the raw artifact instead would mean a permanent RED for as
    long as `capital_action` exists as a legacy sizing field, which trains the
    operator to ignore the gate.
    """
    import re

    found: list[dict] = []
    for surface in surfaces or []:
        if not isinstance(surface, dict):
            continue
        text = str(surface.get("text") or "")
        for pattern in _INSTRUCTION_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                start = max(0, match.start() - 60)
                found.append({
                    "surface": surface.get("name"),
                    "match": text[match.start():match.end() + 20].strip(),
                    "context": text[start:match.end() + 20].strip(),
                })
    return found


def reconcile_capital_authority(decision_plan: Any, capital_plan: Any,
                                rendered_surfaces: Any = None) -> dict:
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

    # The conflicts above are ARTIFACT-level: the decision plan holds sizing the
    # funding authority did not fund. That is expected while `capital_action` is
    # a legacy unconstrained-sizing field, and is NOT by itself a contradiction
    # the operator ever sees. It becomes a real defect only when an investor
    # surface renders it as deploy-now money.
    rendered = find_rendered_instructions(rendered_surfaces)
    unfunded_symbols = {c["symbol"] for c in conflicts}
    leaked = [
        r for r in rendered
        if not funded or any(s and s in r["context"] for s in unfunded_symbols)
    ]

    if leaked:
        status = STATUS_BLOCKED
    elif conflicts:
        status = STATUS_CONSISTENT_UNCONSTRAINED
    else:
        status = STATUS_CONSISTENT

    return _envelope(
        status,
        unconstrained_sizing=conflicts,
        rendered_instruction_leaks=leaked,
        surfaces_checked=[s.get("name") for s in (rendered_surfaces or [])
                          if isinstance(s, dict)],
        conflicts=leaked if leaked else [],
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


# Investor-facing rendered surfaces, checked at the consumer boundary. Only
# products a human actually reads belong here. gui/app.py (Streamlit) is retired
# per docs/STREAMLIT_RETIREMENT.md and no gui_v2 template renders capital_actions,
# so neither is a live surface today — verified 2026-08-08.
INVESTOR_SURFACES = ("daily_memo.md", "daily_memo.txt", "decision_plan.md",
                     "cash_deployment_plan.md")


def _load_surfaces(latest: Path) -> list[dict]:
    surfaces = []
    for name in INVESTOR_SURFACES:
        path = latest / name
        try:
            surfaces.append({"name": name, "text": path.read_text(encoding="utf-8")})
        except Exception:
            continue
    return surfaces


def run_decision_authority(root: str) -> dict:
    """Read both live authorities plus the rendered investor surfaces."""
    latest = Path(root) / "outputs" / "latest"
    return reconcile_capital_authority(
        _read_json(latest / "decision_plan.json"),
        _read_json(latest / "daily_capital_plan.json"),
        rendered_surfaces=_load_surfaces(latest),
    )


def write_decision_authority(result: dict, root: str) -> str:
    """Write the JSON artifact via OutputNamespace.LATEST. Returns its path."""
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json

    return safe_write_json(
        OutputNamespace.LATEST, "decision_authority.json", result,
        base_dir=Path(root) / "outputs",
    )
