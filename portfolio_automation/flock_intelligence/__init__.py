"""Flock Intelligence — simulation-only crowd flocking/dispersion detection.

Detects when a theme/sector/ticker cluster is forming a flock, becoming crowded/
exhausted, dispersing, or breaking apart. Simulation-only research context; never
changes production behavior until a human-approved promotion proposal is applied.
"""
from portfolio_automation.flock_intelligence.states import (  # noqa: F401
    FlockState, GroupFlock, GroupMetrics, TickerFlock, Thresholds, classify_group,
)
from portfolio_automation.flock_intelligence.producer import (  # noqa: F401
    run_flock_intelligence, build_group_metrics,
)
