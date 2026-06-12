"""
Shared base for the portfolio-simulation suite.

Sandbox-only, observe-only. Backtest / crowd-tactic / projection engines all
stamp the same envelope so the observe-only / no-trade / sandbox-only guarantees
are visible in every artifact, not just the code.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

OBSERVE_ONLY: bool = True
SANDBOX_ONLY: bool = True
NO_TRADE: bool = True
SCHEMA_VERSION: str = "1"
SOURCE: str = "portfolio_sim"

DISCLAIMER: str = (
    "Sandbox simulation only. Observe-only research; not a trade recommendation "
    "or a forecast. Tactics are never executed and never mutate the portfolio."
)


class SimStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    DEGRADED = "degraded"
    ERROR = "error"
    DISABLED = "disabled"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sim_envelope(
    *,
    run_id: str,
    run_mode: str,
    status: str = SimStatus.OK.value,
    warnings: list[str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Shared artifact envelope for every portfolio-sim artifact."""
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "run_id": run_id,
        "run_mode": run_mode,
        "created_at": created_at or utc_now_iso(),
        "status": status,
        "observe_only": OBSERVE_ONLY,
        "sandbox_only": SANDBOX_ONLY,
        "no_trade": NO_TRADE,
        "disclaimer": DISCLAIMER,
        "warnings": list(warnings or []),
    }
