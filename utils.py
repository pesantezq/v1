"""
Utility functions for the portfolio automation system.
Includes configuration loading, logging setup, and common helpers.
"""

import json
import logging
import os
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from config.loader import load_runtime_config_dict
from dotenv import load_dotenv


def _cleanup_old_logs(log_dir: str = "logs", keep_days: int = 14) -> None:
    """Delete date-named log files older than keep_days from log_dir.

    Only files matching the pattern YYYY-MM-DD.log are considered.
    Silently skips files that cannot be deleted (e.g. permission errors).

    Args:
        log_dir:   Directory containing daily log files.
        keep_days: Number of days of logs to retain (default 14).
    """
    import re
    from datetime import timedelta

    log_path = Path(log_dir)
    if not log_path.exists():
        return

    cutoff = date.today() - timedelta(days=keep_days)
    pattern = re.compile(r'^\d{4}-\d{2}-\d{2}\.log$')

    for log_file in log_path.iterdir():
        if not log_file.is_file():
            continue
        if not pattern.match(log_file.name):
            continue
        try:
            file_date = date.fromisoformat(log_file.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                log_file.unlink()
            except OSError:
                pass  # Best-effort only


def setup_logging(debug: bool = False, log_dir: str = "logs") -> logging.Logger:
    """Configure and return the application logger.

    Writes to stdout and to logs/YYYY-MM-DD.log so every scheduled run
    leaves a permanent record.  Rotates by removing log files older than
    14 days at startup.
    """
    level = logging.DEBUG if debug else logging.INFO

    # Rotate out old log files before opening today's log
    _cleanup_old_logs(log_dir, keep_days=14)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / f"{date.today().isoformat()}.log"

    # Reconfigure stdout to UTF-8 so emoji/Unicode in log messages don't
    # crash on Windows where the default encoding is cp1252 (charmap).
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding='utf-8'),
        ]
    )

    logger = logging.getLogger('portfolio_automation')
    logger.setLevel(level)

    # Reduce noise from third-party libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    return logger


def load_env(env_path: Optional[str] = None) -> None:
    """Load environment variables from .env file."""
    if env_path:
        load_dotenv(env_path)
    else:
        # Try multiple locations
        possible_paths = [
            Path('.env'),
            Path(__file__).parent / '.env',
            Path.home() / '.portfolio_automation' / '.env'
        ]
        for path in possible_paths:
            if path.exists():
                load_dotenv(path)
                break


