"""Tests for return/risk metrics."""
from __future__ import annotations

import math

from portfolio_automation.portfolio_sim import metrics as m


def test_total_return_and_cagr():
    assert abs(m.total_return([100, 121]) - 0.21) < 1e-9
    assert abs(m.cagr([100, 121], years=2) - 0.10) < 1e-9   # 1.21^(1/2)-1 = 0.10


def test_max_drawdown():
    # peak 110 then 99 → -11/110
    assert abs(m.max_drawdown([100, 110, 99, 121]) - (99 / 110 - 1)) < 1e-9


def test_max_drawdown_monotonic_up_is_zero():
    assert m.max_drawdown([100, 110, 120]) == 0.0


def test_sharpe_positive_for_steady_growth():
    vals = [100 * (1.001 ** i) for i in range(60)]   # steady +0.1%/day
    assert m.sharpe(vals) > 0


def test_excess_return():
    assert abs(m.excess_return(0.15, 0.10) - 0.05) < 1e-12


def test_dca_terminal_hand_checked():
    # growth-of-$1 doubles by the end: [1, 2]; one $100 contribution at step 0.
    bal, total = m.dca_terminal([1.0, 2.0], [(0, 100.0)])
    assert abs(bal - 200.0) < 1e-9    # $100 grows ×2
    assert total == 100.0


def test_dca_terminal_midway_contribution():
    # growth [1,2,4]; contribute $100 at step 1 (value 2) → grows 4/2=2x → $200
    bal, total = m.dca_terminal([1.0, 2.0, 4.0], [(1, 100.0)])
    assert abs(bal - 200.0) < 1e-9
    assert total == 100.0
