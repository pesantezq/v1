"""
ML History Tracking Module

Collects and stores historical recommendation data with outcomes
for supervised learning. Tracks persistence, resolution, and effectiveness.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from enum import Enum

logger = logging.getLogger('portfolio_automation.ml_history')


class MetricType(Enum):
    DRIFT = "Drift"
    ALLOCATION = "Allocation"
    CASH = "Cash"
    SAVINGS = "Savings"
    BUDGET = "Budget"
    EMERGENCY_FUND = "EmergencyFund"


class TrendDirection(Enum):
    IMPROVING = "Improving"
    FLAT = "Flat"
    WORSENING = "Worsening"


class ActionTaken(Enum):
    NONE = "None"
    BUY = "Buy"
    SELL = "Sell"
    CONTRIBUTION_REDIRECT = "ContributionRedirect"
    TRIM = "Trim"
    EXTERNAL = "External"  # User took action outside system


class ResolutionType(Enum):
    PENDING = "Pending"
    NATURAL = "Natural"           # Resolved without intervention
    ACTION_RESOLVED = "ActionResolved"  # Resolved via recommended action
    EXTERNAL_RESOLVED = "ExternalResolved"  # User action
    SUPERSEDED = "Superseded"     # New recommendation replaced it
    EXPIRED = "Expired"           # Too old, no longer relevant


@dataclass
class RecommendationRecord:
    """A single historical recommendation record with outcome tracking."""
    
    # Identification
    record_id: str
    rec_key: str  # e.g., DRIFT_VFH
    symbol: str
    
    # Timestamp
    created_date: str
    resolved_date: Optional[str] = None
    
    # Metric State at Creation
    metric_type: str = ""
    drift_percent: float = 0.0
    absolute_deviation: float = 0.0
    is_underweight: bool = False
    impact_area: str = ""
    asset_class: str = ""
    
    # Temporal Features at Creation
    streak_length: int = 0
    trend_direction: str = "Flat"
    rate_of_change: float = 0.0
    time_since_last_action: int = 0
    
    # Capital Context
    available_cash: float = 0.0
    cash_excess: float = 0.0
    contribution_rate: float = 0.0
    is_taxable: bool = True
    
    # Market Context (optional)
    market_volatility: str = "Medium"
    macro_regime: str = "Neutral"
    
    # Original System Output
    original_action: str = ""
    adjustment_mode: str = ""
    original_score: int = 0
    action_level: str = ""
    
    # Outcome Labels (Y values)
    # Y1: Persistence - did condition persist >= N periods?
    persisted: Optional[bool] = None
    persistence_periods: int = 0
    
    # Y2: Action Effectiveness - did action reduce deviation faster than baseline?
    action_taken: str = "None"
    action_effective: Optional[bool] = None
    
    # Y3: Resolution Time - periods until resolution
    resolution_periods: Optional[int] = None
    resolution_type: str = "Pending"
    
    # Y4: Alert Fatigue - repeated alert resolved without intervention
    alert_count: int = 1
    resolved_without_action: Optional[bool] = None
    
    # Final state
    final_drift_percent: Optional[float] = None
    final_deviation: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RecommendationRecord':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    @property
    def is_resolved(self) -> bool:
        return self.resolution_type != "Pending"
    
    @property
    def days_open(self) -> int:
        """Calculate days since creation."""
        created = datetime.strptime(self.created_date, "%Y-%m-%d").date()
        if self.resolved_date:
            resolved = datetime.strptime(self.resolved_date, "%Y-%m-%d").date()
        else:
            resolved = date.today()
        return (resolved - created).days


class MLHistoryStore:
    """Stores and manages historical recommendation records."""
    
    def __init__(self, filepath: str = "data/ml_history.json"):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._records: Dict[str, RecommendationRecord] = {}
        self._load()
    
    def _load(self) -> None:
        """Load history from disk."""
        if self.filepath.exists():
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                self._records = {
                    k: RecommendationRecord.from_dict(v) 
                    for k, v in data.items()
                }
                logger.debug(f"Loaded {len(self._records)} historical records")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load ML history: {e}")
                self._records = {}
    
    def _save(self) -> None:
        """Persist history to disk."""
        try:
            data = {k: v.to_dict() for k, v in self._records.items()}
            with open(self.filepath, 'w') as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save ML history: {e}")
    
    def add_record(self, record: RecommendationRecord) -> None:
        """Add a new recommendation record."""
        self._records[record.record_id] = record
        self._save()
        logger.debug(f"Added ML record: {record.record_id}")
    
    def update_record(self, record_id: str, **updates) -> bool:
        """Update an existing record."""
        if record_id not in self._records:
            return False
        
        record = self._records[record_id]
        for key, value in updates.items():
            if hasattr(record, key):
                setattr(record, key, value)
        
        self._save()
        return True
    
    def get_record(self, record_id: str) -> Optional[RecommendationRecord]:
        """Get a specific record."""
        return self._records.get(record_id)
    
    def get_pending_records(self) -> List[RecommendationRecord]:
        """Get all unresolved records."""
        return [r for r in self._records.values() if not r.is_resolved]
    
    def get_records_by_key(self, rec_key: str) -> List[RecommendationRecord]:
        """Get all records for a specific recommendation key."""
        return [r for r in self._records.values() if r.rec_key == rec_key]
    
    def get_resolved_records(self, min_records: int = 0) -> List[RecommendationRecord]:
        """Get all resolved records for training."""
        resolved = [r for r in self._records.values() if r.is_resolved]
        if len(resolved) < min_records:
            logger.warning(f"Only {len(resolved)} resolved records (need {min_records})")
        return resolved
    
    def get_records_by_symbol(self, symbol: str) -> List[RecommendationRecord]:
        """Get all records for a symbol."""
        return [r for r in self._records.values() if r.symbol == symbol]
    
    def get_similar_records(
        self,
        metric_type: str,
        asset_class: str,
        drift_range: tuple = None,
        resolved_only: bool = True
    ) -> List[RecommendationRecord]:
        """Find similar historical records for pattern matching."""
        results = []
        for r in self._records.values():
            if resolved_only and not r.is_resolved:
                continue
            if r.metric_type != metric_type:
                continue
            if r.asset_class != asset_class:
                continue
            if drift_range:
                low, high = drift_range
                if not (low <= abs(r.drift_percent) <= high):
                    continue
            results.append(r)
        return results
    
    def calculate_baseline_stats(self, metric_type: str = None) -> Dict[str, Any]:
        """Calculate baseline statistics for resolved records."""
        resolved = self.get_resolved_records()
        
        if metric_type:
            resolved = [r for r in resolved if r.metric_type == metric_type]
        
        if not resolved:
            return {
                'count': 0,
                'avg_resolution_periods': None,
                'natural_resolution_rate': None,
                'action_effectiveness_rate': None
            }
        
        resolution_times = [r.resolution_periods for r in resolved if r.resolution_periods]
        natural_resolutions = sum(1 for r in resolved if r.resolution_type == "Natural")
        action_effective = [r for r in resolved if r.action_effective is not None]
        
        return {
            'count': len(resolved),
            'avg_resolution_periods': sum(resolution_times) / len(resolution_times) if resolution_times else None,
            'natural_resolution_rate': natural_resolutions / len(resolved) if resolved else None,
            'action_effectiveness_rate': sum(1 for r in action_effective if r.action_effective) / len(action_effective) if action_effective else None
        }
    
    def export_training_data(self, filepath: str) -> int:
        """Export resolved records as CSV for ML training."""
        import csv
        
        resolved = self.get_resolved_records()
        if not resolved:
            logger.warning("No resolved records to export")
            return 0
        
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=resolved[0].to_dict().keys())
            writer.writeheader()
            for record in resolved:
                writer.writerow(record.to_dict())
        
        logger.info(f"Exported {len(resolved)} training records to {filepath}")
        return len(resolved)


def create_record_from_adjustment(
    adjustment,  # PortfolioAdjustment
    cash_analysis,  # CashAnalysis
    streak_length: int = 0,
    trend_direction: str = "Flat",
    market_volatility: str = "Medium"
) -> RecommendationRecord:
    """Create a new ML record from a portfolio adjustment."""
    
    record_id = f"{adjustment.rec_key}_{date.today().isoformat()}_{datetime.now().strftime('%H%M%S')}"
    
    return RecommendationRecord(
        record_id=record_id,
        rec_key=adjustment.rec_key,
        symbol=adjustment.symbol,
        created_date=date.today().isoformat(),
        
        # Metric state
        metric_type="Drift",
        drift_percent=adjustment.drift if adjustment.drift else 0.0,
        absolute_deviation=abs(adjustment.drift) - adjustment.band if adjustment.drift and adjustment.band else 0.0,
        is_underweight=adjustment.drift < 0 if adjustment.drift else False,
        impact_area="Portfolio Risk",
        asset_class="Leveraged" if adjustment.is_leveraged else "Equity",
        
        # Temporal
        streak_length=streak_length,
        trend_direction=trend_direction,
        
        # Capital
        available_cash=cash_analysis.available_cash if cash_analysis else 0.0,
        cash_excess=cash_analysis.cash_excess if cash_analysis else 0.0,
        contribution_rate=cash_analysis.monthly_contribution if cash_analysis else 0.0,
        
        # Market context
        market_volatility=market_volatility,
        
        # System output
        original_action=adjustment.adjustment_mode.value if adjustment.adjustment_mode else "",
        adjustment_mode=adjustment.adjustment_mode.value if adjustment.adjustment_mode else "",
        original_score=adjustment.final_score,
        action_level=adjustment.action_level.value if adjustment.action_level else ""
    )


def update_record_resolution(
    store: MLHistoryStore,
    rec_key: str,
    current_drift: float,
    band: float,
    action_taken: str = "None"
) -> None:
    """Check and update resolution status for pending records."""
    
    pending = [r for r in store.get_records_by_key(rec_key) if not r.is_resolved]
    
    for record in pending:
        days_open = record.days_open
        record.persistence_periods = days_open // 7  # Weekly periods
        
        # Check if resolved (drift back within band)
        is_resolved = abs(current_drift) <= band
        
        if is_resolved:
            record.resolved_date = date.today().isoformat()
            record.resolution_periods = record.persistence_periods
            record.final_drift_percent = current_drift
            record.final_deviation = abs(current_drift) - band
            
            # Determine resolution type
            if action_taken != "None":
                record.action_taken = action_taken
                record.resolution_type = "ActionResolved"
                record.action_effective = True  # Simplified - could compare to baseline
                record.resolved_without_action = False
            else:
                record.resolution_type = "Natural"
                record.resolved_without_action = True
                record.action_effective = False
            
            # Persistence label
            record.persisted = record.persistence_periods >= 2
            
            store.update_record(record.record_id, **record.to_dict())
            logger.info(f"Resolved record {record.record_id}: {record.resolution_type}")
        
        else:
            # Still pending - update streak
            record.persisted = record.persistence_periods >= 2
            store.update_record(record.record_id, persistence_periods=record.persistence_periods)
