"""
Projections Module

Computes expected portfolio CAGR, future value projections, and milestone
estimates using simple monthly-compounding math.

All returns are *config-driven assumptions*, NOT predictions. The module
makes no market forecasts — it only applies user-specified expected returns
to illustrate the power of compounding.
"""

import math
import logging
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

from utils import format_currency

logger = logging.getLogger('portfolio_automation.projections')


@dataclass
class CompoundingDashboard:
    """All projection metrics for the compounding dashboard."""
    current_portfolio_value: float
    drawdown_pct: float           # From 12-month high
    expected_cagr: float          # Weighted portfolio CAGR
    monthly_contribution: float

    # 10-year projections
    projected_value_10yr: float
    projected_value_10yr_no_contrib: float   # Growth only, no new contributions
    projected_value_10yr_extra_200: float    # Scenario: +$200/month
    extra_200_impact: float                   # Extra value from the +$200 scenario

    # Milestone estimates in years from today (None = unreachable within 100 years)
    years_to_100k: Optional[float]
    years_to_250k: Optional[float]
    years_to_500k: Optional[float]
    years_to_1m: Optional[float]

    cagr_assumption_note: str

    def to_dict(self) -> Dict[str, Any]:
        def _yr(v: Optional[float]) -> str:
            if v is None:
                return ">100 years"
            if v == 0.0:
                return "Already reached"
            yr = int(v)
            mo = round((v - yr) * 12)
            return f"{yr}y {mo}m"

        return {
            'CurrentPortfolioValue': round(self.current_portfolio_value, 2),
            'DrawdownPct': f"{self.drawdown_pct * 100:.1f}%",
            'ExpectedCAGR': f"{self.expected_cagr:.1%}",
            'MonthlyContribution': round(self.monthly_contribution, 2),
            'Projected10yr': round(self.projected_value_10yr, 2),
            'Projected10yrNoContrib': round(self.projected_value_10yr_no_contrib, 2),
            'Projected10yrExtra200': round(self.projected_value_10yr_extra_200, 2),
            'Extra200Impact': round(self.extra_200_impact, 2),
            'YearsTo100k': _yr(self.years_to_100k),
            'YearsTo250k': _yr(self.years_to_250k),
            'YearsTo500k': _yr(self.years_to_500k),
            'YearsTo1m': _yr(self.years_to_1m),
            'AssumptionNote': self.cagr_assumption_note,
        }


def compute_portfolio_cagr(
    holdings: list,          # List[Holding] with actual_weight set
    total_portfolio: float,
    expected_returns: Dict[str, float],
    target_cash_weight: float = 0.05,
) -> float:
    """
    Compute the weighted expected CAGR of the portfolio.

    Each holding contributes (actual_weight × asset_class_return).
    The unallocated remainder (cash) earns the 'cash' return.

    Args:
        holdings: List of Holding objects; actual_weight must be set.
        total_portfolio: Total portfolio value (used only for guard).
        expected_returns: Map of asset_class -> annual return (e.g. 0.10 for 10%).
        target_cash_weight: Weight attributed to cash position.

    Returns:
        Weighted portfolio CAGR as a fraction (e.g. 0.09 for 9%).
    """
    if total_portfolio <= 0:
        return 0.0

    cash_return = expected_returns.get('cash', 0.04)
    weighted_return = target_cash_weight * cash_return
    allocated_weight = target_cash_weight

    for holding in holdings:
        weight = holding.actual_weight or 0.0
        if weight <= 0:
            continue

        asset_return = expected_returns.get(holding.asset_class)
        if asset_return is None:
            asset_return = expected_returns.get('us_equity', 0.08)
            logger.debug(
                f"No expected return for '{holding.asset_class}' "
                f"({holding.symbol}); falling back to us_equity rate"
            )

        weighted_return += weight * asset_return
        allocated_weight += weight

    # Apply cash return to any unaccounted weight
    unallocated = max(0.0, 1.0 - allocated_weight)
    if unallocated > 0:
        weighted_return += unallocated * cash_return

    return weighted_return


def project_future_value(
    current_value: float,
    monthly_contribution: float,
    annual_cagr: float,
    years: int,
) -> float:
    """
    Project portfolio value with monthly contributions and compounding.

    Uses the standard future-value formula with monthly compounding:
      FV = PV × (1+r)^n  +  PMT × [((1+r)^n - 1) / r]

    where r = monthly rate and n = months.

    Args:
        current_value: Current portfolio value.
        monthly_contribution: Fixed monthly addition.
        annual_cagr: Annual expected return (fraction).
        years: Number of years to project.

    Returns:
        Projected portfolio value.
    """
    if years <= 0:
        return current_value

    monthly_rate = (1 + annual_cagr) ** (1 / 12) - 1
    months = years * 12

    fv_lump = current_value * (1 + monthly_rate) ** months

    if monthly_rate > 0:
        fv_contrib = monthly_contribution * (((1 + monthly_rate) ** months - 1) / monthly_rate)
    else:
        fv_contrib = monthly_contribution * months

    return fv_lump + fv_contrib


