"""
GUI Portfolio Config Writer — gated, safe-write surface for Task 7.

This module is the ONLY sanctioned path for mutating config.json portfolio
state through the GUI.  It reuses the safe-write primitives from
``tools.manual_portfolio_update`` directly (imported, not reimplemented).

Safety invariants (hardcoded):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - Only writes:
      * ``config.json`` (only ``portfolio.holdings`` and
        ``portfolio.cash_available`` are touched; all other keys preserved)
      * ``outputs/policy/portfolio_backups/config.<YYYYMMDD_HHMMSS>.json``
        (pre-update backup — created BEFORE writing config.json)
      * ``outputs/policy/manual_portfolio_updates.jsonl`` (append-only audit)
  - Never touches decision-core artifacts
    (outputs/latest/*, signal_registry.yaml, etc.)
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Reuse safe-write primitives from tools.manual_portfolio_update directly
# ---------------------------------------------------------------------------
from tools.manual_portfolio_update import (
    _atomic_write_json,
    _write_backup,
    _append_audit_record,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_LABEL = "gui_portfolio_config"

_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_SAFETY_DISCLAIMER = (
    "GUI operator update of holdings and cash. "
    "No broker trade placed. No recommendation emitted. "
    "Allocation policy, scoring, watchlist, discovery, and recommendations "
    "are not modified by this workflow."
)

# Tolerance for target-weight sum check
_WEIGHT_SUM_TOLERANCE = 0.02


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_config_edit(
    holdings: list[dict],
    cash: float,
    config: dict,
) -> dict[str, Any]:
    """
    Validate a proposed portfolio edit.

    Parameters
    ----------
    holdings:
        List of holding dicts with at minimum ``symbol`` and ``shares`` keys.
        Optional keys: ``target_weight``, ``asset_class``, ``is_leveraged``,
        ``leverage_factor``.
    cash:
        Proposed cash_available value.
    config:
        Full config dict (used to read growth_mode caps).

    Returns
    -------
    ``{"ok": bool, "errors": [str]}``
    """
    errors: list[str] = []

    # 1. Cash must be non-negative
    try:
        cash_f = float(cash)
    except (TypeError, ValueError):
        errors.append(f"cash_available must be a number, got {cash!r}")
        cash_f = None

    if cash_f is not None and cash_f < 0:
        errors.append(f"cash_available must be non-negative, got {cash_f}")

    if not isinstance(holdings, list):
        errors.append("holdings must be a list")
        return {"ok": False, "errors": errors}

    seen_symbols: set[str] = set()
    weights: list[float] = []
    any_weight_present = False
    total_leveraged_weight: float = 0.0
    max_single_weight: float = 0.0

    for i, h in enumerate(holdings):
        if not isinstance(h, dict):
            errors.append(f"holdings[{i}]: must be a dict")
            continue

        # Symbol: required, non-empty, pattern-valid.
        # We normalise to uppercase for dedup/cap checks, but pattern
        # validation runs on the raw value so that lowercase inputs are
        # caught (the caller / form is expected to send uppercase).
        symbol_raw = str(h.get("symbol") or "").strip()
        symbol = symbol_raw.upper()
        if not symbol_raw:
            errors.append(f"holdings[{i}]: symbol is required and must not be empty")
            continue

        if not _SYMBOL_PATTERN.match(symbol_raw):
            errors.append(
                f"holdings[{i}]: invalid symbol {symbol_raw!r} — "
                "must be 1-10 uppercase letters/digits, may include '.' or '-'"
            )

        if symbol in seen_symbols:
            errors.append(f"holdings[{i}]: duplicate symbol {symbol_raw!r}")
        else:
            seen_symbols.add(symbol)

        # Shares: must be non-negative
        try:
            shares_f = float(h.get("shares") if h.get("shares") is not None else -1)
        except (TypeError, ValueError):
            errors.append(f"holdings[{i}] ({symbol}): shares must be numeric")
            shares_f = 0.0

        if shares_f < 0:
            errors.append(
                f"holdings[{i}] ({symbol}): shares must be non-negative, got {h.get('shares')}"
            )

        # target_weight: optional, must be in [0, 1]
        tw_raw = h.get("target_weight")
        if tw_raw is not None and str(tw_raw).strip() != "":
            try:
                tw = float(tw_raw)
            except (TypeError, ValueError):
                errors.append(
                    f"holdings[{i}] ({symbol}): target_weight must be numeric"
                )
                tw = None
            else:
                if not (0.0 <= tw <= 1.0):
                    errors.append(
                        f"holdings[{i}] ({symbol}): target_weight must be in [0, 1], got {tw}"
                    )
                else:
                    weights.append(tw)
                    any_weight_present = True
                    if tw > max_single_weight:
                        max_single_weight = tw

                    # Track leveraged weight for leverage_cap
                    is_lev = h.get("is_leveraged")
                    if is_lev in (True, "true", "True", "1", 1):
                        total_leveraged_weight += tw

    # target-weight sum check
    if any_weight_present:
        total_w = sum(weights)
        if abs(total_w - 1.0) > _WEIGHT_SUM_TOLERANCE:
            errors.append(
                f"target_weight values sum to {total_w:.4f}; "
                f"must be within {_WEIGHT_SUM_TOLERANCE} of 1.0"
            )

    # concentration_cap and leverage_cap from growth_mode
    growth = config.get("growth_mode") or {}
    if isinstance(growth, dict):
        conc_cap = growth.get("concentration_cap")
        lev_cap = growth.get("leverage_cap")

        if conc_cap is not None and any_weight_present:
            try:
                conc_cap_f = float(conc_cap)
                if max_single_weight > conc_cap_f + 1e-9:
                    errors.append(
                        f"A single position has target_weight {max_single_weight:.4f} "
                        f"which exceeds the concentration_cap {conc_cap_f:.4f} "
                        f"from growth_mode"
                    )
            except (TypeError, ValueError):
                pass

        if lev_cap is not None and any_weight_present:
            try:
                lev_cap_f = float(lev_cap)
                if total_leveraged_weight > lev_cap_f + 1e-9:
                    errors.append(
                        f"Total leveraged target_weight {total_leveraged_weight:.4f} "
                        f"exceeds the leverage_cap {lev_cap_f:.4f} from growth_mode"
                    )
            except (TypeError, ValueError):
                pass

    return {"ok": len(errors) == 0, "errors": errors}


# ---------------------------------------------------------------------------
# Diff (dry-run preview)
# ---------------------------------------------------------------------------


def diff_config_edit(
    before: dict,
    holdings: list[dict],
    cash: float,
) -> dict[str, Any]:
    """
    Compute the before→after diff without writing anything.

    Parameters
    ----------
    before:
        The current config dict.
    holdings:
        The proposed new holdings list.
    cash:
        The proposed new cash_available value.

    Returns
    -------
    ``{
        "holdings": [{"symbol": str, "before": dict|None, "after": dict}],
        "cash": {"before": float, "after": float},
    }``
    """
    portfolio = before.get("portfolio") or {}
    prior_holdings: list[dict] = portfolio.get("holdings") or []
    prior_cash = float(portfolio.get("cash_available") or 0.0)

    prior_by_symbol: dict[str, dict] = {}
    for h in prior_holdings:
        if isinstance(h, dict):
            sym = str(h.get("symbol") or "").strip().upper()
            if sym:
                prior_by_symbol[sym] = h

    holdings_diff: list[dict[str, Any]] = []
    seen: set[str] = set()

    for h in (holdings or []):
        if not isinstance(h, dict):
            continue
        sym = str(h.get("symbol") or "").strip().upper()
        if not sym:
            continue
        seen.add(sym)
        holdings_diff.append({
            "symbol": sym,
            "before": prior_by_symbol.get(sym),  # None = new symbol
            "after": h,
        })

    # Symbols that are being removed
    for sym, prior_h in prior_by_symbol.items():
        if sym not in seen:
            holdings_diff.append({
                "symbol": sym,
                "before": prior_h,
                "after": None,  # None = removed
            })

    return {
        "holdings": holdings_diff,
        "cash": {"before": prior_cash, "after": float(cash)},
    }


# ---------------------------------------------------------------------------
# Apply (write)
# ---------------------------------------------------------------------------


def apply_config_edit(
    root: Path,
    holdings: list[dict],
    cash: float,
) -> dict[str, Any]:
    """
    Apply a validated portfolio edit to config.json.

    Steps (in order):
        1. Load current config.json.
        2. Create a timestamped backup in outputs/policy/portfolio_backups/.
        3. Atomically write the updated config.json (portfolio.holdings +
           portfolio.cash_available only; all other keys preserved).
        4. Append an audit record to outputs/policy/manual_portfolio_updates.jsonl.

    Parameters
    ----------
    root:
        Project root directory (contains config.json and outputs/).
    holdings:
        New holdings list (already validated by ``validate_config_edit``).
    cash:
        New cash_available value (already validated).

    Returns
    -------
    ``{
        "ok": bool,
        "backup_path": str | None,
        "audit_appended": bool,
        "error": str | None,
    }``
    """
    root = Path(root)
    config_path = root / "config.json"

    try:
        # 1. Load current config
        try:
            prior_config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "ok": False,
                "backup_path": None,
                "audit_appended": False,
                "error": f"Failed to read config.json: {exc}",
            }

        if not isinstance(prior_config, dict):
            return {
                "ok": False,
                "backup_path": None,
                "audit_appended": False,
                "error": "config.json root must be a JSON object",
            }

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        iso_ts = datetime.now(timezone.utc).isoformat()
        run_id = f"gui_portfolio_config_{timestamp}"

        # 2. Backup FIRST (before any mutation)
        try:
            backup_path = _write_backup(prior_config, root, timestamp)
        except Exception as exc:
            return {
                "ok": False,
                "backup_path": None,
                "audit_appended": False,
                "error": f"Failed to write backup: {exc}",
            }

        # 3. Build the updated config — only portfolio.holdings + cash_available
        prior_portfolio = prior_config.get("portfolio") or {}
        if not isinstance(prior_portfolio, dict):
            prior_portfolio = {}

        prior_cash = float(prior_portfolio.get("cash_available") or 0.0)
        prior_holdings = prior_portfolio.get("holdings") or []

        new_portfolio = dict(prior_portfolio)
        new_portfolio["holdings"] = [dict(h) for h in holdings]
        new_portfolio["cash_available"] = float(cash)

        new_config = dict(prior_config)
        new_config["portfolio"] = new_portfolio

        try:
            _atomic_write_json(config_path, new_config)
        except Exception as exc:
            return {
                "ok": False,
                "backup_path": str(backup_path),
                "audit_appended": False,
                "error": f"Failed to write config.json: {exc}",
            }

        # 4. Append audit record
        audit_record: dict[str, Any] = {
            "run_id": run_id,
            "timestamp": iso_ts,
            "source": _SOURCE_LABEL,
            "config_path": str(config_path),
            "backup_path": str(backup_path),
            "prior_cash": prior_cash,
            "new_cash": float(cash),
            "cash_delta": float(cash) - prior_cash,
            "prior_holdings_count": len(prior_holdings) if isinstance(prior_holdings, list) else 0,
            "new_holdings_count": len(holdings),
            # Safety flags — hardcoded
            "observe_only": True,
            "no_trade": True,
            "not_recommendation": True,
            "no_allocation_policy_change": True,
            "no_watchlist_mutation": True,
            "no_discovery_promotion": True,
            "safety_disclaimer": _SAFETY_DISCLAIMER,
            # Before / after snapshot (symbols only to keep the record compact)
            "before": {
                "cash": prior_cash,
                "holding_symbols": [
                    str(h.get("symbol") or "").upper()
                    for h in (prior_holdings if isinstance(prior_holdings, list) else [])
                    if isinstance(h, dict)
                ],
            },
            "after": {
                "cash": float(cash),
                "holding_symbols": [
                    str(h.get("symbol") or "").upper()
                    for h in holdings
                    if isinstance(h, dict)
                ],
            },
        }

        try:
            _append_audit_record(audit_record, root)
            audit_appended = True
        except Exception:
            # Audit append failure is non-fatal; write was already committed
            audit_appended = False

        return {
            "ok": True,
            "backup_path": str(backup_path),
            "audit_appended": audit_appended,
            "error": None,
        }

    except Exception as exc:
        return {
            "ok": False,
            "backup_path": None,
            "audit_appended": False,
            "error": f"Unexpected error in apply_config_edit: {exc}",
        }