def get_env(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """Get environment variable with optional default and required flag."""
    value = os.environ.get(key, default)
    if required and value is None:
        raise EnvironmentError(f"Required environment variable '{key}' is not set")
    return value


@dataclass
class Holding:
    """Represents a single portfolio holding."""
    symbol: str
    shares: float
    target_weight: float
    asset_class: str
    is_leveraged: bool = False
    leverage_factor: float = 1.0
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    actual_weight: Optional[float] = None
    drift: Optional[float] = None
    
    @property
    def effective_exposure(self) -> float:
        """Calculate effective exposure accounting for leverage."""
        if self.market_value is None:
            return 0.0
        return self.market_value * self.leverage_factor


@dataclass
class Retirement401k:
    """Represents 401(k) retirement account data."""
    enabled: bool = False
    mode: str = "balance_only"
    balance: float = 0.0
    holdings_csv_path: str = ""
    include_in_net_worth: bool = True
    holdings: list = field(default_factory=list)


@dataclass
class InvestorProfile:
    """Represents investor profile information."""
    name: str
    age: int
    birthdate: str
    annual_income: float
    monthly_expenses: float
    investment_horizon_years: int
    risk_tolerance: str
    strategy: str


@dataclass
class RebalanceRules:
    """Represents rebalancing rules."""
    band_threshold: float
    use_cash_before_selling: bool
    direct_contributions_first: bool
    trim_leverage_before_core: bool
    avoid_taxable_sales: bool
    panic_sell_protection: bool


@dataclass
class Config:
    """Main configuration container."""
    investor: InvestorProfile
    holdings: list[Holding]
    cash_available: float
    target_cash_weight: float
    rebalance_rules: RebalanceRules
    retirement_401k: Retirement401k
    market_data: Dict[str, Any]
    email: Dict[str, Any]
    schedule: Dict[str, Any]
    output: Dict[str, Any]
    ml_advisor: Dict[str, Any] = field(default_factory=dict)
    growth_mode: Dict[str, Any] = field(default_factory=dict)
    monthly_contribution: float = 0.0
    has_regular_contributions: bool = True
    is_taxable_account: bool = True
    speculative_sleeve: Dict[str, Any] = field(default_factory=dict)
    scanner: Dict[str, Any] = field(default_factory=dict)
    api_limits: Dict[str, Any] = field(default_factory=dict)
    theme_engine: Dict[str, Any] = field(default_factory=lambda: {"enabled": False})
    watchlist_scanner: Dict[str, Any] = field(default_factory=lambda: {"enabled": False})
    holding_rationale: Dict[str, str] = field(default_factory=dict)
    opportunity_cost: Dict[str, Any] = field(default_factory=dict)
    market_universe: Dict[str, Any] = field(default_factory=lambda: {"enabled": False})
    universal_scanner_cfg: Dict[str, Any] = field(default_factory=dict)
    opportunity_ranker_cfg: Dict[str, Any] = field(default_factory=dict)
    promotion_engine_cfg: Dict[str, Any] = field(default_factory=dict)
    scraped_intel: Dict[str, Any] = field(default_factory=lambda: {"enabled": False})

    # ── Convenience properties for growth_mode sub-keys ──────────────────────
    @property
    def concentration_cap(self) -> float:
        """Max single-holding weight before a structural violation is raised."""
        return float(self.growth_mode.get('concentration_cap', 0.40))

    @property
    def leverage_cap(self) -> float:
        """Max total leveraged exposure fraction before a trim is required."""
        return float(self.growth_mode.get('leverage_cap', 0.15))

    @property
    def expected_returns(self) -> Dict[str, float]:
        """Asset-class → expected annual return map (config-driven)."""
        defaults = {
            'us_equity': 0.10,
            'us_equity_sector': 0.09,
            'international_equity': 0.08,
            'commodity': 0.04,
            'us_equity_leveraged': 0.14,
            'bonds': 0.04,
            'cash': 0.04,
        }
        defaults.update(self.growth_mode.get('expected_returns', {}))
        return defaults

    @property
    def drawdown_thresholds(self) -> Dict[str, float]:
        """Drawdown threshold map for regime classification."""
        defaults = {
            'modest_equity_tilt': 0.10,
            'aggressive_equity_tilt': 0.20,
            'deploy_all_cash': 0.30,
        }
        defaults.update(self.growth_mode.get('drawdown_thresholds', {}))
        return defaults

    @property
    def scanner_enabled(self) -> bool:
        """True when the S&P 500 candidate scanner is active."""
        return bool(self.scanner.get('enabled', False))

    @property
    def sleeve_enabled(self) -> bool:
        """True when the speculative sleeve allocator is active."""
        return bool(self.speculative_sleeve.get('enabled', False))

    @property
    def fmp_daily_calls_budget(self) -> int:
        """Max FMP API calls allowed per calendar day."""
        return int(self.api_limits.get('fmp_daily_calls_budget', 230))

    @property
    def theme_engine_enabled(self) -> bool:
        """True when the theme engine is active."""
        return bool(self.theme_engine.get('enabled', False))

    @property
    def market_universe_enabled(self) -> bool:
        """True when the broad market coverage scanner is active."""
        return bool(self.market_universe.get('enabled', False))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Config':
        """Create Config from dictionary."""
        investor = InvestorProfile(**data['investor'])
        
        holdings = [
            Holding(
                symbol=h['symbol'],
                shares=float(h['shares']),
                target_weight=float(h['target_weight']),
                asset_class=h['asset_class'],
                is_leveraged=h.get('is_leveraged', False),
                leverage_factor=float(h.get('leverage_factor', 1.0))
            )
            for h in data['portfolio']['holdings']
        ]
        
        rebalance_rules = RebalanceRules(**data['rebalance_rules'])
        
        retirement_data = data.get('retirement_401k', {})
        retirement_401k = Retirement401k(
            enabled=retirement_data.get('enabled', False),
            mode=retirement_data.get('mode', 'balance_only'),
            balance=float(retirement_data.get('balance', 0)),
            holdings_csv_path=retirement_data.get('holdings_csv_path', ''),
            include_in_net_worth=retirement_data.get('include_in_net_worth', True)
        )
        
        return cls(
            investor=investor,
            holdings=holdings,
            cash_available=float(data['portfolio'].get('cash_available', 0)),
            target_cash_weight=float(data['portfolio'].get('target_cash_weight', 0.05)),
            rebalance_rules=rebalance_rules,
            retirement_401k=retirement_401k,
            market_data=data.get('market_data', {}),
            email=data.get('email', {}),
            schedule=data.get('schedule', {}),
            output=data.get('output', {}),
            ml_advisor=data.get('ml_advisor', {}),
            growth_mode=data.get('growth_mode', {}),
            monthly_contribution=float(data['portfolio'].get('monthly_contribution', 0)),
            has_regular_contributions=data['portfolio'].get('has_regular_contributions', True),
            is_taxable_account=data['portfolio'].get('is_taxable_account', True),
            speculative_sleeve=data.get('speculative_sleeve', {}),
            scanner=data.get('scanner', {}),
            api_limits=data.get('api_limits', {}),
            theme_engine=data.get('theme_engine', {"enabled": False}),
            watchlist_scanner=data.get('watchlist_scanner', {"enabled": False}),
            holding_rationale=data.get('holding_rationale', {}),
            opportunity_cost=data.get('opportunity_cost', {}),
            market_universe=data.get('market_universe', {"enabled": False}),
            universal_scanner_cfg=data.get('universal_scanner', {}),
            opportunity_ranker_cfg=data.get('opportunity_ranker', {}),
            promotion_engine_cfg=data.get('promotion_engine', {}),
            scraped_intel=data.get('scraped_intel', {"enabled": False}),
        )


def load_config_dict(
    config_path: Optional[str] = None,
    *,
    profile: Optional[str] = None,
    record_history: bool = True,
) -> Dict[str, Any]:
    """Load the resolved runtime config as a plain dictionary."""
    if config_path is None:
        config_path = get_env('CONFIG_PATH', 'config.json')
    return load_runtime_config_dict(
        config_path,
        profile=profile,
        record_history=record_history,
    )


def load_config(
    config_path: Optional[str] = None,
    *,
    profile: Optional[str] = None,
    record_history: bool = True,
) -> Config:
    """Load legacy or structured config and return the runtime Config object."""
    data = load_config_dict(
        config_path,
        profile=profile,
        record_history=record_history,
    )
    return Config.from_dict(data)


def round_currency(value: float, decimals: int = 2) -> float:
    """Round currency value to specified decimal places."""
    d = Decimal(str(value))
    return float(d.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP))


