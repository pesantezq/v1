"""
Rules-based recommendation engine.
Generates buy/sell/hold recommendations based on portfolio state and rules.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from utils import (
    Holding, RebalanceRules,
    round_currency, format_currency, format_percent
)
from portfolio import (
    PortfolioSummary, HoldingAnalysis,
    get_underweight_holdings, get_overweight_holdings
)


logger = logging.getLogger('portfolio_automation.recommendations')


class ActionType(Enum):
    """Types of recommended actions."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    REBALANCE_ALERT = "REBALANCE_ALERT"
    NO_ACTION = "NO_ACTION"


@dataclass
class Recommendation:
    """A single recommendation for an action."""
    action_type: ActionType
    symbol: str
    shares: Optional[float] = None
    amount: Optional[float] = None
    reason: str = ""
    priority: int = 0
    is_urgent: bool = False


@dataclass
class RecommendationReport:
    """Complete recommendation report."""
    recommendations: list[Recommendation]
    summary_message: str
    has_actions: bool
    has_urgent_actions: bool
    notes: list[str]


def calculate_shares_to_buy(
    available_cash: float,
    price_per_share: float,
    min_shares: int = 1
) -> tuple[int, float]:
    """
    Calculate how many whole shares can be purchased.
    Returns (shares, total_cost).
    """
    if price_per_share <= 0 or available_cash <= 0:
        return 0, 0.0
    
    max_shares = int(available_cash / price_per_share)
    
    if max_shares < min_shares:
        return 0, 0.0
    
    return max_shares, round_currency(max_shares * price_per_share)


def calculate_shares_to_sell(
    target_reduction: float,
    price_per_share: float,
    current_shares: float
) -> tuple[int, float]:
    """
    Calculate shares to sell to achieve target reduction.
    Returns (shares, proceeds).
    """
    if price_per_share <= 0 or target_reduction <= 0:
        return 0, 0.0
    
    shares_needed = int(target_reduction / price_per_share)
    shares_to_sell = min(shares_needed, int(current_shares))
    
    if shares_to_sell <= 0:
        return 0, 0.0
    
    return shares_to_sell, round_currency(shares_to_sell * price_per_share)


def generate_buy_recommendations(
    holdings: list[Holding],
    analyses: list[HoldingAnalysis],
    cash_available: float,
    cash_weight: float,
    target_cash_weight: float,
    rules: RebalanceRules,
    summary: PortfolioSummary
) -> list[Recommendation]:
    """Generate buy recommendations using available cash."""
    recommendations = []
    
    if cash_available <= 0:
        return recommendations
    
    # Get underweight holdings sorted by drift
    underweight = get_underweight_holdings(holdings, cash_weight, target_cash_weight)
    
    if not underweight:
        return recommendations
    
    # Filter out CASH from underweight list
    underweight_assets = [(sym, drift) for sym, drift in underweight if sym != "CASH"]
    
    if not underweight_assets:
        return recommendations
    
    # Use cash to buy most underweight asset
    if rules.use_cash_before_selling and rules.direct_contributions_first:
        most_underweight_symbol, drift = underweight_assets[0]
        
        # Find holding and analysis
        holding = next((h for h in holdings if h.symbol == most_underweight_symbol), None)
        analysis = next((a for a in analyses if a.symbol == most_underweight_symbol), None)
        
        if holding and analysis and holding.current_price and holding.current_price > 0:
            # Reserve target cash percentage
            usable_cash = cash_available
            if target_cash_weight > 0:
                target_cash = summary.total_portfolio_value * target_cash_weight
                usable_cash = max(0, cash_available - target_cash)
            
            shares, cost = calculate_shares_to_buy(usable_cash, holding.current_price)
            
            if shares >= 1:
                recommendations.append(Recommendation(
                    action_type=ActionType.BUY,
                    symbol=most_underweight_symbol,
                    shares=float(shares),
                    amount=cost,
                    reason=f"Most underweight ({format_percent(drift)}). "
                           f"Buy {shares} shares @ {format_currency(holding.current_price)}",
                    priority=1,
                    is_urgent=False
                ))
    
    return recommendations


