# portfolio_automation/brokers/broker_status.py
"""broker_sync_status artifact builder. Pure; observe-only; read-only hardcoded."""
from __future__ import annotations

from portfolio_automation.brokers.broker_models import redact


def build_status(*, enabled: bool, configured: bool, authenticated: bool,
                 account_count: int, position_count: int,
                 last_success_at: str | None, last_error: str | None,
                 now_iso: str, reauth: dict | None = None,
                 auth_state: str | None = None, evidence: dict | None = None) -> dict:
    if not enabled:
        overall = "disabled"
    elif not configured:
        overall = "unconfigured"
    elif last_error:
        overall = "error"
    elif authenticated:
        overall = "ok"
    else:
        overall = "degraded"
    # reauth is the pre-computed schwab_oauth.refresh_token_status() block (kept pure
    # here — the sync layer owns the oauth dependency). The 7-day refresh-token clock
    # is surfaced as ADDITIVE signal and never flips overall_status (observe-only).
    reauth = reauth or {}
    return {
        "generated_at": now_iso, "observe_only": True, "source": "schwab",
        "enabled": bool(enabled), "configured": bool(configured),
        "authenticated": bool(authenticated),
        "read_only_mode": True, "trading_enabled": False,
        "last_success_at": last_success_at,
        "last_error": redact(last_error) if last_error else None,
        "account_count": int(account_count), "position_count": int(position_count),
        "overall_status": overall,
        "reauth_status": reauth.get("reauth_status", "unknown"),
        "reauth_expires_at": reauth.get("expires_at"),
        "reauth_days_remaining": reauth.get("days_remaining"),
        # v2 foundation (additive, observe-only): the structured auth outcome and
        # the evidence truth model. A failed attempt reports STALE_LAST_KNOWN_GOOD
        # or NO_TRUTH — never an empty portfolio.
        "auth_state": auth_state,
        "evidence_truth_status": (evidence or {}).get("truth_status"),
        "evidence_snapshot_id": (evidence or {}).get("admitted_snapshot_id"),
        "evidence_last_attempt_outcome": (evidence or {}).get("latest_attempt_outcome"),
        "evidence_admitted_age_s": (evidence or {}).get("admitted_age_s"),
    }
