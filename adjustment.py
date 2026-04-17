"""
Portfolio Adjustment Module

Consolidates BUY/REBALANCE_ALERT into unified Portfolio Adjustment recommendations
with smart cash-before-selling and tax-aware logic.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Optional, List, Dict, Any

from utils import format_currency, format_percent, Holding


logger = logging.getLogger('portfolio_automation.adjustment')


class RecommendationType(Enum):
    """Types of recommendations."""
    PORTFOLIO_ADJUSTMENT = "PORTFOLIO_ADJUSTMENT"
    CASH_MANAGEMENT = "CASH_MANAGEMENT"
    TAX_OPTIMIZATION = "TAX_OPTIMIZATION"
    RISK_ALERT = "RISK_ALERT"


class AdjustmentMode(Enum):
    """How to execute the adjustment."""
    CONTRIBUTE_ONLY = "CONTRIBUTE_ONLY"           # Use future contributions
    USE_CASH_EXCESS = "USE_CASH_EXCESS"           # Buy with available cash
    TRIM_LEVERAGE_FIRST = "TRIM_LEVERAGE_FIRST"   # Reduce leveraged positions
    SELL_TO_REBALANCE = "SELL_TO_REBALANCE"       # Sell overweight assets
    NO_ACTION = "NO_ACTION"                        # Hold current position


class ActionLevel(Enum):
    """Action urgency levels."""
    ACTION_REQUIRED = "ACTION_REQUIRED"
    RECOMMENDED = "RECOMMENDED"
    MONITOR = "MONITOR"
    FYI = "FYI"


@dataclass
class CashAnalysis:
    """Analysis of available cash and reserves."""
    available_cash: float
    cash_reserve_target: float
    cash_excess: float
    monthly_contribution: float = 0.0
    months_to_fix_via_contributions: float = 0.0
    
    @classmethod
    def calculate(
        cls,
        available_cash: float,
        total_portfolio: float,
        target_cash_pct: float = 0.05,
        monthly_expenses: float = 0.0,
        emergency_months: float = 3.0,
        monthly_contribution: float = 0.0
    ) -> 'CashAnalysis':
        """Calculate cash analysis from portfolio data."""
        # Use higher of: percentage-based or expense-based reserve
        pct_reserve = total_portfolio * target_cash_pct
        expense_reserve = monthly_expenses * emergency_months
        cash_reserve_target = max(pct_reserve, expense_reserve)
        
        cash_excess = max(0, available_cash - cash_reserve_target)
        
        return cls(
            available_cash=available_cash,
            cash_reserve_target=cash_reserve_target,
            cash_excess=cash_excess,
            monthly_contribution=monthly_contribution
        )


@dataclass
class TaxContext:
    """Tax-related context for an asset."""
    is_taxable_account: bool = True
    holding_period_days: int = 0
    is_long_term: bool = False
    unrealized_gain: float = 0.0
    cost_basis: float = 0.0
    
    @property
    def is_short_term_gain(self) -> bool:
        """Check if selling would trigger short-term capital gains."""
        return self.is_taxable_account and not self.is_long_term and self.unrealized_gain > 0


@dataclass
class PortfolioAdjustment:
    """A consolidated portfolio adjustment recommendation."""
    # Key for deduplication
    rec_key: str  # Format: DRIFT_SYMBOL or METRIC_SYMBOL
    
    # Classification
    recommendation_type: RecommendationType
    adjustment_mode: AdjustmentMode
    action_level: ActionLevel
    
    # Target
    symbol: str
    
    # Scoring
    final_score: int
    
    # Content
    title: str
    what: str
    why: str
    do: str
    next_check: str
    
    # Optional trade details
    shares: Optional[float] = None
    amount: Optional[float] = None
    
    # Metadata
    timestamp: str = field(default_factory=lambda: datetime.now().strftime('%Y-%m-%d %H:%M'))
    
    # Context
    drift: Optional[float] = None
    band: Optional[float] = None
    is_leveraged: bool = False
    tax_context: Optional[TaxContext] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for CSV export."""
        return {
            'RecKey': self.rec_key,
            'ActionLevel': self.action_level.value,
            'RecommendationType': self.recommendation_type.value,
            'AdjustmentMode': self.adjustment_mode.value,
            'Symbol': self.symbol,
            'Shares': self.shares or '',
            'Amount': self.amount or '',
            'FinalScore': self.final_score,
            'What': self.what,
            'Why': self.why,
            'Do': self.do,
            'NextCheck': self.next_check,
            'Timestamp': self.timestamp,
            'Drift': f"{self.drift:.2%}" if self.drift else '',
            'Band': f"{self.band:.2%}" if self.band else '',
            'IsLeveraged': self.is_leveraged
        }