def estimate_milestone(
    current_value: float,
    monthly_contribution: float,
    annual_cagr: float,
    target: float,
    max_years: int = 100,
) -> Optional[float]:
    """
    Estimate years needed to reach a portfolio value milestone.

    Uses binary search over projected values for accuracy with monthly
    compounding. Monotonicity is guaranteed when CAGR > 0 or contribution > 0.

    Returns:
        Years (float) to reach target, 0.0 if already reached,
        None if unreachable within max_years.
    """
    if current_value >= target:
        return 0.0

    if annual_cagr <= 0 and monthly_contribution <= 0:
        return None

    # Quick check: is the target reachable at all?
    if project_future_value(current_value, monthly_contribution, annual_cagr, max_years) < target:
        return None

    low, high = 0.0, float(max_years)
    for _ in range(60):  # Binary search — converges in ~60 iterations to 0.001-year precision
        mid = (low + high) / 2
        fv = project_future_value(
            current_value, monthly_contribution, annual_cagr, int(math.ceil(mid))
        )
        if fv >= target:
            high = mid
        else:
            low = mid
        if high - low < 0.05:
            break

    return round(high, 1)


def compute_compounding_dashboard(
    current_value: float,
    monthly_contribution: float,
    expected_cagr: float,
    drawdown_pct: float,
) -> CompoundingDashboard:
    """
    Compute all compounding dashboard metrics.

    Args:
        current_value: Current portfolio value.
        monthly_contribution: Monthly contribution amount.
        expected_cagr: Weighted annual expected CAGR (from compute_portfolio_cagr).
        drawdown_pct: Drawdown from 12-month high (as fraction, e.g. 0.08).

    Returns:
        CompoundingDashboard with all projection metrics.
    """
    proj_10yr = project_future_value(current_value, monthly_contribution, expected_cagr, 10)
    proj_10yr_no_contrib = project_future_value(current_value, 0, expected_cagr, 10)
    proj_10yr_extra_200 = project_future_value(
        current_value, monthly_contribution + 200, expected_cagr, 10
    )
    extra_200_impact = proj_10yr_extra_200 - proj_10yr

    y_100k = estimate_milestone(current_value, monthly_contribution, expected_cagr, 100_000)
    y_250k = estimate_milestone(current_value, monthly_contribution, expected_cagr, 250_000)
    y_500k = estimate_milestone(current_value, monthly_contribution, expected_cagr, 500_000)
    y_1m = estimate_milestone(current_value, monthly_contribution, expected_cagr, 1_000_000)

    cagr_note = (
        f"Assumes {expected_cagr:.1%} annual CAGR (weighted by asset class, config-driven) "
        f"and {format_currency(monthly_contribution)}/mo fixed contribution. "
        f"Illustrative only — does not guarantee returns."
    )

    return CompoundingDashboard(
        current_portfolio_value=current_value,
        drawdown_pct=drawdown_pct,
        expected_cagr=expected_cagr,
        monthly_contribution=monthly_contribution,
        projected_value_10yr=proj_10yr,
        projected_value_10yr_no_contrib=proj_10yr_no_contrib,
        projected_value_10yr_extra_200=proj_10yr_extra_200,
        extra_200_impact=extra_200_impact,
        years_to_100k=y_100k,
        years_to_250k=y_250k,
        years_to_500k=y_500k,
        years_to_1m=y_1m,
        cagr_assumption_note=cagr_note,
    )


def format_dashboard_text(dashboard: CompoundingDashboard) -> str:
    """Format the compounding dashboard as a human-readable console block."""

    def _yr(v: Optional[float]) -> str:
        if v is None:
            return ">100 years"
        if v == 0.0:
            return "Already reached!"
        yr = int(v)
        mo = round((v - yr) * 12)
        return f"~{yr}y {mo}m"

    lines = [
        "=" * 62,
        "  COMPOUNDING DASHBOARD (Aggressive Growth Mode)",
        "=" * 62,
        f"  Current Portfolio:      {format_currency(dashboard.current_portfolio_value)}",
        f"  Drawdown (12m high):    {dashboard.drawdown_pct * 100:.1f}%",
        f"  Expected CAGR:          {dashboard.expected_cagr:.1%}  (weighted, see note below)",
        f"  Monthly Contribution:   {format_currency(dashboard.monthly_contribution)}",
        "",
        "  ── 10-Year Projections ──────────────────────────────",
        f"  With contributions:     {format_currency(dashboard.projected_value_10yr)}",
        f"  Growth only (no contrib){format_currency(dashboard.projected_value_10yr_no_contrib)}",
        f"  +$200/mo scenario:      {format_currency(dashboard.projected_value_10yr_extra_200)}"
        f"  (+{format_currency(dashboard.extra_200_impact)})",
        "",
        "  ── Milestone Estimates ──────────────────────────────",
        f"  $100k:    {_yr(dashboard.years_to_100k)}",
        f"  $250k:    {_yr(dashboard.years_to_250k)}",
        f"  $500k:    {_yr(dashboard.years_to_500k)}",
        f"  $1M:      {_yr(dashboard.years_to_1m)}",
        "",
        f"  Note: {dashboard.cagr_assumption_note}",
        "=" * 62,
    ]
    return "\n".join(lines)