def generate_sell_recommendations(
    holdings: list[Holding],
    analyses: list[HoldingAnalysis],
    cash_weight: float,
    target_cash_weight: float,
    rules: RebalanceRules,
    summary: PortfolioSummary
) -> list[Recommendation]:
    """Generate sell recommendations when rebalancing is needed."""
    recommendations = []
    
    if not summary.has_breach:
        return recommendations
    
    if rules.avoid_taxable_sales:
        # Only recommend sells if band is actually breached
        if summary.max_drift <= rules.band_threshold:
            return recommendations
    
    # Get overweight holdings (leveraged first)
    overweight = get_overweight_holdings(holdings, cash_weight, target_cash_weight)
    
    if not overweight:
        return recommendations
    
    for symbol, drift, is_leveraged in overweight:
        if symbol == "CASH":
            continue
        
        # Only recommend sell if this specific holding is breached
        if abs(drift) <= rules.band_threshold:
            continue
        
        holding = next((h for h in holdings if h.symbol == symbol), None)
        if not holding or not holding.current_price or holding.shares <= 0:
            continue
        
        # Calculate target reduction
        target_weight_diff = drift
        target_value_reduction = summary.total_portfolio_value * target_weight_diff
        
        shares, proceeds = calculate_shares_to_sell(
            target_value_reduction,
            holding.current_price,
            holding.shares
        )
        
        if shares > 0:
            priority = 1 if is_leveraged and rules.trim_leverage_before_core else 2
            
            reason = f"Overweight by {format_percent(drift)}. "
            if is_leveraged:
                reason += "LEVERAGED - trim first. "
            reason += f"Sell {shares} shares @ {format_currency(holding.current_price)}"
            
            recommendations.append(Recommendation(
                action_type=ActionType.SELL,
                symbol=symbol,
                shares=float(shares),
                amount=proceeds,
                reason=reason,
                priority=priority,
                is_urgent=summary.has_breach
            ))
    
    return recommendations


def generate_hold_recommendations(
    holdings: list[Holding],
    analyses: list[HoldingAnalysis]
) -> list[Recommendation]:
    """Generate hold recommendations for holdings within tolerance."""
    recommendations = []
    
    for analysis in analyses:
        if analysis.drift is not None and not analysis.is_breached:
            recommendations.append(Recommendation(
                action_type=ActionType.HOLD,
                symbol=analysis.symbol,
                reason=f"Within band ({format_percent(analysis.drift)})",
                priority=10
            ))
    
    return recommendations


def generate_alert_recommendations(
    summary: PortfolioSummary,
    rules: RebalanceRules
) -> list[Recommendation]:
    """Generate rebalance alerts if threshold is breached."""
    recommendations = []
    
    if summary.has_breach:
        recommendations.append(Recommendation(
            action_type=ActionType.REBALANCE_ALERT,
            symbol=summary.max_drift_symbol,
            reason=f"Drift of {format_percent(summary.max_drift)} exceeds "
                   f"±{format_percent(summary.breach_threshold)} band",
            priority=0,
            is_urgent=True
        ))
    
    return recommendations