def determine_adjustment_mode(
    drift: float,
    band: float,
    cash_analysis: CashAnalysis,
    is_leveraged: bool,
    tax_context: Optional[TaxContext],
    has_regular_contributions: bool = True,
    suppress_sells: bool = False,
    growth_mode: bool = False,
) -> tuple[AdjustmentMode, str]:
    """
    Determine the best adjustment mode based on rules.
    Returns (mode, explanation).
    
    Priority order:
    1. Never sell if contributions can fix it
    2. Use cash excess before selling
    3. Trim leverage before core holdings
    4. Only sell when necessary
    5. Tax-aware lot selection when selling
    """
    is_underweight = drift < 0
    is_overweight = drift > 0
    drift_magnitude = abs(drift)
    is_breached = drift_magnitude > band
    
    # =========================================
    # UNDERWEIGHT POSITIONS - BUY LOGIC
    # =========================================
    if is_underweight:
        # Rule 2: Use cash excess first (underweight)
        if cash_analysis.cash_excess > 0:
            return (
                AdjustmentMode.USE_CASH_EXCESS,
                f"Buy using {format_currency(cash_analysis.cash_excess)} cash excess (keeps reserve intact)"
            )
        
        # Rule 1: Use contributions if possible (underweight)
        if has_regular_contributions and cash_analysis.monthly_contribution > 0:
            # Estimate months to fix
            target_amount = drift_magnitude * (cash_analysis.available_cash + cash_analysis.cash_reserve_target) / band
            months_to_fix = target_amount / cash_analysis.monthly_contribution if cash_analysis.monthly_contribution > 0 else float('inf')
            
            if months_to_fix <= 6:  # Fixable within 6 months
                return (
                    AdjustmentMode.CONTRIBUTE_ONLY,
                    f"Direct next contributions to this asset (~{months_to_fix:.0f} months to target)"
                )
        
        # Default for underweight - suggest contributions
        if has_regular_contributions:
            return (
                AdjustmentMode.CONTRIBUTE_ONLY,
                "Direct future contributions to underweight assets"
            )
    
    # =========================================
    # OVERWEIGHT POSITIONS - SELL/TRIM LOGIC
    # =========================================
    if is_overweight:
        # In growth mode, selling is disabled for regular drift (structural violations
        # are handled separately in detect_structural_violations).
        if growth_mode:
            return (
                AdjustmentMode.CONTRIBUTE_ONLY,
                "Overweight in growth mode: redirect contributions; selling disabled for non-structural drift"
            )

        # Anti-panic gating: suppress sells during significant drawdowns
        if suppress_sells:
            return (
                AdjustmentMode.CONTRIBUTE_ONLY,
                "Sell suppressed (drawdown > 20%): redirect contributions away from overweight position"
            )

        # Rule 3: Trim leverage first (overweight leveraged position)
        if is_leveraged and is_breached:
            return (
                AdjustmentMode.TRIM_LEVERAGE_FIRST,
                "Reduce leveraged position before trimming core holdings"
            )

        # For non-leveraged overweight, try to fix via contribution redirection first
        if has_regular_contributions and not is_breached:
            return (
                AdjustmentMode.CONTRIBUTE_ONLY,
                "Redirect contributions away from overweight assets"
            )

        # Rule 4: Sell only when band is breached AND other options exhausted
        if is_breached:
            # Check tax implications
            if tax_context and tax_context.is_short_term_gain:
                days_to_long_term = 365 - tax_context.holding_period_days
                if days_to_long_term <= 60:
                    return (
                        AdjustmentMode.NO_ACTION,
                        f"Wait {days_to_long_term} days for long-term treatment before selling"
                    )

            # Prefer redirecting contributions for moderate breaches
            if has_regular_contributions and drift_magnitude <= band * 2.5:
                return (
                    AdjustmentMode.CONTRIBUTE_ONLY,
                    "Redirect all contributions away from overweight position; avoid selling"
                )

            # For severe breaches (>2.5x band) without contributions, recommend sell
            if not has_regular_contributions:
                return (
                    AdjustmentMode.SELL_TO_REBALANCE,
                    "Sell to restore within band (prefer long-term lots, highest cost basis)"
                )

            # Severe breach with contributions — still prefer contributions but flag urgency
            return (
                AdjustmentMode.CONTRIBUTE_ONLY,
                "Redirect all contributions away; consider trimming if drift persists 2+ months"
            )

    # No action needed
    return (
        AdjustmentMode.NO_ACTION,
        "Position within acceptable range"
    )


