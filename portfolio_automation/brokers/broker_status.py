# portfolio_automation/brokers/broker_status.py
"""broker_sync_status artifact builder. Pure; observe-only; read-only hardcoded."""
from __future__ import annotations

from portfolio_automation.brokers.broker_models import redact


def build_status(*, enabled: bool, configured: bool, authenticated: bool,
                 account_count: int, position_count: int,
                 last_success_at: str | None, last_error: str | None,
                 now_iso: str) -> dict:
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
    return {
        "generated_at": now_iso, "observe_only": True, "source": "schwab",
        "enabled": bool(enabled), "configured": bool(configured),
        "authenticated": bool(authenticated),
        "read_only_mode": True, "trading_enabled": False,
        "last_success_at": last_success_at,
        "last_error": redact(last_error) if last_error else None,
        "account_count": int(account_count), "position_count": int(position_count),
        "overall_status": overall,
    }
