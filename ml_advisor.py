"""
ML Advisor Module

Provides learning-assisted decision support through pattern recognition.
Does NOT predict prices - estimates probabilities, confidence, and timing.

Core principle: Rules decide, ML advises.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum

from ml_history import (
    MLHistoryStore, RecommendationRecord, 
    MetricType, TrendDirection, ResolutionType
)

logger = logging.getLogger('portfolio_automation.ml_advisor')


class ConfidenceLevel(Enum):
    """Confidence levels for ML estimates."""
    HIGH = "High"           # 80%+ historical support
    MEDIUM = "Medium"       # 50-80% historical support
    LOW = "Low"             # <50% or insufficient data
    INSUFFICIENT = "Insufficient"  # Not enough data


@dataclass
class PersistenceEstimate:
    """Estimate of whether a condition will persist."""
    probability: float          # 0-1 probability of persisting >= N periods
    confidence: ConfidenceLevel
    expected_periods: float     # Expected number of periods before resolution
    sample_size: int
    explanation: str


@dataclass 
class ActionEffectivenessEstimate:
    """Estimate of whether action improves outcomes."""
    action_benefit_probability: float  # P(action reduces resolution time)
    expected_time_with_action: float   # Expected periods if action taken
    expected_time_without: float       # Expected periods if no action
    confidence: ConfidenceLevel
    sample_size: int
    explanation: str


@dataclass
class AlertFatigueEstimate:
    """Estimate of alert fatigue risk."""
    false_alert_probability: float  # P(resolves without intervention)
    repeat_alert_count: int         # How many times this has been alerted
    should_suppress: bool           # Recommendation to suppress
    confidence: ConfidenceLevel
    explanation: str


@dataclass
class MLAdvisorOutput:
    """Complete ML advisor output for a recommendation."""
    rec_key: str
    symbol: str
    
    # Estimates
    persistence: PersistenceEstimate
    effectiveness: ActionEffectivenessEstimate
    alert_fatigue: AlertFatigueEstimate
    
    # Adjusted confidence
    original_score: int
    adjusted_score: int
    score_adjustment_reason: str
    
    # Regime context
    regime: str  # "Risk-On", "Risk-Off", "Neutral"
    regime_confidence: ConfidenceLevel
    
    # Summary recommendation
    ml_recommendation: str  # "Act Now", "Wait", "Monitor", "Suppress"
    explanation: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'RecKey': self.rec_key,
            'Symbol': self.symbol,
            'PersistenceProbability': f"{self.persistence.probability:.0%}",
            'ExpectedResolutionPeriods': f"{self.persistence.expected_periods:.1f}",
            'ActionBenefitProbability': f"{self.effectiveness.action_benefit_probability:.0%}",
            'FalseAlertProbability': f"{self.alert_fatigue.false_alert_probability:.0%}",
            'OriginalScore': self.original_score,
            'AdjustedScore': self.adjusted_score,
            'MLRecommendation': self.ml_recommendation,
            'Explanation': self.explanation
        }


class MLAdvisor:
    """
    Learning-assisted decision support advisor.
    
    Uses historical patterns to estimate:
    - Probability of persistence
    - Expected time-to-resolution
    - Action effectiveness
    - Alert fatigue risk
    
    Does NOT:
    - Predict prices
    - Make buy/sell decisions
    - Override rule-based thresholds
    """
    
    # Minimum records needed for reliable estimates
    MIN_RECORDS_FOR_ESTIMATE = 10
    MIN_RECORDS_FOR_HIGH_CONFIDENCE = 30
    
    def __init__(self, history_store: MLHistoryStore):
        self.history = history_store
        self._baseline_stats: Dict[str, Any] = {}

    def _get_baseline_stats(self, metric_type: str = None) -> Dict[str, Any]:
        """Get or calculate baseline statistics, keyed by metric_type."""
        cache_key = metric_type or "__all__"
        if cache_key not in self._baseline_stats:
            self._baseline_stats[cache_key] = self.history.calculate_baseline_stats(metric_type)
        return self._baseline_stats[cache_key]
    
    def _calculate_confidence(self, sample_size: int) -> ConfidenceLevel:
        """Determine confidence level based on sample size."""
        if sample_size < self.MIN_RECORDS_FOR_ESTIMATE:
            return ConfidenceLevel.INSUFFICIENT
        elif sample_size >= self.MIN_RECORDS_FOR_HIGH_CONFIDENCE:
            return ConfidenceLevel.HIGH
        elif sample_size >= self.MIN_RECORDS_FOR_ESTIMATE:
            return ConfidenceLevel.MEDIUM
        else:
            return ConfidenceLevel.LOW
    
    def estimate_persistence(
        self,
        rec_key: str,
        metric_type: str,
        asset_class: str,
        current_drift: float,
        streak_length: int,
        trend_direction: str
    ) -> PersistenceEstimate:
        """
        Estimate probability that condition will persist.
        
        Y1 = 1 if condition persisted >= N periods, else 0
        """
        # Find similar historical records
        drift_range = (abs(current_drift) * 0.7, abs(current_drift) * 1.3)
        similar = self.history.get_similar_records(
            metric_type=metric_type,
            asset_class=asset_class,
            drift_range=drift_range,
            resolved_only=True
        )
        
        sample_size = len(similar)
        confidence = self._calculate_confidence(sample_size)
        
        if sample_size < self.MIN_RECORDS_FOR_ESTIMATE:
            # Use priors / heuristics
            persistence_prob = 0.5 + (streak_length * 0.1)  # Longer streaks more likely to persist
            expected_periods = 4.0  # Default assumption
            explanation = f"Insufficient historical data ({sample_size} records). Using streak-based heuristic."
        else:
            # Calculate from history
            persisted_count = sum(1 for r in similar if r.persisted)
            persistence_prob = persisted_count / sample_size
            
            resolution_times = [r.resolution_periods for r in similar if r.resolution_periods]
            expected_periods = sum(resolution_times) / len(resolution_times) if resolution_times else 4.0
            
            # Adjust for trend
            if trend_direction == "Worsening":
                persistence_prob = min(0.95, persistence_prob * 1.2)
                expected_periods *= 1.3
            elif trend_direction == "Improving":
                persistence_prob = max(0.1, persistence_prob * 0.8)
                expected_periods *= 0.7
            
            explanation = f"Based on {sample_size} similar historical records. "
            if streak_length >= 3:
                explanation += f"Extended streak ({streak_length} periods) increases persistence likelihood."
        
        return PersistenceEstimate(
            probability=min(0.99, max(0.01, persistence_prob)),
            confidence=confidence,
            expected_periods=expected_periods,
            sample_size=sample_size,
            explanation=explanation
        )
    
    def estimate_action_effectiveness(
        self,
        rec_key: str,
        metric_type: str,
        asset_class: str,
        adjustment_mode: str,
        has_cash_excess: bool,
        has_contributions: bool
    ) -> ActionEffectivenessEstimate:
        """
        Estimate whether action improves outcomes vs waiting.
        
        Y2 = 1 if action reduced deviation faster than baseline, else 0
        """
        similar = self.history.get_similar_records(
            metric_type=metric_type,
            asset_class=asset_class,
            resolved_only=True
        )
        
        sample_size = len(similar)
        confidence = self._calculate_confidence(sample_size)
        
        if sample_size < self.MIN_RECORDS_FOR_ESTIMATE:
            # Use logical priors based on adjustment mode
            if adjustment_mode == "USE_CASH_EXCESS" and has_cash_excess:
                action_benefit = 0.75
                time_with = 1.0
                time_without = 3.0
            elif adjustment_mode == "CONTRIBUTE_ONLY" and has_contributions:
                action_benefit = 0.60
                time_with = 2.0
                time_without = 4.0
            else:
                action_benefit = 0.50
                time_with = 2.5
                time_without = 3.5
            
            explanation = f"Insufficient data ({sample_size} records). Using mode-based priors."
        else:
            # Split by action taken
            with_action = [r for r in similar if r.action_taken != "None" and r.resolution_periods]
            without_action = [r for r in similar if r.action_taken == "None" and r.resolution_periods]
            
            if with_action and without_action:
                time_with = sum(r.resolution_periods for r in with_action) / len(with_action)
                time_without = sum(r.resolution_periods for r in without_action) / len(without_action)
                action_benefit = len([r for r in with_action if r.action_effective]) / len(with_action)
            else:
                time_with = 2.0
                time_without = 4.0
                action_benefit = 0.50
            
            explanation = f"Based on {len(with_action)} action vs {len(without_action)} no-action cases."
        
        return ActionEffectivenessEstimate(
            action_benefit_probability=action_benefit,
            expected_time_with_action=time_with,
            expected_time_without=time_without,
            confidence=confidence,
            sample_size=sample_size,
            explanation=explanation
        )
    
    def estimate_alert_fatigue(
        self,
        rec_key: str,
        alert_count: int,
        days_since_first_alert: int
    ) -> AlertFatigueEstimate:
        """
        Estimate alert fatigue risk.
        
        Y4 = 1 if repeated alert resolved without intervention
        """
        # Get history for this specific key
        key_records = self.history.get_records_by_key(rec_key)
        resolved_records = [r for r in key_records if r.is_resolved]
        
        if not resolved_records:
            # First time seeing this - low fatigue risk
            return AlertFatigueEstimate(
                false_alert_probability=0.20,
                repeat_alert_count=alert_count,
                should_suppress=False,
                confidence=ConfidenceLevel.LOW,
                explanation="First occurrence - no historical pattern to suppress."
            )
        
        # Calculate false alert rate
        natural_resolutions = sum(1 for r in resolved_records if r.resolution_type == "Natural")
        false_alert_rate = natural_resolutions / len(resolved_records)
        
        # Should suppress if high false alert rate and many repeats
        should_suppress = false_alert_rate > 0.7 and alert_count >= 3
        
        confidence = self._calculate_confidence(len(resolved_records))
        
        if should_suppress:
            explanation = f"{false_alert_rate:.0%} of past alerts resolved naturally. Consider suppressing until threshold breach increases."
        elif false_alert_rate > 0.5:
            explanation = f"{false_alert_rate:.0%} natural resolution rate. Monitor but consider delaying action."
        else:
            explanation = f"Low false alert rate ({false_alert_rate:.0%}). Alert appears actionable."
        
        return AlertFatigueEstimate(
            false_alert_probability=false_alert_rate,
            repeat_alert_count=alert_count,
            should_suppress=should_suppress,
            confidence=confidence,
            explanation=explanation
        )
    
    def detect_regime(self, market_volatility: str = "Medium") -> Tuple[str, ConfidenceLevel]:
        """
        Detect current market regime.
        
        In production, this would use HMM or similar.
        For now, uses simple heuristics.
        """
        # Simplified regime detection
        if market_volatility == "High":
            return "Risk-Off", ConfidenceLevel.MEDIUM
        elif market_volatility == "Low":
            return "Risk-On", ConfidenceLevel.MEDIUM
        else:
            return "Neutral", ConfidenceLevel.LOW
    
    def calculate_score_adjustment(
        self,
        original_score: int,
        persistence: PersistenceEstimate,
        effectiveness: ActionEffectivenessEstimate,
        alert_fatigue: AlertFatigueEstimate,
        regime: str
    ) -> Tuple[int, str]:
        """
        Calculate adjusted score based on ML estimates.
        
        Rules still dominate - ML only adjusts confidence slightly.
        """
        adjustment = 0
        reasons = []
        
        # Persistence adjustment (-5 to +5)
        if persistence.probability > 0.8 and persistence.confidence != ConfidenceLevel.INSUFFICIENT:
            adjustment += 5
            reasons.append("High persistence likelihood (+5)")
        elif persistence.probability < 0.3 and persistence.confidence != ConfidenceLevel.INSUFFICIENT:
            adjustment -= 5
            reasons.append("Likely self-resolving (-5)")
        
        # Effectiveness adjustment (-5 to +5)
        if effectiveness.action_benefit_probability > 0.7:
            adjustment += 3
            reasons.append("Action historically effective (+3)")
        elif effectiveness.action_benefit_probability < 0.3:
            adjustment -= 3
            reasons.append("Action benefit uncertain (-3)")
        
        # Alert fatigue adjustment (-10 to 0)
        if alert_fatigue.should_suppress:
            adjustment -= 10
            reasons.append("High false alert rate (-10)")
        elif alert_fatigue.false_alert_probability > 0.5:
            adjustment -= 5
            reasons.append("Moderate false alert rate (-5)")
        
        # Regime adjustment (-3 to +3)
        if regime == "Risk-Off":
            adjustment += 3
            reasons.append("Risk-off regime (+3)")
        elif regime == "Risk-On":
            adjustment -= 2
            reasons.append("Risk-on regime (-2)")
        
        # Clamp to reasonable range
        adjusted_score = max(0, min(100, original_score + adjustment))
        reason_str = "; ".join(reasons) if reasons else "No adjustment"
        
        return adjusted_score, reason_str
    
    def advise(
        self,
        rec_key: str,
        symbol: str,
        metric_type: str,
        asset_class: str,
        current_drift: float,
        streak_length: int,
        trend_direction: str,
        adjustment_mode: str,
        original_score: int,
        has_cash_excess: bool = False,
        has_contributions: bool = True,
        alert_count: int = 1,
        days_since_first_alert: int = 0,
        market_volatility: str = "Medium"
    ) -> MLAdvisorOutput:
        """
        Generate complete ML advisory output for a recommendation.
        """
        # Get estimates
        persistence = self.estimate_persistence(
            rec_key=rec_key,
            metric_type=metric_type,
            asset_class=asset_class,
            current_drift=current_drift,
            streak_length=streak_length,
            trend_direction=trend_direction
        )
        
        effectiveness = self.estimate_action_effectiveness(
            rec_key=rec_key,
            metric_type=metric_type,
            asset_class=asset_class,
            adjustment_mode=adjustment_mode,
            has_cash_excess=has_cash_excess,
            has_contributions=has_contributions
        )
        
        alert_fatigue = self.estimate_alert_fatigue(
            rec_key=rec_key,
            alert_count=alert_count,
            days_since_first_alert=days_since_first_alert
        )
        
        # Detect regime
        regime, regime_confidence = self.detect_regime(market_volatility)
        
        # Calculate adjusted score
        adjusted_score, adjustment_reason = self.calculate_score_adjustment(
            original_score=original_score,
            persistence=persistence,
            effectiveness=effectiveness,
            alert_fatigue=alert_fatigue,
            regime=regime
        )
        
        # Determine ML recommendation
        if alert_fatigue.should_suppress:
            ml_recommendation = "Suppress"
            explanation = f"High false alert rate ({alert_fatigue.false_alert_probability:.0%}). Recommend suppressing until condition worsens."
        elif effectiveness.action_benefit_probability > 0.7 and persistence.probability > 0.6:
            ml_recommendation = "Act Now"
            explanation = f"Action historically effective ({effectiveness.action_benefit_probability:.0%} benefit). {persistence.expected_periods:.1f} periods expected without action."
        elif persistence.probability < 0.4:
            ml_recommendation = "Wait"
            explanation = f"Likely to self-resolve ({1-persistence.probability:.0%} probability). Monitor for {persistence.expected_periods:.0f} periods."
        else:
            ml_recommendation = "Monitor"
            explanation = f"Mixed signals. {persistence.probability:.0%} persistence probability, {effectiveness.action_benefit_probability:.0%} action benefit."
        
        return MLAdvisorOutput(
            rec_key=rec_key,
            symbol=symbol,
            persistence=persistence,
            effectiveness=effectiveness,
            alert_fatigue=alert_fatigue,
            original_score=original_score,
            adjusted_score=adjusted_score,
            score_adjustment_reason=adjustment_reason,
            regime=regime,
            regime_confidence=regime_confidence,
            ml_recommendation=ml_recommendation,
            explanation=explanation
        )


# =============================================================================
# CLAUDE PROMPT FOR HISTORICAL ANALYSIS
# =============================================================================

HISTORICAL_ANALYSIS_PROMPT = '''You are a financial decision-support analyst.

INPUT:
I will provide historical recommendation records with:
- Metric
- Symbol
- DriftPercent
- ActionTaken
- ResolutionTime
- MarketContext
- CashContext
- FinalOutcome

TASK:
1) Identify recurring patterns where:
   - Action was unnecessary
   - Action improved resolution speed
   - Alerts self-resolved
2) Estimate:
   - Probability of persistence
   - Typical time-to-resolution
3) Recommend:
   - Whether future similar cases should be monitored or acted on
4) Output:
   - Probability estimates
   - Confidence adjustment suggestions
   - Explanation grounded in past behavior

RULES:
- Do not recommend BUY or SELL
- Do not predict prices
- Focus on decision effectiveness
- Use neutral, analytical language

OUTPUT:
A concise structured analysis.
'''


def get_historical_analysis_prompt() -> str:
    """Return the Claude prompt for historical analysis."""
    return HISTORICAL_ANALYSIS_PROMPT


def format_records_for_claude(records: List[RecommendationRecord]) -> str:
    """Format historical records for Claude analysis."""
    if not records:
        return "No historical records available."
    
    lines = ["Historical Recommendation Records:", "=" * 50, ""]
    
    for r in records:
        lines.append(f"Record: {r.record_id}")
        lines.append(f"  Symbol: {r.symbol}")
        lines.append(f"  Metric: {r.metric_type}")
        lines.append(f"  DriftPercent: {r.drift_percent:.2%}")
        lines.append(f"  ActionTaken: {r.action_taken}")
        lines.append(f"  ResolutionTime: {r.resolution_periods} periods")
        lines.append(f"  ResolutionType: {r.resolution_type}")
        lines.append(f"  ActionEffective: {r.action_effective}")
        lines.append(f"  CashExcess: ${r.cash_excess:,.2f}")
        lines.append(f"  MarketVolatility: {r.market_volatility}")
        lines.append("")
    
    return "\n".join(lines)