def calculate_trade_size(
    drift: float,
    total_portfolio: float,
    current_price: float,
    cash_analysis: CashAnalysis,
    mode: AdjustmentMode
) -> tuple[Optional[int], Optional[float]]:
    """
    Calculate recommended trade size (shares and amount).
    Returns (shares, amount) or (None, None) if no trade.
    """
    if mode == AdjustmentMode.NO_ACTION:
        return None, None
    
    target_value_change = abs(drift) * total_portfolio
    
    if mode == AdjustmentMode.USE_CASH_EXCESS:
        # Use available cash excess, up to what's needed
        amount = min(cash_analysis.cash_excess, target_value_change)
        shares = int(amount / current_price) if current_price > 0 else 0
        if shares < 1:
            return None, None
        return shares, shares * current_price
    
    elif mode == AdjustmentMode.CONTRIBUTE_ONLY:
        # Suggest contribution amount
        amount = min(target_value_change, cash_analysis.monthly_contribution * 3)
        shares = int(amount / current_price) if current_price > 0 else 0
        return shares if shares > 0 else None, amount if amount > 0 else None
    
    elif mode in [AdjustmentMode.SELL_TO_REBALANCE, AdjustmentMode.TRIM_LEVERAGE_FIRST]:
        # Calculate sell amount
        amount = target_value_change
        shares = int(amount / current_price) if current_price > 0 else 0
        if shares < 1:
            return None, None
        return shares, shares * current_price
    
    return None, None


def create_portfolio_adjustment(
    symbol: str,
    drift: float,
    band: float,
    current_price: float,
    total_portfolio: float,
    cash_analysis: CashAnalysis,
    is_leveraged: bool = False,
    tax_context: Optional[TaxContext] = None,
    has_regular_contributions: bool = True,
    base_score: int = 50,
    suppress_sells: bool = False,
    growth_mode: bool = False,
) -> Optional[PortfolioAdjustment]:
    """
    Create a consolidated portfolio adjustment recommendation.
    Returns None if no adjustment needed.
    """
    drift_magnitude = abs(drift)
    is_breached = drift_magnitude > band
    is_overweight = drift > 0
    is_underweight = drift < 0
    
    # Skip if within band and not significant
    if not is_breached and drift_magnitude < band * 0.5:
        return None
    
    # Determine mode
    mode, mode_explanation = determine_adjustment_mode(
        drift=drift,
        band=band,
        cash_analysis=cash_analysis,
        is_leveraged=is_leveraged,
        tax_context=tax_context,
        has_regular_contributions=has_regular_contributions,
        suppress_sells=suppress_sells,
        growth_mode=growth_mode,
    )
    
    if mode == AdjustmentMode.NO_ACTION and not is_breached:
        return None
    
    # Calculate trade size
    shares, amount = calculate_trade_size(
        drift=drift,
        total_portfolio=total_portfolio,
        current_price=current_price,
        cash_analysis=cash_analysis,
        mode=mode
    )
    
    # Determine action level based on score and breach
    if growth_mode:
        # In growth mode: drift is informational only; structural violations
        # are handled separately with ACTION_REQUIRED. Regular drift is MONITOR/FYI.
        if is_breached:
            action_level = ActionLevel.MONITOR
            score_boost = 5
        else:
            action_level = ActionLevel.FYI
            score_boost = 0
    elif is_breached:
        if drift_magnitude > band * 2:
            action_level = ActionLevel.ACTION_REQUIRED
            score_boost = 30
        else:
            action_level = ActionLevel.RECOMMENDED
            score_boost = 15
    else:
        action_level = ActionLevel.MONITOR
        score_boost = 0

    # Boost score for leveraged positions
    if is_leveraged:
        score_boost += 10

    final_score = min(100, base_score + score_boost)
    
    # Build recommendation content
    direction = "underweight" if drift < 0 else "overweight"
    
    title = f"Rebalance {symbol} (drift {drift:+.0%} vs +/-{band:.0%})"
    
    what = f"{symbol} is {direction} by {drift_magnitude:.1%} (target band: +/-{band:.0%})"
    
    if is_leveraged:
        why = f"Leveraged position amplifies portfolio risk; {direction} creates concentration"
    elif is_breached:
        why = f"Drift exceeds rebalance threshold; {direction} position increases risk"
    else:
        why = f"Approaching rebalance threshold; monitor for action"
    
    # Build the "Do" instruction based on mode
    if mode == AdjustmentMode.USE_CASH_EXCESS and shares and amount:
        do = f"Buy {shares} shares of {symbol} using cash excess (~{format_currency(amount)})"
    elif mode == AdjustmentMode.CONTRIBUTE_ONLY:
        if is_overweight:
            # Overweight - redirect contributions AWAY
            do = f"Redirect all contributions away from {symbol}; direct to underweight assets"
        else:
            # Underweight - direct contributions TO
            if amount:
                do = f"Direct next contributions to {symbol} (~{format_currency(amount)} needed)"
            else:
                do = f"Direct next contributions to {symbol} until drift < {band:.0%}"
    elif mode == AdjustmentMode.TRIM_LEVERAGE_FIRST and shares and amount:
        do = f"Trim {shares} shares of {symbol} (~{format_currency(amount)}); reduce leverage before core holdings"
    elif mode == AdjustmentMode.SELL_TO_REBALANCE and shares and amount:
        do = f"Sell {shares} shares of {symbol} (~{format_currency(amount)}); prefer long-term lots, highest cost basis"
    elif mode == AdjustmentMode.NO_ACTION:
        if tax_context and tax_context.is_short_term_gain:
            days = 365 - tax_context.holding_period_days
            do = f"Wait {days} days for long-term capital gains treatment before rebalancing"
        else:
            do = "No action needed; position within acceptable range"
    else:
        do = mode_explanation
    
    # Next check
    if is_breached:
        next_check = "Next contribution or weekly review"
    else:
        next_check = "Monthly review"
    
    return PortfolioAdjustment(
        rec_key=f"DRIFT_{symbol}",
        recommendation_type=RecommendationType.PORTFOLIO_ADJUSTMENT,
        adjustment_mode=mode,
        action_level=action_level,
        symbol=symbol,
        final_score=final_score,
        title=title,
        what=what,
        why=why,
        do=do,
        next_check=next_check,
        shares=shares,
        amount=amount,
        drift=drift,
        band=band,
        is_leveraged=is_leveraged,
        tax_context=tax_context
    )


