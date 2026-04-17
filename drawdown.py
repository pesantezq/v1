"""
Drawdown Tracking Module

Tracks portfolio peak value (rolling 12-month high and all-time high),
computes current drawdown percentage, and provides behavioral gating
for the Aggressive Wealth Growth Mode.

Regime definitions (based on 12-month rolling high):
  normal          : drawdown < 10%
  modest_dip      : 10% <= drawdown < 20%  -> tilt contributions to equity
  significant_dip : 20% <= drawdown < 30%  -> aggressive equity tilt
  severe_dip      : drawdown >= 30%        -> deploy all available cash
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger('portfolio_automation.drawdown')

# Default drawdown thresholds (fractions, e.g. 0.10 = 10%)
DRAWDOWN_THRESHOLDS_DEFAULT: Dict[str, float] = {
    'modest_equity_tilt': 0.10,
    'aggressive_equity_tilt': 0.20,
    'deploy_all_cash': 0.30,
}


@dataclass
class DrawdownState:
    """Persistent state tracking portfolio peak and drawdown."""
    all_time_high: float = 0.0
    rolling_12m_high: float = 0.0
    rolling_12m_high_date: str = ""
    last_update_date: str = ""
    current_value: float = 0.0

    @property
    def drawdown_from_ath(self) -> float:
        """Drawdown percentage from all-time high (0.0 = no drawdown)."""
        if self.all_time_high <= 0:
            return 0.0
        return max(0.0, (self.all_time_high - self.current_value) / self.all_time_high)

    @property
    def drawdown_from_12m_high(self) -> float:
        """Drawdown percentage from 12-month rolling high (0.0 = no drawdown)."""
        if self.rolling_12m_high <= 0:
            return 0.0
        return max(0.0, (self.rolling_12m_high - self.current_value) / self.rolling_12m_high)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DrawdownState':
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class DrawdownTracker:
    """
    Tracks and persists portfolio drawdown state across runs.

    State is saved to a JSON file so peak tracking is maintained
    between executions.
    """

    def __init__(self, filepath: str = "data/drawdown_state.json"):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _load(self) -> DrawdownState:
        if self.filepath.exists():
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                return DrawdownState.from_dict(data)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Failed to load drawdown state, starting fresh: {e}")
        return DrawdownState()

    def _save(self) -> None:
        try:
            with open(self.filepath, 'w') as f:
                json.dump(self._state.to_dict(), f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save drawdown state: {e}")

    def update(self, current_value: float) -> DrawdownState:
        """
        Update peak tracking with the current portfolio value.

        Call once per run after fetching live prices. Automatically
        resets the 12-month rolling high when the window expires.
        """
        today = date.today().isoformat()
        self._state.current_value = current_value
        self._state.last_update_date = today

        # All-time high
        if current_value > self._state.all_time_high:
            self._state.all_time_high = current_value
            logger.info(f"New all-time high: ${current_value:,.2f}")

        # 12-month rolling high
        if self._state.rolling_12m_high_date:
            high_date = date.fromisoformat(self._state.rolling_12m_high_date)
            days_since_high = (date.today() - high_date).days
            if days_since_high > 365:
                # Rolling window expired; reset to current value
                self._state.rolling_12m_high = current_value
                self._state.rolling_12m_high_date = today
                logger.info("12-month rolling high reset (window expired)")
            elif current_value >= self._state.rolling_12m_high:
                self._state.rolling_12m_high = current_value
                self._state.rolling_12m_high_date = today
        else:
            # First run — initialise
            self._state.rolling_12m_high = current_value
            self._state.rolling_12m_high_date = today

        self._save()
        return self._state

    @property
    def state(self) -> DrawdownState:
        return self._state

    def get_regime(
        self,
        thresholds: Optional[Dict[str, float]] = None
    ) -> str:
        """
        Return the current drawdown regime label.

        Returns one of: 'normal', 'modest_dip', 'significant_dip', 'severe_dip'
        Regime is based on the 12-month rolling high drawdown.
        """
        t = thresholds or DRAWDOWN_THRESHOLDS_DEFAULT
        dd = self._state.drawdown_from_12m_high

        if dd < t.get('modest_equity_tilt', 0.10):
            return 'normal'
        elif dd < t.get('aggressive_equity_tilt', 0.20):
            return 'modest_dip'
        elif dd < t.get('deploy_all_cash', 0.30):
            return 'significant_dip'
        else:
            return 'severe_dip'

    def should_suppress_sells(self, leverage_violation: bool = False) -> bool:
        """
        Return True if non-structural sell recommendations should be suppressed.

        Anti-panic gating rule: drawdown > 20% suppresses all sell
        recommendations EXCEPT leverage cap violations.
        """
        dd = self._state.drawdown_from_12m_high
        if dd >= 0.20 and not leverage_violation:
            return True
        return False

    def format_summary(self, thresholds: Optional[Dict[str, float]] = None) -> str:
        """Return a one-line drawdown status string for console output."""
        ath_dd = self._state.drawdown_from_ath * 100
        rolling_dd = self._state.drawdown_from_12m_high * 100
        regime = self.get_regime(thresholds)
        ath = self._state.all_time_high
        return (
            f"Drawdown: {rolling_dd:.1f}% from 12m-high | "
            f"{ath_dd:.1f}% from ATH ${ath:,.0f} | "
            f"Regime: {regime}"
        )
