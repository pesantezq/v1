"""
Portfolio simulation suite (sandbox-only, observe-only).

- Historical backtest of the operator's real portfolio + alternative tactics.
- Crowd-signal tactic (forward shadow-track + labeled proxy backtest).
- Forward Monte-Carlo projection.

Never trades, never mutates the official portfolio / decision plan / registry.
See docs/superpowers/specs/2026-06-12-*.md.
"""
from __future__ import annotations

from portfolio_automation.portfolio_sim.sim_base import SimStatus, sim_envelope

__all__ = ["SimStatus", "sim_envelope"]
