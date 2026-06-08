# portfolio_automation/brokers/broker_reconciliation.py
"""Pure reconciliation of a Schwab snapshot vs local config.json, plus a
PROPOSAL-ONLY config-update artifact. No config writes. Observe-only."""
from __future__ import annotations

_QTY_EPS = 1e-6
_CASH_EPS = 0.01


def _local_holdings(config: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for h in ((config.get("portfolio") or {}).get("holdings") or []):
        if isinstance(h, dict) and h.get("symbol"):
            out[str(h["symbol"]).upper()] = h
    return out


def reconcile(snapshot: dict, positions: dict, config: dict) -> dict:
    schwab = {str(p.get("symbol", "")).upper(): p
              for p in (positions.get("positions") or []) if p.get("symbol")}
    local = _local_holdings(config)
    matched, mismatches, missing_local, missing_schwab = [], [], [], []
    for sym in sorted(set(schwab) | set(local)):
        sp, lp = schwab.get(sym), local.get(sym)
        if sp and lp:
            sq = float(sp.get("quantity") or 0.0)
            lq = float(lp.get("shares") or 0.0)
            if abs(sq - lq) < _QTY_EPS:
                matched.append({"symbol": sym, "schwab_qty": sq, "local_shares": lq})
            else:
                mismatches.append({"symbol": sym, "schwab_qty": sq, "local_shares": lq,
                                   "delta": round(sq - lq, 6)})
        elif sp and not lp:
            missing_local.append({"symbol": sym, "schwab_qty": float(sp.get("quantity") or 0.0)})
        else:
            missing_schwab.append({"symbol": sym, "local_shares": float(lp.get("shares") or 0.0)})

    schwab_cash = float((snapshot.get("totals") or {}).get("cash") or 0.0)
    local_cash = float((config.get("portfolio") or {}).get("cash_available") or 0.0)
    cash_delta = round(schwab_cash - local_cash, 2)

    has_broker = bool(schwab) or bool((snapshot.get("totals") or {}).get("market_value"))
    has_local = bool(local)
    if not has_broker:
        summary = "no_broker_data"
    elif not has_local:
        summary = "no_local_config"
    elif mismatches or missing_local or missing_schwab or abs(cash_delta) >= _CASH_EPS:
        summary = "mismatch"
    else:
        summary = "ok"

    n_diff = len(mismatches) + len(missing_local) + len(missing_schwab)
    msg = {
        "ok": "Schwab and local config agree. No review needed.",
        "no_broker_data": "No Schwab data available — run --sync first.",
        "no_local_config": "No local holdings configured to compare against.",
        "mismatch": (f"Review {n_diff} holding difference(s)"
                     + (f" and a ${abs(cash_delta):.2f} cash difference" if abs(cash_delta) >= _CASH_EPS else "")
                     + ". Generate a config-update proposal to align local config to Schwab reality."),
    }[summary]

    return {
        "generated_at": snapshot.get("generated_at"), "observe_only": True, "source": "schwab",
        "summary_status": summary,
        "matched": matched, "quantity_mismatches": mismatches,
        "missing_in_local": missing_local, "missing_in_schwab": missing_schwab,
        "cash": {"schwab": schwab_cash, "local": local_cash, "delta": cash_delta},
        "target_allocation_comparison": None,
        "operator_review_message": msg,
    }


def validate_proposed_holdings(holdings: list[dict], cash: float, config: dict) -> dict:
    errors: list[str] = []
    if cash is not None and float(cash) < 0:
        errors.append(f"negative cash: {cash}")
    seen = set()
    for h in holdings or []:
        sym = str(h.get("symbol") or "").strip()
        if not sym:
            errors.append("missing/empty symbol field in a holding")
            continue
        if sym in seen:
            errors.append(f"duplicate symbol: {sym}")
        seen.add(sym)
        if float(h.get("shares") or 0) < 0:
            errors.append(f"negative shares for {sym}")
    # Concentration/leverage caps are enforced at allocation/decision time (decision engine / allocation policy),
    # NOT on a shares-to-broker-reality sync proposal — this validator only guards data sanity
    # (negatives, missing/dup symbols, target-weight sum).
    # target weights sum check only if any target_weight present
    tws = [float(h["target_weight"]) for h in (holdings or []) if h.get("target_weight") is not None]
    if tws and abs(sum(tws) - 1.0) > 0.02:
        errors.append(f"target weights sum to {sum(tws):.3f}, expected ~1.0")
    return {"ok": not errors, "errors": errors}


def build_proposal(reconciliation: dict, config: dict, *, now_iso: str) -> dict:
    """PROPOSAL ONLY — never writes config.json. Aligns local holdings/cash toward
    Schwab reality; operator applies via tools/manual_portfolio_update.py."""
    local = _local_holdings(config)
    before_holdings = [dict(h) for h in local.values()]
    before_cash = float((config.get("portfolio") or {}).get("cash_available") or 0.0)

    after = {sym: dict(h) for sym, h in local.items()}
    for m in reconciliation.get("quantity_mismatches", []):
        after.setdefault(m["symbol"], {"symbol": m["symbol"]})["shares"] = m["schwab_qty"]
    for m in reconciliation.get("missing_in_local", []):
        after[m["symbol"]] = {"symbol": m["symbol"], "shares": m["schwab_qty"]}
    after_holdings = list(after.values())
    after_cash = reconciliation.get("cash", {}).get("schwab", before_cash)

    missing_in_schwab = reconciliation.get("missing_in_schwab") or []
    reason = "Align local StockBot config to Schwab actual holdings/cash (observe-only)."
    if missing_in_schwab:
        retained = ", ".join(sorted(m["symbol"] for m in missing_in_schwab))
        reason += (
            f" Local-only holdings absent from Schwab ({retained}) are retained for"
            " operator review (not auto-removed)."
        )

    validation = validate_proposed_holdings(after_holdings, after_cash, config)
    return {
        "generated_at": now_iso, "observe_only": True, "source": "schwab",
        "source_snapshot_timestamp": reconciliation.get("generated_at"),
        "before": {"holdings": before_holdings, "cash": before_cash},
        "proposed_after": {"holdings": after_holdings, "cash": after_cash},
        "reason": reason,
        "validation": validation,
        "operator_approval_required": True,
        "auto_applied": False,
        "apply_instructions": ("Reviewed manual step only: apply via "
                               "`python -m tools.manual_portfolio_update` (backup+audit+validate). "
                               "This proposal performs NO writes and NO trades."),
    }
