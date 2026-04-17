"""
Finance Recommendation Scoring System (0-100)

Evaluates financial health across 6 core categories and generates
scored, actionable recommendations with contextual explanations.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any

from utils import format_currency, format_percent


logger = logging.getLogger('portfolio_automation.scoring')


class ImpactArea(Enum):
    """Core finance categories."""
    CASH_SAFETY = "Cash Safety"
    CASHFLOW = "Cashflow"
    DEBT = "Debt"
    PORTFOLIO_RISK = "Portfolio Risk"
    TAXES = "Taxes"
    FRAUD_SECURITY = "Fraud/Security"


class ActionLevel(Enum):
    """Action urgency levels based on score."""
    FYI = "FYI"                      # 0-24: don't email
    MONITOR = "Monitor"              # 25-49: digest only
    RECOMMENDED = "Recommended"      # 50-74: send
    ACTION_REQUIRED = "Action Required"  # 75-100: send + highlight


@dataclass
class TrendData:
    """Historical trend data for a metric."""
    current_value: float
    previous_values: List[float] = field(default_factory=list)  # Most recent first
    periods_below_threshold: int = 0
    periods_above_threshold: int = 0
    threshold: float = 0.0
    is_increasing_bad: bool = True  # True if increasing values are worse
    
    @property
    def streak(self) -> int:
        """Number of consecutive periods breaching threshold."""
        return max(self.periods_below_threshold, self.periods_above_threshold)
    
    @property
    def is_worsening(self) -> bool:
        """Check if trend is getting worse over last 3 periods."""
        if len(self.previous_values) < 2:
            return False
        
        values = [self.current_value] + self.previous_values[:2]
        if self.is_increasing_bad:
            return values[0] > values[1] > values[2] if len(values) >= 3 else values[0] > values[1]
        else:
            return values[0] < values[1] < values[2] if len(values) >= 3 else values[0] < values[1]
    
    @property
    def has_spike(self) -> bool:
        """Check for sudden large change vs baseline."""
        if not self.previous_values:
            return False
        avg_previous = sum(self.previous_values[:3]) / min(3, len(self.previous_values))
        if avg_previous == 0:
            return False
        change_pct = abs(self.current_value - avg_previous) / abs(avg_previous)
        return change_pct > 0.25  # 25% change from baseline


@dataclass
class ScoringComponents:
    """Breakdown of score components."""
    severity: int = 0          # 0-40: How far from target?
    persistence: int = 0       # 0-25: Is it getting worse?
    impact: int = 0            # 0-25: What's the downside?
    priority: int = 0          # 0-10: User preferences
    confidence: int = 100      # 0-100: Data quality
    
    @property
    def raw_score(self) -> int:
        """Calculate raw score before confidence adjustment."""
        return min(100, self.severity + self.persistence + self.impact + self.priority)
    
    @property
    def final_score(self) -> int:
        """Calculate final score with confidence adjustment."""
        return int(self.raw_score * (self.confidence / 100))


@dataclass
class FinanceRecommendation:
    """A scored, contextual finance recommendation."""
    # Identification
    id: str
    impact_area: ImpactArea
    
    # Scoring
    components: ScoringComponents
    
    # Content (max 10 words for title)
    title: str
    trigger: str           # Exact metric + threshold breach
    what_changed: str      # Current vs target + delta
    why_it_matters: str    # Tie to risk/goal/taxes
    action: str            # Specific action with $ or %
    next_check: str        # Date or "next paycheck / next week"
    evidence: str          # 1-line trend summary
    
    # Metadata
    created_at: datetime = field(default_factory=datetime.now)
    last_sent: Optional[datetime] = None
    
    @property
    def final_score(self) -> int:
        return self.components.final_score
    
    @property
    def action_level(self) -> ActionLevel:
        score = self.final_score
        if score >= 75:
            return ActionLevel.ACTION_REQUIRED
        elif score >= 50:
            return ActionLevel.RECOMMENDED
        elif score >= 25:
            return ActionLevel.MONITOR
        else:
            return ActionLevel.FYI
    
    @property
    def should_email(self) -> bool:
        """Check if this item alone warrants an email."""
        return self.final_score >= 50
    
    def can_resend(self, min_days: int = 7, score_increase_threshold: int = 15) -> bool:
        """Check if recommendation can be sent again (anti-spam)."""
        if self.last_sent is None:
            return True
        
        days_since = (datetime.now() - self.last_sent).days
        if days_since >= min_days:
            return True
        
        # Allow resend if score increased significantly
        # (would need to store previous score - simplified here)
        return False


# =============================================================================
# SEVERITY CALCULATORS (0-40)
# =============================================================================

def calc_severity_savings_rate(current: float, target: float) -> int:
    """Calculate severity for savings rate gap."""
    gap = target - current
    if gap <= 0.02:
        return 5
    elif gap <= 0.05:
        return 15
    elif gap <= 0.10:
        return 25
    else:
        return 40


def calc_severity_emergency_fund(months: float, target_min: float = 3.0) -> int:
    """Calculate severity for emergency fund months."""
    if months >= target_min:
        return int(10 * (1 - min(1, (months - target_min) / 3)))  # 0-10 if above target
    elif months >= 2:
        return 20
    elif months >= 1:
        return 30
    else:
        return 40


def calc_severity_portfolio_drift(drift: float, band: float = 0.07) -> int:
    """Calculate severity for portfolio drift."""
    abs_drift = abs(drift)
    if abs_drift <= band:
        return int(10 * (abs_drift / band))  # 0-10 within band
    elif abs_drift <= band + 0.05:  # 7-12%
        return 25
    else:
        return 40


def calc_severity_debt_utilization(utilization: float, target: float = 0.30) -> int:
    """Calculate severity for debt/credit utilization."""
    if utilization <= target:
        return int(10 * (utilization / target))
    elif utilization <= 0.50:
        return 20
    elif utilization <= 0.75:
        return 30
    else:
        return 40


def calc_severity_budget_variance(variance_pct: float) -> int:
    """Calculate severity for budget overspend."""
    if variance_pct <= 0.05:
        return 5
    elif variance_pct <= 0.10:
        return 15
    elif variance_pct <= 0.20:
        return 25
    else:
        return 40


# =============================================================================
# PERSISTENCE CALCULATOR (0-25)
# =============================================================================

def calc_persistence(trend: TrendData) -> int:
    """Calculate persistence/trend score."""
    score = 0
    
    # Streak scoring
    if trend.streak == 1:
        score += 5
    elif trend.streak <= 3:
        score += 12
    elif trend.streak >= 4:
        score += 18
    
    # Worsening trend
    if trend.is_worsening:
        score += 5
    
    # Spike detection
    if trend.has_spike:
        score += 2
    
    return min(25, score)


# =============================================================================
# IMPACT CALCULATOR (0-25)
# =============================================================================

def calc_impact(area: ImpactArea, severity: int) -> int:
    """Calculate impact score based on category and severity."""
    # Base impact by category
    base_impact = {
        ImpactArea.CASH_SAFETY: 22,
        ImpactArea.FRAUD_SECURITY: 25,
        ImpactArea.DEBT: 20,
        ImpactArea.PORTFOLIO_RISK: 18,
        ImpactArea.TAXES: 18,
        ImpactArea.CASHFLOW: 15,
    }
    
    base = base_impact.get(area, 15)
    
    # Adjust based on severity
    if severity <= 10:
        return int(base * 0.4)
    elif severity <= 25:
        return int(base * 0.7)
    else:
        return base


# =============================================================================
# PRIORITY CALCULATOR (0-10)
# =============================================================================

def calc_priority(user_priority: int) -> int:
    """Convert user priority (1-5) to score (2-10)."""
    mapping = {1: 2, 2: 4, 3: 6, 4: 8, 5: 10}
    return mapping.get(user_priority, 6)


# =============================================================================
# CONFIDENCE CALCULATOR (0-100)
# =============================================================================

def calc_confidence(
    is_auto_imported: bool = True,
    has_manual_entries: bool = False,
    is_partial_period: bool = False,
    missing_categories: int = 0
) -> int:
    """Calculate data confidence score."""
    if is_auto_imported and not has_manual_entries and not is_partial_period:
        return 100
    
    confidence = 100
    
    if has_manual_entries:
        confidence = 70
    
    if is_partial_period or missing_categories > 0:
        confidence = min(confidence, 40 + (10 * (5 - missing_categories)))
    
    return max(0, min(100, confidence))


# =============================================================================
# RECOMMENDATION GENERATORS
# =============================================================================

def create_savings_rate_recommendation(
    current_rate: float,
    target_rate: float,
    trend: TrendData,
    monthly_income: float,
    user_priority: int = 3,
    confidence: int = 100
) -> Optional[FinanceRecommendation]:
    """Generate recommendation for low savings rate."""
    gap = target_rate - current_rate
    
    if gap <= 0.02:  # Within 2% is acceptable
        return None
    
    severity = calc_severity_savings_rate(current_rate, target_rate)
    persistence = calc_persistence(trend)
    impact = calc_impact(ImpactArea.CASHFLOW, severity)
    priority = calc_priority(user_priority)
    
    components = ScoringComponents(
        severity=severity,
        persistence=persistence,
        impact=impact,
        priority=priority,
        confidence=confidence
    )
    
    # Calculate suggested increase
    gap_amount = monthly_income * gap
    suggested_increase = round(gap_amount / 2, -1)  # Start with half the gap, round to $10
    
    return FinanceRecommendation(
        id=f"savings_rate_{date.today().isoformat()}",
        impact_area=ImpactArea.CASHFLOW,
        components=components,
        title="Savings rate below target",
        trigger=f"SavingsRate {current_rate:.1%} < {target_rate:.1%} for {trend.streak} periods",
        what_changed=f"{current_rate:.1%} vs {target_rate:.1%} ({gap:+.1%})",
        why_it_matters="Slows wealth building and reduces financial cushion",
        action=f"Increase auto-transfer by {format_currency(suggested_increase)}/mo OR cut discretionary by {format_currency(suggested_increase/4)}/wk",
        next_check="After next paycheck",
        evidence=f"Down {trend.streak} periods; trend {'worsening' if trend.is_worsening else 'stable'}"
    )


def create_emergency_fund_recommendation(
    current_months: float,
    target_months: float,
    monthly_expenses: float,
    trend: TrendData,
    user_priority: int = 4,
    confidence: int = 100
) -> Optional[FinanceRecommendation]:
    """Generate recommendation for low emergency fund."""
    if current_months >= target_months:
        return None
    
    severity = calc_severity_emergency_fund(current_months, target_months)
    persistence = calc_persistence(trend)
    impact = calc_impact(ImpactArea.CASH_SAFETY, severity)
    priority = calc_priority(user_priority)
    
    components = ScoringComponents(
        severity=severity,
        persistence=persistence,
        impact=impact,
        priority=priority,
        confidence=confidence
    )
    
    gap_months = target_months - current_months
    target_amount = target_months * monthly_expenses
    monthly_contribution = round((gap_months * monthly_expenses) / 12, -1)  # Build over 12 months
    
    return FinanceRecommendation(
        id=f"emergency_fund_{date.today().isoformat()}",
        impact_area=ImpactArea.CASH_SAFETY,
        components=components,
        title="Emergency fund below target",
        trigger=f"EmergencyFund {current_months:.1f} months < {target_months:.0f} month target",
        what_changed=f"{current_months:.1f} vs {target_months:.0f} months ({gap_months:+.1f})",
        why_it_matters="Insufficient buffer for job loss or unexpected expenses",
        action=f"Build to {target_months:.0f} months by adding {format_currency(monthly_contribution)}/mo until {format_currency(target_amount)}",
        next_check="Monthly review",
        evidence=f"Current: {format_currency(current_months * monthly_expenses)}; streak={trend.streak}"
    )


def create_portfolio_drift_recommendation(
    symbol: str,
    drift: float,
    band: float,
    is_leveraged: bool,
    trend: TrendData,
    user_priority: int = 3,
    confidence: int = 100
) -> Optional[FinanceRecommendation]:
    """Generate recommendation for portfolio drift."""
    abs_drift = abs(drift)
    
    if abs_drift <= band:
        return None
    
    severity = calc_severity_portfolio_drift(drift, band)
    persistence = calc_persistence(trend)
    impact = calc_impact(ImpactArea.PORTFOLIO_RISK, severity)
    priority = calc_priority(user_priority)
    
    # Increase priority for leveraged positions
    if is_leveraged:
        priority = min(10, priority + 2)
    
    components = ScoringComponents(
        severity=severity,
        persistence=persistence,
        impact=impact,
        priority=priority,
        confidence=confidence
    )
    
    direction = "overweight" if drift > 0 else "underweight"
    action_verb = "reduce" if drift > 0 else "increase"
    
    if drift > 0 and is_leveraged:
        action = f"Trim {symbol} position first (leveraged); redirect to underweight assets"
    elif drift > 0:
        action = f"Direct new contributions away from {symbol} until drift < {band:.0%}"
    else:
        action = f"Direct next contributions to {symbol} until drift < {band:.0%} (avoid selling)"
    
    return FinanceRecommendation(
        id=f"drift_{symbol}_{date.today().isoformat()}",
        impact_area=ImpactArea.PORTFOLIO_RISK,
        components=components,
        title=f"{symbol} {direction} - rebalance needed",
        trigger=f"Drift {abs_drift:.1%} > +/-{band:.0%} band",
        what_changed=f"{drift:+.1%} from target allocation",
        why_it_matters=f"{'Leveraged position amplifies risk' if is_leveraged else 'Portfolio concentration increases risk'}",
        action=action,
        next_check="Next contribution or weekly review",
        evidence=f"Drift trend: {'worsening' if trend.is_worsening else 'stable'}; {trend.streak} periods"
    )


def create_tax_efficiency_recommendation(
    symbol: str,
    holding_period_days: int,
    potential_gain: float,
    user_priority: int = 3,
    confidence: int = 100
) -> Optional[FinanceRecommendation]:
    """Generate recommendation for tax-inefficient moves."""
    if holding_period_days >= 365 or potential_gain <= 0:
        return None
    
    days_to_ltcg = 365 - holding_period_days
    
    # Severity based on how close to long-term and gain size
    if days_to_ltcg <= 30:
        severity = 15  # Very close, just wait
    elif days_to_ltcg <= 90:
        severity = 25
    else:
        severity = 35
    
    # Adjust for gain size
    if potential_gain > 5000:
        severity = min(40, severity + 5)
    
    components = ScoringComponents(
        severity=severity,
        persistence=0,  # Not a trend-based metric
        impact=calc_impact(ImpactArea.TAXES, severity),
        priority=calc_priority(user_priority),
        confidence=confidence
    )
    
    return FinanceRecommendation(
        id=f"tax_{symbol}_{date.today().isoformat()}",
        impact_area=ImpactArea.TAXES,
        components=components,
        title=f"Avoid selling {symbol} - short-term gain",
        trigger=f"Holding period {holding_period_days} days < 365 with {format_currency(potential_gain)} gain",
        what_changed=f"{days_to_ltcg} days until long-term capital gains rate",
        why_it_matters=f"Short-term gains taxed as ordinary income; potential {format_currency(potential_gain * 0.15)} extra tax",
        action=f"Wait {days_to_ltcg} days for LTCG treatment; use contributions to rebalance instead",
        next_check=f"{(date.today() + timedelta(days=days_to_ltcg)).strftime('%Y-%m-%d')}",
        evidence=f"Purchased {holding_period_days} days ago; {format_currency(potential_gain)} unrealized gain"
    )


def create_overspending_recommendation(
    category: str,
    actual: float,
    budget: float,
    trend: TrendData,
    user_priority: int = 3,
    confidence: int = 100
) -> Optional[FinanceRecommendation]:
    """Generate recommendation for budget overspend."""
    if actual <= budget:
        return None
    
    variance = actual - budget
    variance_pct = variance / budget if budget > 0 else 1.0
    
    severity = calc_severity_budget_variance(variance_pct)
    persistence = calc_persistence(trend)
    impact = calc_impact(ImpactArea.CASHFLOW, severity)
    priority = calc_priority(user_priority)
    
    components = ScoringComponents(
        severity=severity,
        persistence=persistence,
        impact=impact,
        priority=priority,
        confidence=confidence
    )
    
    weekly_reduction = round(variance / 4, 0)
    
    return FinanceRecommendation(
        id=f"budget_{category}_{date.today().isoformat()}",
        impact_area=ImpactArea.CASHFLOW,
        components=components,
        title=f"{category} over budget",
        trigger=f"{category} {format_currency(actual)} > {format_currency(budget)} budget",
        what_changed=f"{format_currency(actual)} vs {format_currency(budget)} ({variance_pct:+.0%})",
        why_it_matters="Reduces savings capacity and may signal lifestyle creep",
        action=f"Cap {category.lower()} to {format_currency(budget/4)}/wk; review top 5 merchants",
        next_check="End of week",
        evidence=f"Over budget {trend.streak} periods; {format_currency(variance)} excess"
    )


# =============================================================================
# RECOMMENDATION AGGREGATION
# =============================================================================

def deduplicate_recommendations(
    recommendations: List[FinanceRecommendation]
) -> List[FinanceRecommendation]:
    """
    Deduplicate recommendations by grouping related issues.
    E.g., "Spending up + savings down" -> one "Cashflow tightening" item.
    """
    # Group by impact area
    by_area: Dict[ImpactArea, List[FinanceRecommendation]] = {}
    for rec in recommendations:
        if rec.impact_area not in by_area:
            by_area[rec.impact_area] = []
        by_area[rec.impact_area].append(rec)
    
    deduplicated = []
    
    for area, recs in by_area.items():
        if len(recs) == 1:
            deduplicated.append(recs[0])
        else:
            # Keep highest scored item, merge context
            recs.sort(key=lambda r: r.final_score, reverse=True)
            primary = recs[0]
            
            # Add note about related issues
            related = [r.title for r in recs[1:]]
            if related:
                primary.evidence += f"; Related: {', '.join(related[:2])}"
            
            deduplicated.append(primary)
    
    return deduplicated


def filter_for_email(
    recommendations: List[FinanceRecommendation],
    max_items: int = 8
) -> List[FinanceRecommendation]:
    """Filter and limit recommendations for email."""
    # Sort by score descending
    sorted_recs = sorted(recommendations, key=lambda r: r.final_score, reverse=True)
    
    # Apply anti-spam: filter items that can't be resent
    eligible = [r for r in sorted_recs if r.can_resend()]
    
    # Limit total items
    return eligible[:max_items]


def should_send_email(
    recommendations: List[FinanceRecommendation],
    is_digest_day: bool = False
) -> bool:
    """Determine if email should be sent based on recommendations."""
    action_required = sum(1 for r in recommendations if r.action_level == ActionLevel.ACTION_REQUIRED)
    recommended = sum(1 for r in recommendations if r.action_level == ActionLevel.RECOMMENDED)
    
    # Send if: >=1 Action Required OR >=2 Recommended OR it's digest day
    if action_required >= 1:
        return True
    if recommended >= 2:
        return True
    if is_digest_day and recommendations:
        return True
    
    return False


# =============================================================================
# GROWTH MODE — NEW SCORING COMPONENTS (Aggressive Wealth Growth Mode)
# Weights: drift_severity(10) + concentration_risk(20) + equity_exposure(25)
#          + contribution_efficiency(25) + expected_cagr(20) = 100
# =============================================================================

def calc_growth_drift_severity(drift: float, band: float = 0.12) -> int:
    """
    Drift severity for growth mode (max 10, down from 40).
    Drift is informational in growth mode; structural violations carry the weight.
    """
    abs_drift = abs(drift)
    if abs_drift <= band:
        return max(0, int(5 * abs_drift / band))  # 0–5 within band
    elif abs_drift <= band * 1.5:
        return 7
    return 10


def calc_concentration_risk_score(actual_weight: float, cap: float = 0.40) -> int:
    """
    Concentration risk score (0–20).
    High when a holding approaches or exceeds the configured cap.
    """
    if cap <= 0:
        return 0
    usage = actual_weight / cap
    if usage >= 1.0:
        return 20
    elif usage >= 0.85:
        return 15
    elif usage >= 0.70:
        return 8
    return 0


def calc_equity_exposure_score(
    is_equity: bool,
    is_underweight: bool,
    is_leveraged: bool = False
) -> int:
    """
    Equity-exposure score (0–25).
    Underweight equity positions score highest — growth mode needs equity.
    """
    if is_equity and is_underweight and not is_leveraged:
        return 20   # Core equity underweight — priority deployment target
    elif is_equity and is_underweight and is_leveraged:
        return 10   # Leveraged equity underweight — less urgent
    elif is_equity and not is_underweight:
        return 5    # Equity overweight — minor flag
    return 0        # Non-equity underweight/overweight — lowest priority


def calc_contribution_efficiency_score(
    has_contribution_plan: bool,
    is_underweight: bool
) -> int:
    """
    Contribution-efficiency score (0–25).
    Low score when a known contribution plan addresses the imbalance.
    High score when no contribution plan exists for an underweight position.
    """
    if is_underweight and not has_contribution_plan:
        return 20   # Gap exists with no plan — needs attention
    elif is_underweight and has_contribution_plan:
        return 8    # Plan in place — routine action
    elif not is_underweight:
        return 5    # Overweight — less urgent than underweight
    return 0


def calc_expected_cagr_score(
    portfolio_cagr: float,
    target_cagr: float = 0.09
) -> int:
    """
    Expected-CAGR score (0–20).
    High when portfolio CAGR is significantly below target.
    """
    gap = target_cagr - portfolio_cagr
    if gap <= 0:
        return 0    # At or above target
    elif gap <= 0.02:
        return 6
    elif gap <= 0.04:
        return 12
    return 20


def compute_growth_portfolio_score(
    drift: float,
    band: float,
    actual_weight: float,
    concentration_cap: float,
    is_equity: bool,
    is_leveraged: bool,
    has_contribution_plan: bool,
    portfolio_cagr: float,
    target_cagr: float = 0.09,
) -> int:
    """
    Compute a portfolio-adjustment score using growth-mode weights.

    Components:
      drift_severity        (0-10)   — reduced; drift is informational
      concentration_risk    (0-20)   — penalty for approaching/exceeding cap
      equity_exposure       (0-25)   — underweight equity scores highest
      contribution_efficiency(0-25) — higher when no plan covers the gap
      expected_cagr         (0-20)   — higher when CAGR lags target

    Total max = 100.
    """
    is_underweight = drift < 0

    score = (
        calc_growth_drift_severity(drift, band)
        + calc_concentration_risk_score(actual_weight, concentration_cap)
        + calc_equity_exposure_score(is_equity, is_underweight, is_leveraged)
        + calc_contribution_efficiency_score(has_contribution_plan, is_underweight)
        + calc_expected_cagr_score(portfolio_cagr, target_cagr)
    )

    # Minor boost for leveraged positions breaching the band
    if is_leveraged and abs(drift) > band:
        score += 8

    return min(100, score)


def create_structural_violation_recommendation(
    symbol: str,
    actual_weight: float,
    cap: float,
    violation_type: str,   # 'concentration' or 'leverage'
    is_taxable: bool,
    trend: Optional[TrendData] = None,
    confidence: int = 100,
) -> FinanceRecommendation:
    """
    Generate a high-urgency recommendation for a structural violation.

    Structural violations are the ONLY cases in Aggressive Wealth Growth Mode
    where trimming/selling is recommended.

    violation_type: 'concentration' or 'leverage'
    """
    excess_pct = actual_weight - cap
    severity = 40   # Always maximum severity for structural violations

    persistence = calc_persistence(trend) if trend else 0
    impact = calc_impact(ImpactArea.PORTFOLIO_RISK, severity)
    priority = 10   # Always highest priority

    components = ScoringComponents(
        severity=severity,
        persistence=persistence,
        impact=impact,
        priority=priority,
        confidence=confidence,
    )

    if violation_type == 'concentration':
        title = f"{symbol} exceeds concentration cap"
        trigger = f"{symbol} weight {actual_weight:.1%} > {cap:.0%} cap"
        what_changed = f"{symbol} at {actual_weight:.1%}; cap is {cap:.0%} ({excess_pct:+.1%} over)"
        why_it_matters = (
            f"Over-concentration amplifies single-stock/sector risk; "
            f"breaches risk guardrail"
        )
        if is_taxable:
            action = (
                f"Trim {symbol} to <{cap:.0%}: prefer lots held >1 year "
                f"(long-term rate), highest cost-basis lots first"
            )
        else:
            action = f"Trim {symbol} to <{cap:.0%} (tax-advantaged account — fewer constraints)"
    else:  # leverage
        title = f"Leveraged exposure exceeds {cap:.0%} cap"
        trigger = f"Total leveraged exposure {actual_weight:.1%} > {cap:.0%} cap"
        what_changed = f"Leveraged exposure at {actual_weight:.1%}; cap is {cap:.0%}"
        why_it_matters = "Leveraged positions amplify drawdowns; excess leverage is the highest-priority trim"
        action = (
            f"Reduce {symbol} until total leveraged exposure < {cap:.0%}; "
            f"trim before core positions regardless of drawdown"
        )

    return FinanceRecommendation(
        id=f"structural_{violation_type}_{symbol}_{date.today().isoformat()}",
        impact_area=ImpactArea.PORTFOLIO_RISK,
        components=components,
        title=title,
        trigger=trigger,
        what_changed=what_changed,
        why_it_matters=why_it_matters,
        action=action,
        next_check="As soon as possible",
        evidence=f"Structural violation — selling is warranted",
    )


def categorize_recommendations(
    recommendations: List[FinanceRecommendation]
) -> Dict[ActionLevel, List[FinanceRecommendation]]:
    """Categorize recommendations by action level."""
    result = {level: [] for level in ActionLevel}
    
    for rec in recommendations:
        result[rec.action_level].append(rec)
    
    # Sort each category by score
    for level in result:
        result[level].sort(key=lambda r: r.final_score, reverse=True)
    
    return result
