"""
Tax-Loss Harvest Advisor — observe-only.

Surfaces holdings sitting at unrealized losses that would generate a tax
deduction if sold today, alongside an optional replacement candidate to
preserve exposure during the 30-day wash-sale window.

Hard constraints:
  - This module produces advisory NOTES ONLY. It never recommends a SELL
    decision into the decision plan and never modifies allocations.
  - Activates only when config.json portfolio.is_taxable_account is true.
  - Skips when entry/cost basis is unknown for a symbol (cannot compute
    unrealized loss).

Inputs (read-only):
  - config.json portfolio.holdings (symbols, shares, optional cost_basis)
  - config.json portfolio.is_taxable_account flag
  - FMP historical prices (optional; for current price)
  - tax_replacement_map (optional): {sold_symbol: replacement_symbol[]}
    for like-exposure suggestions (e.g. QQQ → VGT). Default: empty;
    operator decides replacement.

Outputs (LATEST namespace):
  - outputs/latest/tax_harvest_advisor.json
  - outputs/latest/tax_harvest_advisor.md

Hard guarantees:
  - observe_only=True hardcoded.
  - Never raises into pipeline.
  - Never writes a SELL decision anywhere.
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

logger = logging.getLogger("stockbot.portfolio_automation.tax_harvest_advisor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum unrealized loss in dollars before flagging for harvest.
# Below this it isn't worth the tax-prep friction.
_MIN_LOSS_DOLLARS = 25.0

# Loss percentage threshold for "material harvest opportunity" label.
_MATERIAL_LOSS_PCT = 0.05  # 5% drop from cost basis

# Default like-exposure replacement map. Empty by design — the operator
# decides what counts as substantially-identical for IRS purposes.
DEFAULT_REPLACEMENT_MAP: dict[str, list[str]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
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
# Pure logic
# ---------------------------------------------------------------------------


def evaluate_position(
    *,
    symbol: str,
    shares: float | None,
    cost_basis: float | None,
    current_price: float | None,
    replacement_map: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """
    Return a tax-loss-harvest row for one position.

    Status values:
      - ok: full info available
      - missing_cost_basis: cannot compute unrealized P&L
      - missing_price: no current price available
      - no_loss: current_price >= cost_basis (gain or break-even)
      - sub_minimum: loss below _MIN_LOSS_DOLLARS threshold
    """
    if shares is None or shares <= 0:
        return {
            "symbol": symbol,
            "status": "no_position",
            "harvest_recommended": False,
            "loss_dollars": None,
            "loss_pct": None,
        }
    if cost_basis is None or cost_basis <= 0:
        return {
            "symbol": symbol,
            "status": "missing_cost_basis",
            "harvest_recommended": False,
            "loss_dollars": None,
            "loss_pct": None,
            "notes": ["cost_basis missing in config.json — cannot evaluate"],
        }
    if current_price is None or current_price <= 0:
        return {
            "symbol": symbol,
            "status": "missing_price",
            "harvest_recommended": False,
            "loss_dollars": None,
            "loss_pct": None,
        }
    loss_per_share = cost_basis - current_price
    loss_dollars = round(loss_per_share * shares, 2)
    loss_pct = round(loss_per_share / cost_basis, 4)

    if loss_dollars <= 0:
        return {
            "symbol": symbol,
            "status": "no_loss",
            "harvest_recommended": False,
            "loss_dollars": loss_dollars,
            "loss_pct": loss_pct,
        }
    if loss_dollars < _MIN_LOSS_DOLLARS:
        return {
            "symbol": symbol,
            "status": "sub_minimum",
            "harvest_recommended": False,
            "loss_dollars": loss_dollars,
            "loss_pct": loss_pct,
            "notes": [
                f"unrealized loss ${loss_dollars:.2f} below "
                f"${_MIN_LOSS_DOLLARS:.0f} threshold"
            ],
        }

    notes: list[str] = []
    if loss_pct >= _MATERIAL_LOSS_PCT:
        notes.append("material loss — strongest harvest candidate")
    notes.append(
        "wash-sale window: do not buy substantially identical security for 30 days"
    )

    replacement_candidates: list[str] = []
    if replacement_map:
        replacement_candidates = list(replacement_map.get(symbol, []) or [])

    return {
        "symbol": symbol,
        "status": "ok",
        "harvest_recommended": True,
        "loss_dollars": loss_dollars,
        "loss_pct": loss_pct,
        "cost_basis": round(cost_basis, 4),
        "current_price": round(current_price, 4),
        "shares": shares,
        "replacement_candidates": replacement_candidates,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Plan envelope
# ---------------------------------------------------------------------------


def build_plan(
    *,
    is_taxable: bool,
    rows: list[dict[str, Any]],
    notes: list[str],
) -> dict[str, Any]:
    if not is_taxable:
        summary_line = "Tax harvest advisor: skipped (account is not taxable)"
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "observe_only": True,
            "schema_version": "1",
            "is_taxable_account": False,
            "summary_line": summary_line,
            "positions": [],
            "harvestable_count": 0,
            "total_harvestable_loss_dollars": 0.0,
            "notes": list(notes),
        }
    harvestable = [r for r in rows if r.get("harvest_recommended")]
    total_loss = round(sum(r.get("loss_dollars", 0.0) for r in harvestable), 2)
    summary_line = (
        f"Tax harvest advisor: {len(harvestable)} harvestable position(s); "
        f"total unrealized loss ${total_loss:.2f}"
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "schema_version": "1",
        "is_taxable_account": True,
        "summary_line": summary_line,
        "harvestable_count": len(harvestable),
        "total_harvestable_loss_dollars": total_loss,
        "positions": rows,
        "notes": list(notes),
        "advisory_disclaimer": (
            "Advisory only. No SELL decisions are emitted. "
            "Wash-sale compliance is the operator's responsibility."
        ),
    }


def _render_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Tax-Loss Harvest Advisor",
        "",
        f"_Generated: {plan.get('generated_at')}_",
        "",
        "Observe-only. No SELL decisions emitted.",
        "",
        plan.get("summary_line", ""),
        "",
    ]
    if not plan.get("is_taxable_account"):
        lines.append("_Account is not taxable; nothing to harvest._")
        return "\n".join(lines) + "\n"

    lines += [
        "## Positions",
        "",
        "| Symbol | Status | Loss $ | Loss % | Replacement | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for r in plan.get("positions", []):
        loss = r.get("loss_dollars")
        pct = r.get("loss_pct")
        lines.append("| {sym} | {status} | {loss} | {pct} | {repl} | {note} |".format(
            sym=r.get("symbol", "?"),
            status=r.get("status", "?"),
            loss=(f"${loss:.2f}" if isinstance(loss, (int, float)) else "—"),
            pct=(f"{pct:+.1%}" if isinstance(pct, (int, float)) else "—"),
            repl=", ".join(r.get("replacement_candidates") or []) or "—",
            note=", ".join(r.get("notes") or []) or "",
        ))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def _current_price_from_fmp(fmp_client: Any, symbol: str) -> float | None:
    try:
        hist = fmp_client.get_historical_prices(symbol, years=1, ttl_days=1)
    except Exception as exc:
        logger.debug("tax_harvest: FMP fetch failed for %s: %s", symbol, exc)
        return None
    if not hist:
        return None
    # FMP rows newest-first
    for r in hist:
        if isinstance(r, dict):
            c = _safe_float(r.get("adjClose")) or _safe_float(r.get("close"))
            if c is not None and c > 0:
                return c
    return None


def run_tax_harvest_advisor(
    repo_root: Path | str,
    *,
    fmp_client: Any | None = None,
    replacement_map: dict[str, list[str]] | None = None,
    price_overrides: dict[str, float] | None = None,
    base_dir: Path | str = "outputs",
) -> dict[str, Any]:
    """
    Evaluate every holding for tax-loss-harvest opportunity.

    *price_overrides* lets tests inject prices directly without an FMP stub.
    *replacement_map* is optional — when None, no replacement candidate is
    listed (operator decides like-exposure substitutes).
    """
    repo_root = Path(repo_root)
    base_dir = Path(base_dir)
    cfg = _load_json_safe(repo_root / "config.json")
    portfolio = cfg.get("portfolio") or {}
    is_taxable = bool(portfolio.get("is_taxable_account", False))

    notes: list[str] = []
    if not is_taxable:
        notes.append("account is not flagged as taxable in config.json")
        plan = build_plan(is_taxable=False, rows=[], notes=notes)
        _write_artifacts(plan, base_dir)
        return plan

    replacement_map = replacement_map if replacement_map is not None else DEFAULT_REPLACEMENT_MAP
    price_overrides = price_overrides or {}
    rows: list[dict[str, Any]] = []

    for h in portfolio.get("holdings") or []:
        if not isinstance(h, dict):
            continue
        symbol = _safe_str(h.get("symbol")).upper()
        shares = _safe_float(h.get("shares"))
        cost_basis = _safe_float(h.get("cost_basis"))
        if not symbol or shares is None or shares <= 0:
            continue
        if symbol in price_overrides:
            price = _safe_float(price_overrides[symbol])
        elif fmp_client is not None:
            price = _current_price_from_fmp(fmp_client, symbol)
        else:
            price = None
        rows.append(evaluate_position(
            symbol=symbol,
            shares=shares,
            cost_basis=cost_basis,
            current_price=price,
            replacement_map=replacement_map,
        ))

    plan = build_plan(is_taxable=True, rows=rows, notes=notes)
    _write_artifacts(plan, base_dir)
    return plan


def _write_artifacts(plan: dict[str, Any], base_dir: Path) -> None:
    try:
        safe_write_json(
            OutputNamespace.LATEST, "tax_harvest_advisor.json", plan, base_dir=base_dir,
        )
        safe_write_text(
            OutputNamespace.LATEST, "tax_harvest_advisor.md",
            _render_markdown(plan), base_dir=base_dir,
        )
    except Exception as exc:
        logger.warning(
            "tax_harvest_advisor: failed to write artifacts (non-fatal): %s", exc
        )