def generate_recommendations(
    holdings: list[Holding],
    analyses: list[HoldingAnalysis],
    summary: PortfolioSummary,
    rules: RebalanceRules,
    cash_available: float,
    cash_weight: float,
    target_cash_weight: float,
    context_notes: Optional[list[str]] = None
) -> RecommendationReport:
    """
    Generate complete recommendation report.
    Main entry point for recommendation engine.
    """
    all_recommendations = []
    notes = context_notes or []
    
    # Check for missing price data
    missing_prices = [h.symbol for h in holdings if h.current_price is None]
    if missing_prices:
        notes.append(f"WARNING: Missing price data for: {', '.join(missing_prices)}")
        notes.append("Drift calculations may be inaccurate. Re-run to fetch remaining prices.")
    
    # Check for panic selling protection
    if rules.panic_sell_protection:
        notes.append("Panic sell protection enabled - no forced liquidations")
    
    # Generate alerts first
    alerts = generate_alert_recommendations(summary, rules)
    all_recommendations.extend(alerts)
    
    # Generate buy recommendations
    buys = generate_buy_recommendations(
        holdings, analyses, cash_available, cash_weight,
        target_cash_weight, rules, summary
    )
    all_recommendations.extend(buys)
    
    # Generate sell recommendations only if breached and rules allow
    if summary.has_breach and not rules.avoid_taxable_sales:
        sells = generate_sell_recommendations(
            holdings, analyses, cash_weight,
            target_cash_weight, rules, summary
        )
        all_recommendations.extend(sells)
    elif summary.has_breach and rules.avoid_taxable_sales:
        notes.append("Taxable sales avoided - consider directing new contributions")
    
    # Generate hold recommendations
    holds = generate_hold_recommendations(holdings, analyses)
    all_recommendations.extend(holds)
    
    # Sort by priority
    all_recommendations.sort(key=lambda r: r.priority)
    
    # Determine if there are actionable items
    has_actions = any(
        r.action_type in [ActionType.BUY, ActionType.SELL, ActionType.REBALANCE_ALERT]
        for r in all_recommendations
    )
    
    has_urgent = any(r.is_urgent for r in all_recommendations)
    
    # Generate summary message
    if not has_actions:
        summary_message = "NO ACTION REQUIRED - Portfolio is within target bands"
    elif has_urgent:
        summary_message = "⚠️  REBALANCE ALERT - Action recommended"
    else:
        summary_message = "Opportunities available - Review recommendations"
    
    return RecommendationReport(
        recommendations=all_recommendations,
        summary_message=summary_message,
        has_actions=has_actions,
        has_urgent_actions=has_urgent,
        notes=notes
    )


def format_recommendations_text(report: RecommendationReport) -> str:
    """Format recommendations report as readable text."""
    lines = [
        "=" * 50,
        "RECOMMENDATIONS",
        "=" * 50,
        "",
        report.summary_message,
        ""
    ]
    
    # Group by action type
    action_groups = {}
    for rec in report.recommendations:
        action_type = rec.action_type.value
        if action_type not in action_groups:
            action_groups[action_type] = []
        action_groups[action_type].append(rec)
    
    # Display in priority order
    priority_order = [
        ActionType.REBALANCE_ALERT.value,
        ActionType.BUY.value,
        ActionType.SELL.value,
        ActionType.HOLD.value,
        ActionType.NO_ACTION.value
    ]
    
    for action_type in priority_order:
        if action_type not in action_groups:
            continue
        
        recs = action_groups[action_type]
        lines.append(f"\n{action_type}:")
        lines.append("-" * 40)
        
        for rec in recs:
            if rec.shares and rec.amount:
                lines.append(
                    f"  {rec.symbol}: {rec.shares:.0f} shares "
                    f"(~{format_currency(rec.amount)})"
                )
            else:
                lines.append(f"  {rec.symbol}")
            
            if rec.reason:
                lines.append(f"    → {rec.reason}")
    
    if report.notes:
        lines.append("\n" + "-" * 40)
        lines.append("NOTES:")
        for note in report.notes:
            lines.append(f"  • {note}")
    
    lines.append("\n" + "=" * 50)
    
    return "\n".join(lines)


def get_action_summary(report: RecommendationReport) -> dict:
    """Get summary of actions for email report."""
    summary = {
        'total_buy_amount': 0.0,
        'total_sell_amount': 0.0,
        'buy_count': 0,
        'sell_count': 0,
        'alert_count': 0,
        'hold_count': 0
    }
    
    for rec in report.recommendations:
        if rec.action_type == ActionType.BUY:
            summary['buy_count'] += 1
            if rec.amount:
                summary['total_buy_amount'] += rec.amount
        elif rec.action_type == ActionType.SELL:
            summary['sell_count'] += 1
            if rec.amount:
                summary['total_sell_amount'] += rec.amount
        elif rec.action_type == ActionType.REBALANCE_ALERT:
            summary['alert_count'] += 1
        elif rec.action_type == ActionType.HOLD:
            summary['hold_count'] += 1
    
    return summary