"""
Finance Analyzer Module

Integrates the scoring system with portfolio data to generate
comprehensive financial health recommendations.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any

from utils import Holding, Config, format_currency
from portfolio import PortfolioSummary, HoldingAnalysis
from scoring import (
    FinanceRecommendation, TrendData, ImpactArea, ActionLevel,
    create_savings_rate_recommendation,
    create_emergency_fund_recommendation,
    create_portfolio_drift_recommendation,
    create_tax_efficiency_recommendation,
    create_overspending_recommendation,
    deduplicate_recommendations,
    calc_confidence
)


logger = logging.getLogger('portfolio_automation.finance_analyzer')


@dataclass
class FinanceSnapshot:
    """Point-in-time snapshot of financial metrics."""
    date: str
    portfolio_value: float
    cash_available: float
    emergency_fund_months: float
    savings_rate: float
    max_drift: float
    max_drift_symbol: str
    drifts_by_symbol: Dict[str, float] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            'date': self.date,
            'portfolio_value': self.portfolio_value,
            'cash_available': self.cash_available,
            'emergency_fund_months': self.emergency_fund_months,
            'savings_rate': self.savings_rate,
            'max_drift': self.max_drift,
            'max_drift_symbol': self.max_drift_symbol,
            'drifts_by_symbol': self.drifts_by_symbol
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'FinanceSnapshot':
        return cls(
            date=data['date'],
            portfolio_value=data['portfolio_value'],
            cash_available=data['cash_available'],
            emergency_fund_months=data.get('emergency_fund_months', 0),
            savings_rate=data.get('savings_rate', 0),
            max_drift=data['max_drift'],
            max_drift_symbol=data['max_drift_symbol'],
            drifts_by_symbol=data.get('drifts_by_symbol', {})
        )


class FinanceHistoryStore:
    """Stores and retrieves historical finance snapshots."""
    
    def __init__(self, filepath: str = "data/finance_history.json"):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._history: List[FinanceSnapshot] = []
        self._load()
    
    def _load(self) -> None:
        """Load history from disk."""
        if self.filepath.exists():
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                self._history = [FinanceSnapshot.from_dict(d) for d in data]
                logger.debug(f"Loaded {len(self._history)} historical snapshots")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load history: {e}")
                self._history = []
    
    def _save(self) -> None:
        """Persist history to disk."""
        try:
            data = [s.to_dict() for s in self._history]
            with open(self.filepath, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save history: {e}")
    
    def add(self, snapshot: FinanceSnapshot) -> None:
        """Add a new snapshot to history."""
        self._history.append(snapshot)
        # Keep last 90 days
        self._history = self._history[-90:]
        self._save()
    
    def get_recent(self, periods: int = 10) -> List[FinanceSnapshot]:
        """Get most recent snapshots."""
        return self._history[-periods:] if self._history else []
    
    def get_trend_data(
        self,
        metric: str,
        threshold: float,
        is_increasing_bad: bool = True
    ) -> TrendData:
        """Build TrendData from historical values."""
        recent = self.get_recent(10)
        
        if not recent:
            return TrendData(
                current_value=0,
                threshold=threshold,
                is_increasing_bad=is_increasing_bad
            )
        
        values = []
        for snap in reversed(recent):  # Most recent first
            if metric == 'savings_rate':
                values.append(snap.savings_rate)
            elif metric == 'emergency_fund':
                values.append(snap.emergency_fund_months)
            elif metric == 'max_drift':
                values.append(abs(snap.max_drift))
            elif metric.startswith('drift_'):
                symbol = metric.replace('drift_', '')
                values.append(abs(snap.drifts_by_symbol.get(symbol, 0)))
        
        if not values:
            return TrendData(
                current_value=0,
                threshold=threshold,
                is_increasing_bad=is_increasing_bad
            )
        
        current = values[0]
        previous = values[1:] if len(values) > 1 else []
        
        # Calculate streak
        periods_below = 0
        periods_above = 0
        for v in values:
            if v < threshold:
                periods_below += 1
            elif v > threshold:
                periods_above += 1
        
        return TrendData(
            current_value=current,
            previous_values=previous,
            periods_below_threshold=periods_below,
            periods_above_threshold=periods_above,
            threshold=threshold,
            is_increasing_bad=is_increasing_bad
        )


@dataclass
class FinanceConfig:
    """Configuration for finance analysis."""
    # Targets
    target_savings_rate: float = 0.15
    target_emergency_months: float = 3.0
    drift_band: float = 0.07
    
    # Monthly figures
    monthly_income: float = 8000.0
    monthly_expenses: float = 3000.0
    
    # User priorities (1-5)
    priority_savings: int = 3
    priority_emergency: int = 4
    priority_drift: int = 3
    priority_taxes: int = 3
    priority_budget: int = 2
    
    # Data quality
    is_auto_imported: bool = True
    has_manual_entries: bool = False
    is_partial_period: bool = False
    
    @classmethod
    def from_investor_config(cls, config: Config) -> 'FinanceConfig':
        """Create from main config."""
        return cls(
            target_savings_rate=0.15,  # Default target
            target_emergency_months=3.0,
            drift_band=config.rebalance_rules.band_threshold,
            monthly_income=config.investor.annual_income / 12,
            monthly_expenses=config.investor.monthly_expenses
        )


class FinanceAnalyzer:
    """Main analyzer that generates scored recommendations."""
    
    def __init__(
        self,
        config: FinanceConfig,
        history_store: Optional[FinanceHistoryStore] = None
    ):
        self.config = config
        self.history = history_store or FinanceHistoryStore()
        self.confidence = calc_confidence(
            is_auto_imported=config.is_auto_imported,
            has_manual_entries=config.has_manual_entries,
            is_partial_period=config.is_partial_period
        )
    
    def analyze(
        self,
        summary: PortfolioSummary,
        holdings: List[Holding],
        analyses: List[HoldingAnalysis],
        current_savings_rate: Optional[float] = None,
        budget_variances: Optional[Dict[str, tuple]] = None
    ) -> List[FinanceRecommendation]:
        """
        Analyze financial state and generate recommendations.
        
        Args:
            summary: Portfolio summary
            holdings: List of holdings
            analyses: List of holding analyses
            current_savings_rate: Current savings rate (if known)
            budget_variances: Dict of {category: (actual, budget)}
        
        Returns:
            List of scored recommendations
        """
        recommendations = []
        
        # Create and save snapshot
        emergency_months = summary.cash_value / self.config.monthly_expenses if self.config.monthly_expenses > 0 else 0
        
        snapshot = FinanceSnapshot(
            date=date.today().isoformat(),
            portfolio_value=summary.total_portfolio_value,
            cash_available=summary.cash_value,
            emergency_fund_months=emergency_months,
            savings_rate=current_savings_rate or 0,
            max_drift=summary.max_drift,
            max_drift_symbol=summary.max_drift_symbol,
            drifts_by_symbol={a.symbol: a.drift for a in analyses if a.drift is not None}
        )
        self.history.add(snapshot)
        
        # 1. Savings Rate Analysis
        if current_savings_rate is not None:
            trend = self.history.get_trend_data(
                'savings_rate',
                self.config.target_savings_rate,
                is_increasing_bad=False  # Low savings is bad
            )
            trend.current_value = current_savings_rate
            
            rec = create_savings_rate_recommendation(
                current_rate=current_savings_rate,
                target_rate=self.config.target_savings_rate,
                trend=trend,
                monthly_income=self.config.monthly_income,
                user_priority=self.config.priority_savings,
                confidence=self.confidence
            )
            if rec:
                recommendations.append(rec)
        
        # 2. Emergency Fund Analysis
        trend = self.history.get_trend_data(
            'emergency_fund',
            self.config.target_emergency_months,
            is_increasing_bad=False
        )
        trend.current_value = emergency_months
        
        rec = create_emergency_fund_recommendation(
            current_months=emergency_months,
            target_months=self.config.target_emergency_months,
            monthly_expenses=self.config.monthly_expenses,
            trend=trend,
            user_priority=self.config.priority_emergency,
            confidence=self.confidence
        )
        if rec:
            recommendations.append(rec)
        
        # 3. Portfolio Drift Analysis (per holding)
        for holding, analysis in zip(holdings, analyses):
            if analysis.drift is None:
                continue
            
            trend = self.history.get_trend_data(
                f'drift_{holding.symbol}',
                self.config.drift_band,
                is_increasing_bad=True
            )
            trend.current_value = abs(analysis.drift)
            
            rec = create_portfolio_drift_recommendation(
                symbol=holding.symbol,
                drift=analysis.drift,
                band=self.config.drift_band,
                is_leveraged=holding.is_leveraged,
                trend=trend,
                user_priority=self.config.priority_drift,
                confidence=self.confidence
            )
            if rec:
                recommendations.append(rec)
        
        # 4. Budget Variance Analysis
        if budget_variances:
            for category, (actual, budget) in budget_variances.items():
                if actual <= budget:
                    continue
                
                trend = TrendData(
                    current_value=actual,
                    threshold=budget,
                    is_increasing_bad=True
                )
                
                rec = create_overspending_recommendation(
                    category=category,
                    actual=actual,
                    budget=budget,
                    trend=trend,
                    user_priority=self.config.priority_budget,
                    confidence=self.confidence
                )
                if rec:
                    recommendations.append(rec)
        
        # Deduplicate and sort by score
        recommendations = deduplicate_recommendations(recommendations)
        recommendations.sort(key=lambda r: r.final_score, reverse=True)
        
        logger.info(f"Generated {len(recommendations)} recommendations")
        for rec in recommendations[:5]:
            logger.info(f"  [{rec.action_level.value}] {rec.title}: {rec.final_score}")
        
        return recommendations
    
    def get_summary_lines(
        self,
        summary: PortfolioSummary,
        current_savings_rate: Optional[float] = None
    ) -> List[str]:
        """Generate summary lines for email."""
        from email_digest import build_top_summary
        
        emergency_months = summary.cash_value / self.config.monthly_expenses if self.config.monthly_expenses > 0 else 0
        
        return build_top_summary(
            total_portfolio=summary.total_portfolio_value,
            cash_available=summary.cash_value,
            savings_rate=current_savings_rate,
            target_savings_rate=self.config.target_savings_rate,
            emergency_months=emergency_months,
            target_emergency_months=self.config.target_emergency_months,
            max_drift=summary.max_drift,
            drift_band=self.config.drift_band
        )


def export_recommendations_csv(
    filepath: str,
    recommendations: List[FinanceRecommendation]
) -> bool:
    """Export scored recommendations to CSV."""
    import csv
    from email_digest import format_recommendations_for_csv
    
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        rows = format_recommendations_for_csv(recommendations)
        
        if not rows:
            logger.warning("No recommendations to export")
            return False
        
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        
        logger.info(f"Exported {len(rows)} recommendations to {filepath}")
        return True
        
    except IOError as e:
        logger.error(f"Failed to export recommendations: {e}")
        return False
