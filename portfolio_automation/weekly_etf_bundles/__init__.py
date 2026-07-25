"""
Weekly ETF Bundle Watchlist — standalone, observe-only weekly subsystem.

Analyzes manually curated ETF baskets, freezes each weekly ranking as an
immutable prediction, matures those predictions at multiple forward horizons,
scores their quality (hit rates, calibration, ranking metrics), makes the
strategy available to Strat Lab for controlled improvement, and sends an
informational weekly email.

HARD BOUNDARIES (enforced in code + tests):
  observe_only:                        True
  simulation_active:                   True
  production_gated:                    True
  human_approval_required_for_production: True
  feeds_decision_engine:               False

This package is fully isolated from the daily recommendation, memo, watchlist,
and capital-allocation packages. It NEVER writes decision_plan.json, creates
trades/allocations/actions/approvals, mutates portfolio or production-watchlist
state, or feeds the production decision engine. Bundle membership is human-owned
(config/weekly_etf_bundles.yaml) and is never modified automatically.
"""
from __future__ import annotations

# Machine-readable posture — imported by health checks and tests to assert the
# invariants stay true. Do NOT make any of these conditional.
POSTURE: dict[str, bool] = {
    "observe_only": True,
    "simulation_active": True,
    "production_gated": True,
    "human_approval_required_for_production": True,
    "feeds_decision_engine": False,
}

SCHEMA_VERSION = "1"
SOURCE_LABEL = "weekly_etf_bundles"
STRATEGY_ID = "weekly_etf_bundle_v1"
MODEL_VERSION = "weekly_etf_v1"

DISCLAIMER = (
    "Observe-only weekly ETF bundle watchlist. Informational analysis of curated "
    "ETF baskets. Does NOT create trades, allocations, or actions, does NOT modify "
    "portfolio or watchlist state, and does NOT feed the production decision engine. "
    "Statistics are observational and do not represent live portfolio returns."
)