def detect_structural_violations(
    holdings: list,
    analyses: list,
    total_portfolio: float,
    concentration_cap: float = 0.40,
    leverage_cap: float = 0.15,
    is_taxable: bool = True,
) -> List[PortfolioAdjustment]:
    """
    Detect concentration and leverage cap structural violations.

    These are the ONLY cases where sell/trim is recommended in growth mode.
    Returns PortfolioAdjustment objects with ACTION_REQUIRED level.

    Args:
        holdings: Holding objects (actual_weight must be set).
        analyses: HoldingAnalysis objects.
        total_portfolio: Total portfolio value.
        concentration_cap: Max weight for any single holding (default 0.40).
        leverage_cap: Max total leveraged exposure (default 0.15).
        is_taxable: Whether account is taxable (affects tax guidance).

    Returns:
        List of PortfolioAdjustment objects for each violation found.
    """
    violations: List[PortfolioAdjustment] = []

    # ── Concentration violations (per-holding) ────────────────────────────────
    for holding, analysis in zip(holdings, analyses):
        actual_weight = holding.actual_weight or 0.0
        if actual_weight <= concentration_cap:
            continue
        if holding.current_price is None:
            continue

        excess_pct = actual_weight - concentration_cap
        trim_value = excess_pct * total_portfolio
        trim_shares = int(trim_value / holding.current_price) if holding.current_price > 0 else 0

        tax_note = (
            " (prefer lots held >1 year, highest cost-basis first)" if is_taxable else ""
        )

        violations.append(PortfolioAdjustment(
            rec_key=f"CONCENTRATION_{holding.symbol}",
            recommendation_type=RecommendationType.RISK_ALERT,
            adjustment_mode=AdjustmentMode.SELL_TO_REBALANCE,
            action_level=ActionLevel.ACTION_REQUIRED,
            symbol=holding.symbol,
            final_score=90,
            title=f"STRUCTURAL: {holding.symbol} exceeds {concentration_cap:.0%} concentration cap",
            what=(
                f"{holding.symbol} is {actual_weight:.1%} of portfolio "
                f"(cap: {concentration_cap:.0%}, excess: {excess_pct:+.1%})"
            ),
            why=(
                "Over-concentration amplifies single-holding risk; "
                "this is a structural guardrail breach"
            ),
            do=(
                f"Trim {trim_shares or 'some'} shares of {holding.symbol} "
                f"(~{format_currency(trim_value)}){tax_note} "
                f"to bring weight below {concentration_cap:.0%}"
            ),
            next_check="As soon as possible",
            shares=trim_shares if trim_shares > 0 else None,
            amount=trim_value,
            drift=actual_weight - holding.target_weight,
            band=concentration_cap,
            is_leveraged=holding.is_leveraged,
        ))

    # ── Leverage cap violation (aggregate) ───────────────────────────────────
    total_leveraged_exposure = 0.0
    leveraged_holdings = []
    for holding in holdings:
        if holding.is_leveraged:
            weight = holding.actual_weight or 0.0
            exposure = weight * holding.leverage_factor
            total_leveraged_exposure += exposure
            leveraged_holdings.append((holding, weight))

    if total_leveraged_exposure > leverage_cap and leveraged_holdings:
        # Target: trim the most-overweight leveraged position first
        leveraged_holdings.sort(key=lambda hw: hw[1], reverse=True)
        primary_holding, primary_weight = leveraged_holdings[0]

        excess_exposure = total_leveraged_exposure - leverage_cap
        trim_value = (excess_exposure / primary_holding.leverage_factor) * total_portfolio
        trim_shares = (
            int(trim_value / primary_holding.current_price)
            if primary_holding.current_price and primary_holding.current_price > 0
            else None
        )

        violations.append(PortfolioAdjustment(
            rec_key=f"LEVERAGE_CAP_{primary_holding.symbol}",
            recommendation_type=RecommendationType.RISK_ALERT,
            adjustment_mode=AdjustmentMode.TRIM_LEVERAGE_FIRST,
            action_level=ActionLevel.ACTION_REQUIRED,
            symbol=primary_holding.symbol,
            final_score=95,
            title=f"STRUCTURAL: Leveraged exposure {total_leveraged_exposure:.1%} exceeds {leverage_cap:.0%} cap",
            what=(
                f"Total leveraged exposure: {total_leveraged_exposure:.1%} "
                f"(cap: {leverage_cap:.0%}, excess: {total_leveraged_exposure - leverage_cap:+.1%})"
            ),
            why=(
                "Leveraged positions amplify drawdowns; "
                "this is the highest-priority trim — applies even during market dips"
            ),
            do=(
                f"Trim {primary_holding.symbol}"
                + (f" ~{trim_shares} shares (~{format_currency(trim_value)})" if trim_shares else "")
                + f" to bring total leveraged exposure below {leverage_cap:.0%}"
            ),
            next_check="As soon as possible",
            shares=trim_shares,
            amount=trim_value,
            drift=primary_weight - primary_holding.target_weight,
            band=leverage_cap,
            is_leveraged=True,
        ))

    if violations:
        logger.warning(f"Detected {len(violations)} structural violation(s)")

    return violations