def round_percent(value: float, decimals: int = 2) -> float:
    """Round percentage value to specified decimal places."""
    return round_currency(value * 100, decimals) / 100


def format_currency(value: float) -> str:
    """Format value as currency string."""
    return f"${value:,.2f}"


def format_percent(value: float) -> str:
    """Format value as percentage string."""
    return f"{value * 100:.2f}%"


def is_annual_review_date(review_date_str: str) -> bool:
    """Check if today matches the annual review date (MM-DD format)."""
    try:
        today = date.today()
        month, day = map(int, review_date_str.split('-'))
        return today.month == month and today.day == day
    except (ValueError, AttributeError):
        return False


def is_weekly_summary_day(day_name: str) -> bool:
    """Check if today matches the weekly summary day."""
    day_mapping = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2,
        'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6
    }
    target_day = day_mapping.get(day_name.lower())
    if target_day is None:
        return False
    return date.today().weekday() == target_day


def get_timestamp() -> str:
    """Get current timestamp string."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def get_date_str() -> str:
    """Get current date string."""
    return date.today().strftime('%Y-%m-%d')


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safely divide two numbers, returning default if denominator is zero."""
    if denominator == 0:
        return default
    return numerator / denominator


def validate_config(config: Config) -> list[str]:
    """Validate configuration and return list of warnings/errors."""
    issues = []
    
    # Validate holdings weights sum to ~1.0 (including cash target)
    total_weight = sum(h.target_weight for h in config.holdings) + config.target_cash_weight
    if abs(total_weight - 1.0) > 0.001:
        issues.append(f"Target weights sum to {total_weight:.4f}, expected 1.0")
    
    # Validate rebalance band
    if not 0 < config.rebalance_rules.band_threshold < 1:
        issues.append(f"Invalid rebalance band: {config.rebalance_rules.band_threshold}")
    
    # Validate investor age
    if config.investor.age < 0 or config.investor.age > 120:
        issues.append(f"Invalid investor age: {config.investor.age}")
    
    # Validate symbols are not empty
    for holding in config.holdings:
        if not holding.symbol or not holding.symbol.strip():
            issues.append("Found holding with empty symbol")
    
    return issues
