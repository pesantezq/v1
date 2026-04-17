"""
Portfolio calculations module.
Handles market value, allocation, drift, and exposure calculations.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from utils import (
    Holding, Config, Retirement401k,
    safe_divide, round_currency, round_percent,
    format_currency, format_percent
)


logger = logging.getLogger('portfolio_automation.portfolio')


@dataclass
class PortfolioSummary:
    """Summary of portfolio state."""
    total_holdings_value: float
    cash_value: float
    total_portfolio_value: float
    retirement_401k_value: float
    total_net_worth: float
    cash_weight: float
    max_drift: float
    max_drift_symbol: str
    has_breach: bool
    breach_threshold: float
    effective_equity_exposure: float
    effective_leverage_exposure: float
    timestamp: str


@dataclass
class HoldingAnalysis:
    """Analysis result for a single holding."""
    symbol: str
    shares: float
    current_price: Optional[float]
    market_value: Optional[float]
    target_weight: float
    actual_weight: float
    drift: float
    drift_direction: str
    is_breached: bool
    effective_exposure: float
    asset_class: str
    is_leveraged: bool


def calculate_portfolio_values(
    holdings: list[Holding],
    cash_available: float,
    retirement_401k: Retirement401k
) -> tuple[float, float, float, float]:
    """
    Calculate portfolio value totals.
    Returns (holdings_value, cash_value, total_portfolio, total_net_worth).
    """
    holdings_value = sum(
        h.market_value for h in holdings
        if h.market_value is not None
    )
    
    total_portfolio = holdings_value + cash_available
    
    retirement_value = retirement_401k.balance if retirement_401k.enabled else 0.0
    
    total_net_worth = total_portfolio
    if retirement_401k.enabled and retirement_401k.include_in_net_worth:
        total_net_worth += retirement_value
    
    return (
        round_currency(holdings_value),
        round_currency(cash_available),
        round_currency(total_portfolio),
        round_currency(total_net_worth)
    )


def calculate_allocations(
    holdings: list[Holding],
    total_portfolio_value: float,
    cash_available: float,
    target_cash_weight: float
) -> tuple[list[Holding], float, float]:
    """
    Calculate actual allocations and drift for all holdings.
    Returns (updated_holdings, cash_weight, cash_drift).
    """
    if total_portfolio_value <= 0:
        logger.warning("Total portfolio value is zero or negative")
        return holdings, 0.0, 0.0
    
    for holding in holdings:
        if holding.market_value is not None:
            holding.actual_weight = holding.market_value / total_portfolio_value
            holding.drift = holding.actual_weight - holding.target_weight
        else:
            holding.actual_weight = None
            holding.drift = None
    
    cash_weight = safe_divide(cash_available, total_portfolio_value)
    cash_drift = cash_weight - target_cash_weight
    
    return holdings, round_percent(cash_weight), round_percent(cash_drift)


def calculate_effective_exposure(holdings: list[Holding]) -> tuple[float, float]:
    """
    Calculate effective exposure accounting for leverage.
    Returns (total_equity_exposure, leveraged_exposure).
    """
    total_exposure = 0.0
    leveraged_exposure = 0.0
    
    for holding in holdings:
        if holding.market_value is None:
            continue
        
        effective = holding.market_value * holding.leverage_factor
        
        # Count equity-related assets toward exposure
        if holding.asset_class in ['us_equity', 'us_equity_sector', 
                                   'international_equity', 'us_equity_leveraged']:
            total_exposure += effective
        
        if holding.is_leveraged:
            leveraged_exposure += holding.market_value
    
    return round_currency(total_exposure), round_currency(leveraged_exposure)


def find_max_drift(
    holdings: list[Holding],
    cash_drift: float
) -> tuple[float, str]:
    """Find the maximum absolute drift and its source."""
    max_drift = abs(cash_drift)
    max_drift_symbol = "CASH"
    
    for holding in holdings:
        if holding.drift is not None:
            abs_drift = abs(holding.drift)
            if abs_drift > max_drift:
                max_drift = abs_drift
                max_drift_symbol = holding.symbol
    
    return round_percent(max_drift), max_drift_symbol


def analyze_holdings(
    holdings: list[Holding],
    total_portfolio_value: float,
    band_threshold: float
) -> list[HoldingAnalysis]:
    """Generate detailed analysis for each holding."""
    analyses = []
    
    for holding in holdings:
        actual_weight = holding.actual_weight or 0.0
        drift = holding.drift or 0.0
        
        if drift > 0:
            direction = "overweight"
        elif drift < 0:
            direction = "underweight"
        else:
            direction = "on_target"
        
        analysis = HoldingAnalysis(
            symbol=holding.symbol,
            shares=holding.shares,
            current_price=holding.current_price,
            market_value=holding.market_value,
            target_weight=holding.target_weight,
            actual_weight=actual_weight,
            drift=drift,
            drift_direction=direction,
            is_breached=abs(drift) > band_threshold,
            effective_exposure=holding.effective_exposure if holding.market_value else 0.0,
            asset_class=holding.asset_class,
            is_leveraged=holding.is_leveraged
        )
        analyses.append(analysis)
    
    return analyses


def generate_portfolio_summary(
    holdings: list[Holding],
    cash_available: float,
    target_cash_weight: float,
    retirement_401k: Retirement401k,
    band_threshold: float,
    timestamp: str
) -> PortfolioSummary:
    """Generate comprehensive portfolio summary."""
    
    # Calculate values
    holdings_value, cash_value, total_portfolio, total_net_worth = \
        calculate_portfolio_values(holdings, cash_available, retirement_401k)
    
    # Calculate allocations and drift
    holdings, cash_weight, cash_drift = calculate_allocations(
        holdings, total_portfolio, cash_available, target_cash_weight
    )
    
    # Find maximum drift
    max_drift, max_drift_symbol = find_max_drift(holdings, cash_drift)
    
    # Check for threshold breach
    has_breach = max_drift > band_threshold
    
    # Calculate effective exposure
    equity_exposure, leverage_exposure = calculate_effective_exposure(holdings)
    
    return PortfolioSummary(
        total_holdings_value=holdings_value,
        cash_value=cash_value,
        total_portfolio_value=total_portfolio,
        retirement_401k_value=retirement_401k.balance if retirement_401k.enabled else 0.0,
        total_net_worth=total_net_worth,
        cash_weight=cash_weight,
        max_drift=max_drift,
        max_drift_symbol=max_drift_symbol,
        has_breach=has_breach,
        breach_threshold=band_threshold,
        effective_equity_exposure=equity_exposure,
        effective_leverage_exposure=leverage_exposure,
        timestamp=timestamp
    )


def get_underweight_holdings(
    holdings: list[Holding],
    cash_weight: float,
    target_cash_weight: float
) -> list[tuple[str, float]]:
    """
    Get list of underweight holdings sorted by drift magnitude.
    Returns list of (symbol, drift) tuples.
    """
    underweight = []
    
    for holding in holdings:
        if holding.drift is not None and holding.drift < 0:
            underweight.append((holding.symbol, holding.drift))
    
    # Include cash if underweight
    cash_drift = cash_weight - target_cash_weight
    if cash_drift < 0:
        underweight.append(("CASH", cash_drift))
    
    # Sort by drift (most underweight first)
    underweight.sort(key=lambda x: x[1])
    
    return underweight


def get_overweight_holdings(
    holdings: list[Holding],
    cash_weight: float,
    target_cash_weight: float
) -> list[tuple[str, float, bool]]:
    """
    Get list of overweight holdings sorted by priority for trimming.
    Returns list of (symbol, drift, is_leveraged) tuples.
    Leveraged holdings are prioritized for trimming.
    """
    overweight = []
    
    for holding in holdings:
        if holding.drift is not None and holding.drift > 0:
            overweight.append((holding.symbol, holding.drift, holding.is_leveraged))
    
    # Include cash if overweight
    cash_drift = cash_weight - target_cash_weight
    if cash_drift > 0:
        overweight.append(("CASH", cash_drift, False))
    
    # Sort: leveraged first, then by drift magnitude
    overweight.sort(key=lambda x: (-int(x[2]), -x[1]))
    
    return overweight


def format_summary_text(summary: PortfolioSummary) -> str:
    """Format portfolio summary as readable text."""
    lines = [
        "=" * 50,
        "PORTFOLIO SUMMARY",
        "=" * 50,
        f"Timestamp: {summary.timestamp}",
        "",
        "VALUES:",
        f"  Holdings Value:    {format_currency(summary.total_holdings_value)}",
        f"  Cash:              {format_currency(summary.cash_value)}",
        f"  Total Portfolio:   {format_currency(summary.total_portfolio_value)}",
    ]
    
    if summary.retirement_401k_value > 0:
        lines.append(f"  401(k) Balance:    {format_currency(summary.retirement_401k_value)}")
        lines.append(f"  Total Net Worth:   {format_currency(summary.total_net_worth)}")
    
    lines.extend([
        "",
        "ALLOCATION:",
        f"  Cash Weight:       {format_percent(summary.cash_weight)}",
        "",
        "DRIFT ANALYSIS:",
        f"  Max Drift:         {format_percent(summary.max_drift)} ({summary.max_drift_symbol})",
        f"  Threshold:         ±{format_percent(summary.breach_threshold)}",
        f"  Status:            {'⚠️  REBALANCE NEEDED' if summary.has_breach else '✓ Within band'}",
        "",
        "EXPOSURE:",
        f"  Effective Equity:  {format_currency(summary.effective_equity_exposure)}",
        f"  Leveraged Assets:  {format_currency(summary.effective_leverage_exposure)}",
        "=" * 50
    ])
    
    return "\n".join(lines)


def format_holdings_table(analyses: list[HoldingAnalysis]) -> str:
    """Format holdings analysis as a table."""
    header = (
        f"{'Symbol':<8} {'Shares':>10} {'Price':>10} {'Value':>12} "
        f"{'Target':>8} {'Actual':>8} {'Drift':>8} {'Status':<12}"
    )
    separator = "-" * len(header)
    
    rows = [header, separator]
    
    for a in analyses:
        price_str = f"${a.current_price:.2f}" if a.current_price else "N/A"
        value_str = f"${a.market_value:,.2f}" if a.market_value else "N/A"
        actual_str = format_percent(a.actual_weight) if a.actual_weight else "N/A"
        drift_str = f"{a.drift*100:+.2f}%" if a.drift is not None else "N/A"
        
        status = "⚠️ BREACH" if a.is_breached else a.drift_direction.upper()
        
        row = (
            f"{a.symbol:<8} {a.shares:>10.2f} {price_str:>10} {value_str:>12} "
            f"{format_percent(a.target_weight):>8} {actual_str:>8} {drift_str:>8} {status:<12}"
        )
        rows.append(row)
    
    return "\n".join(rows)