def generate_portfolio_adjustments(
    holdings: list,
    analyses: list,
    total_portfolio: float,
    cash_available: float,
    target_cash_pct: float,
    band: float,
    monthly_expenses: float = 0.0,
    monthly_contribution: float = 0.0,
    has_regular_contributions: bool = True,
    growth_mode: bool = False,
    suppress_sells: bool = False,
    concentration_cap: float = 0.40,
    leverage_cap: float = 0.15,
    is_taxable: bool = True,
) -> List[PortfolioAdjustment]:
    """
    Generate consolidated portfolio adjustment recommendations.
    One recommendation per symbol, highest priority only.

    In growth_mode=True:
    - Structural violations (concentration/leverage) are always included.
    - Selling is suppressed for non-structural drift except when leverage cap
      is breached (leverage violations bypass suppress_sells).
    - Regular drift recommendations are downgraded to MONITOR level.
    """
    # Calculate cash analysis
    cash_analysis = CashAnalysis.calculate(
        available_cash=cash_available,
        total_portfolio=total_portfolio,
        target_cash_pct=target_cash_pct,
        monthly_expenses=monthly_expenses,
        monthly_contribution=monthly_contribution
    )

    adjustments = []

    # ── Structural violations first (growth mode) ─────────────────────────────
    if growth_mode:
        violations = detect_structural_violations(
            holdings=holdings,
            analyses=analyses,
            total_portfolio=total_portfolio,
            concentration_cap=concentration_cap,
            leverage_cap=leverage_cap,
            is_taxable=is_taxable,
        )
        adjustments.extend(violations)

    # ── Regular drift adjustments ─────────────────────────────────────────────
    for holding, analysis in zip(holdings, analyses):
        if analysis.drift is None or holding.current_price is None:
            continue

        # In growth mode, suppress regular drift sells when anti-panic is active
        effective_suppress_sells = suppress_sells
        if growth_mode:
            # Determine if this holding already has a structural violation rec
            violation_keys = {a.symbol for a in adjustments if 'STRUCTURAL' in a.title}
            if holding.symbol in violation_keys:
                continue  # Already covered by structural rec — skip

        adjustment = create_portfolio_adjustment(
            symbol=holding.symbol,
            drift=analysis.drift,
            band=band,
            current_price=holding.current_price,
            total_portfolio=total_portfolio,
            cash_analysis=cash_analysis,
            is_leveraged=holding.is_leveraged,
            tax_context=None,
            has_regular_contributions=has_regular_contributions,
            suppress_sells=effective_suppress_sells,
            growth_mode=growth_mode,
        )

        if adjustment:
            adjustments.append(adjustment)

    # Sort by score descending
    adjustments.sort(key=lambda a: a.final_score, reverse=True)

    # Deduplicate by rec_key (keep highest score)
    seen_keys: set = set()
    unique_adjustments = []
    for adj in adjustments:
        if adj.rec_key not in seen_keys:
            seen_keys.add(adj.rec_key)
            unique_adjustments.append(adj)

    logger.info(f"Generated {len(unique_adjustments)} portfolio adjustments")

    return unique_adjustments


