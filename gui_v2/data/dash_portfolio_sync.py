"""Portfolio Sync cockpit — Schwab read view.

Reads five Schwab artifacts from outputs/latest/ defensively (all may be absent —
this is NORMAL since the brokers package lives on a separate branch and Schwab is
optional). Every absent artifact produces an explicit info empty state, never red.

SAFETY:
  - observe_only=True hardcoded.
  - No trade/buy/sell/execute/order language.
  - Account IDs are already masked in the upstream artifacts (…1234 format).
  - "Generate Config Update Proposal" is a read-only reconcile call — it writes the
    proposal artifact only; it NEVER mutates config.json.
  - schwab_available flag: True only when portfolio_automation.brokers.schwab_sync
    is importable. When False the control renders DISABLED with a note.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import card, _read_json

# ---------------------------------------------------------------------------
# Defensive broker import — sets module-level flag
# ---------------------------------------------------------------------------

try:
    from portfolio_automation.brokers.schwab_sync import run_reconcile as _run_reconcile  # noqa: F401
    schwab_available: bool = True
except ImportError:
    schwab_available = False


# ---------------------------------------------------------------------------
# Status-mapping helper (local, matches dash_system conventions)
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, str] = {
    "ok": "ok",
    "healthy": "ok",
    "success": "ok",
    "connected": "ok",
    "ok_with_warnings": "warning",
    "partial": "warning",
    "warn": "warning",
    "warning": "warning",
    "amber": "warning",
    "degraded": "warning",
    "failed": "red",
    "error": "red",
    "red": "red",
    "unconfigured": "info",
    "not_configured": "info",
    "info": "info",
    "unknown": "unknown",
}


def _map_status(raw: str | None) -> str:
    return _STATUS_MAP.get((raw or "unknown").lower().strip(), "unknown")


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------


def _card_connection(bss: dict | None) -> dict:
    """Connection status card from broker_sync_status.json."""
    if bss is None:
        return card(
            "Connection",
            status="info",
            label="not configured",
            summary="Schwab not configured — broker_sync_status.json absent. Schwab is optional.",
            source_artifacts=["broker_sync_status.json"],
        )

    overall = bss.get("overall_status") or "unknown"
    configured = bss.get("configured") or False
    authenticated = bss.get("authenticated") or False

    # Map overall to card status; unconfigured → info (not red — Schwab is optional)
    if overall.lower() in ("unconfigured", "not_configured"):
        c_status = "info"
        label = "not configured"
    elif overall.lower() in ("ok", "healthy", "connected"):
        c_status = "ok"
        label = "connected"
    elif overall.lower() in ("error", "failed", "red"):
        c_status = "red"
        label = overall
    else:
        c_status = _map_status(overall)
        label = overall

    parts = [f"configured: {'yes' if configured else 'no'}"]
    parts.append(f"authenticated: {'yes' if authenticated else 'no'}")

    return card(
        "Connection",
        status=c_status,
        label=label,
        summary="; ".join(parts),
        source_artifacts=["broker_sync_status.json"],
        updated_at=bss.get("generated_at"),
    )


def _card_last_sync(bss: dict | None) -> dict:
    """Last sync timestamp card."""
    if bss is None:
        return card(
            "Last Sync",
            status="info",
            label="never",
            summary="No sync data — Schwab not configured.",
            source_artifacts=["broker_sync_status.json"],
        )

    last_success_at = bss.get("last_success_at")
    last_error = bss.get("last_error")

    if last_success_at:
        c_status = "ok"
        label = last_success_at[:10] if len(str(last_success_at)) >= 10 else str(last_success_at)
        summary = f"Last successful sync: {last_success_at}"
        if last_error:
            summary += f"; last error: {last_error}"
    else:
        c_status = "info"
        label = "never"
        summary = "No successful sync recorded yet."
        if last_error:
            summary += f" Last error: {last_error}"

    return card(
        "Last Sync",
        status=c_status,
        label=label,
        summary=summary,
        source_artifacts=["broker_sync_status.json"],
        updated_at=bss.get("generated_at"),
    )


def _card_holdings_matched(recon: dict | None) -> dict:
    """Holdings matched count from portfolio_reconciliation.json."""
    if recon is None:
        return card(
            "Holdings Matched",
            status="info",
            label="no data",
            summary="portfolio_reconciliation.json absent — run reconcile to compare holdings.",
            source_artifacts=["portfolio_reconciliation.json"],
        )

    matched = recon.get("matched_count") or 0
    total = recon.get("total_count") or matched
    missing_local = recon.get("missing_in_local") or []
    missing_schwab = recon.get("missing_in_schwab") or []
    quantity_mismatches = recon.get("quantity_mismatches") or []

    n_missing_local = len(missing_local) if isinstance(missing_local, list) else int(missing_local or 0)
    n_missing_schwab = len(missing_schwab) if isinstance(missing_schwab, list) else int(missing_schwab or 0)
    n_qty_mismatch = len(quantity_mismatches) if isinstance(quantity_mismatches, list) else int(quantity_mismatches or 0)

    n_mismatches = n_missing_local + n_missing_schwab + n_qty_mismatch

    if n_mismatches > 0:
        c_status = "warning"
        label = f"{n_mismatches} mismatches"
    else:
        c_status = "ok"
        label = f"{matched} matched"

    parts = [f"{matched} matched"]
    if total and total != matched:
        parts.append(f"{total} total")
    if n_qty_mismatch:
        parts.append(f"{n_qty_mismatch} quantity mismatch(es)")
    if n_missing_local:
        parts.append(f"{n_missing_local} missing in local")
    if n_missing_schwab:
        parts.append(f"{n_missing_schwab} missing in Schwab")

    return card(
        "Holdings Matched",
        status=c_status,
        label=label,
        summary="; ".join(parts),
        source_artifacts=["portfolio_reconciliation.json"],
        updated_at=recon.get("generated_at"),
    )


def _card_cash_difference(recon: dict | None) -> dict:
    """Cash difference card from portfolio_reconciliation.json."""
    if recon is None:
        return card(
            "Cash Difference",
            status="info",
            label="no data",
            summary="portfolio_reconciliation.json absent.",
            source_artifacts=["portfolio_reconciliation.json"],
        )

    cash_obj = recon.get("cash") or {}
    if isinstance(cash_obj, dict):
        delta = cash_obj.get("delta")
        local_val = cash_obj.get("local")
        schwab_val = cash_obj.get("schwab")
    else:
        delta = None
        local_val = None
        schwab_val = None

    if delta is None:
        return card(
            "Cash Difference",
            status="info",
            label="no data",
            summary="Cash reconciliation data not available.",
            source_artifacts=["portfolio_reconciliation.json"],
            updated_at=recon.get("generated_at"),
        )

    delta_f = float(delta)
    if abs(delta_f) < 0.01:
        c_status = "ok"
        label = "matched"
    elif abs(delta_f) < 100:
        c_status = "warning"
        label = f"${delta_f:+.2f}"
    else:
        c_status = "red"
        label = f"${delta_f:+.2f}"

    parts = [f"delta: ${delta_f:+.2f}"]
    if local_val is not None:
        parts.append(f"local: ${float(local_val):.2f}")
    if schwab_val is not None:
        parts.append(f"Schwab: ${float(schwab_val):.2f}")

    return card(
        "Cash Difference",
        status=c_status,
        label=label,
        summary="; ".join(parts),
        source_artifacts=["portfolio_reconciliation.json"],
        updated_at=recon.get("generated_at"),
    )


def _card_proposal_status(proposal: dict | None) -> dict:
    """Proposal status card from portfolio_config_update_proposal.json."""
    if proposal is None:
        return card(
            "Config Update Proposal",
            status="info",
            label="none",
            summary="No config update proposal generated yet. Use 'Generate Config Update Proposal' to create one.",
            source_artifacts=["portfolio_config_update_proposal.json"],
        )

    operator_approval_required = proposal.get("operator_approval_required")
    auto_applied = proposal.get("auto_applied") or False
    proposal_status = proposal.get("status") or proposal.get("proposal_status") or "pending"
    n_changes = len(proposal.get("changes") or [])

    if auto_applied:
        c_status = "warning"
        label = "auto-applied"
        summary = f"Proposal was auto-applied. {n_changes} change(s). Operator review recommended."
    elif operator_approval_required is True:
        c_status = "warning"
        label = "pending review"
        summary = f"Operator review required before applying. {n_changes} change(s)."
    elif operator_approval_required is False:
        c_status = "info"
        label = "ready"
        summary = f"Proposal ready. {n_changes} change(s). No approval required."
    else:
        c_status = "info"
        label = proposal_status
        summary = f"Proposal status: {proposal_status}. {n_changes} change(s)."

    return card(
        "Config Update Proposal",
        status=c_status,
        label=label,
        summary=summary,
        source_artifacts=["portfolio_config_update_proposal.json"],
        updated_at=proposal.get("generated_at"),
    )


# ---------------------------------------------------------------------------
# Mismatch rows helper (for holdings-mismatch table)
# ---------------------------------------------------------------------------


def _build_mismatch_rows(recon: dict | None) -> list[dict[str, Any]]:
    """
    Extract mismatch rows from portfolio_reconciliation.json for the desktop table
    and mobile cards.

    Returns a list of dicts with keys: symbol, mismatch_type, local_value,
    schwab_value, delta.
    """
    if not recon:
        return []

    rows: list[dict[str, Any]] = []

    quantity_mismatches = recon.get("quantity_mismatches") or []
    if isinstance(quantity_mismatches, list):
        for item in quantity_mismatches:
            if not isinstance(item, dict):
                continue
            rows.append({
                "symbol": item.get("symbol") or "—",
                "mismatch_type": "quantity",
                "local_value": item.get("local_quantity"),
                "schwab_value": item.get("schwab_quantity"),
                "delta": item.get("delta"),
            })

    missing_in_local = recon.get("missing_in_local") or []
    if isinstance(missing_in_local, list):
        for item in missing_in_local:
            symbol = item if isinstance(item, str) else (item.get("symbol") if isinstance(item, dict) else str(item))
            rows.append({
                "symbol": symbol or "—",
                "mismatch_type": "missing in local",
                "local_value": None,
                "schwab_value": "present",
                "delta": None,
            })

    missing_in_schwab = recon.get("missing_in_schwab") or []
    if isinstance(missing_in_schwab, list):
        for item in missing_in_schwab:
            symbol = item if isinstance(item, str) else (item.get("symbol") if isinstance(item, dict) else str(item))
            rows.append({
                "symbol": symbol or "—",
                "mismatch_type": "missing in Schwab",
                "local_value": "present",
                "schwab_value": None,
                "delta": None,
            })

    return rows


# ---------------------------------------------------------------------------
# Public collector
# ---------------------------------------------------------------------------


def collect_portfolio_sync_view(root: Path) -> dict[str, Any]:
    """
    Persona collector for /dashboard/portfolio-sync.

    Returns::

        {
          "cards": [ <card dicts> ],
          "persona": "portfolio_sync",
          "mismatch_rows": [ <row dicts> ],       # for the holdings-mismatch table
          "schwab_available": bool,               # True = brokers module importable
          "observe_only": True,
        }

    Reads five artifacts — all may be absent → explicit empty states (info, never red):
      - broker_sync_status.json
      - schwab_portfolio_snapshot.json
      - schwab_positions.json
      - portfolio_reconciliation.json
      - portfolio_config_update_proposal.json
    """
    root = Path(root)
    latest = root / "outputs" / "latest"

    bss = _read_json(latest / "broker_sync_status.json")
    _snapshot = _read_json(latest / "schwab_portfolio_snapshot.json")   # read but not used in cards yet
    _positions = _read_json(latest / "schwab_positions.json")            # read but not used in cards yet
    recon = _read_json(latest / "portfolio_reconciliation.json")
    proposal = _read_json(latest / "portfolio_config_update_proposal.json")

    cards: list[dict[str, Any]] = []
    cards.append(_card_connection(bss))
    cards.append(_card_last_sync(bss))
    cards.append(_card_holdings_matched(recon))
    cards.append(_card_cash_difference(recon))
    cards.append(_card_proposal_status(proposal))

    mismatch_rows = _build_mismatch_rows(recon)

    return {
        "cards": cards,
        "persona": "portfolio_sync",
        "mismatch_rows": mismatch_rows,
        "schwab_available": schwab_available,
        "observe_only": True,
    }