def format_adjustments_for_email_view(
    adjustments: List[PortfolioAdjustment]
) -> List[Dict[str, Any]]:
    """
    Format adjustments for EmailView export.
    Filters out FYI items and limits to 8 total.
    """
    # Filter out FYI
    email_items = [
        a for a in adjustments 
        if a.action_level != ActionLevel.FYI
    ]
    
    # Sort by action level priority, then score
    level_priority = {
        ActionLevel.ACTION_REQUIRED: 0,
        ActionLevel.RECOMMENDED: 1,
        ActionLevel.MONITOR: 2
    }
    
    email_items.sort(key=lambda a: (level_priority.get(a.action_level, 99), -a.final_score))
    
    # Limit to 8 items
    email_items = email_items[:8]
    
    return [a.to_dict() for a in email_items]


# =============================================================================
# CLAUDE PROMPT FOR EMAIL GENERATION
# =============================================================================

CLAUDE_EMAIL_PROMPT = '''You are a finance recommendations email writer.

INPUT:
I will provide rows from an Excel sheet called EmailView with columns:
- ActionLevel (ACTION_REQUIRED / RECOMMENDED / MONITOR)
- RecommendationType (e.g., PORTFOLIO_ADJUSTMENT)
- Symbol
- Shares (optional)
- Amount (optional)
- FinalScore (0-100)
- What
- Why
- Do
- NextCheck
- Timestamp

TASK:
Write a single email draft with:
1) A subject line following:
   "Portfolio Update: <# Action Required> Required • <# Recommended> Recommended"
2) A body that is short, scannable, and high signal-to-noise:
   - Start with a 3-line summary:
     - "Items: X Required, Y Recommended"
     - "Primary theme: <1 phrase>" (e.g., "Rebalance underweight positions")
     - "Next review: <date/time from NextCheck>"
   - Then sections in this order:
     A) ACTION REQUIRED (max 3 items)
     B) RECOMMENDED (max 5 items)
     C) MONITOR (max 3 items, only if non-empty)
3) Each item must be formatted in exactly 3 bullets:
   - What: <use What>
   - Why: <use Why>
   - Do: <use Do + shares/amount if provided>
   End each item with "Next check: <NextCheck>"

RULES:
- Do not include FYI/HOLD items.
- Do not include more than 8 total items.
- If multiple items refer to the same Symbol and RecommendationType, keep only the highest FinalScore one.
- Keep tone calm, rules-based, long-term oriented, and tax-aware (avoid frequent trading language).
- Do not invent numbers. Use only provided fields.

OUTPUT:
Return the email text only (no explanations), with Subject first on its own line.
'''


def get_email_prompt() -> str:
    """Return the Claude prompt for email generation."""
    return CLAUDE_EMAIL_PROMPT
