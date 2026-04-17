"""
401(k) retirement account integration module.
Supports balance-only mode and holdings CSV import.
"""

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from utils import Retirement401k, round_currency, format_currency


logger = logging.getLogger('portfolio_automation.retirement')


@dataclass
class RetirementHolding:
    """A single holding in a 401(k) account."""
    symbol: str
    name: str
    shares: float
    price: float
    market_value: float
    percentage: float


@dataclass
class RetirementSummary:
    """Summary of 401(k) account."""
    total_balance: float
    holdings: list[RetirementHolding]
    mode: str
    last_updated: Optional[str] = None


def load_401k_holdings_csv(csv_path: str) -> tuple[list[RetirementHolding], float]:
    """
    Load 401(k) holdings from CSV file.
    Expected columns: symbol, name, shares, price, market_value
    Returns (holdings_list, total_balance).
    """
    path = Path(csv_path)
    
    if not path.exists():
        logger.error(f"401(k) holdings CSV not found: {csv_path}")
        return [], 0.0
    
    holdings = []
    total_balance = 0.0
    
    try:
        with open(path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                try:
                    # Parse required fields
                    symbol = row.get('symbol', row.get('Symbol', '')).strip()
                    name = row.get('name', row.get('Name', symbol)).strip()
                    
                    # Parse numeric fields with flexibility
                    shares_str = row.get('shares', row.get('Shares', '0'))
                    shares = float(shares_str.replace(',', '').replace('$', ''))
                    
                    price_str = row.get('price', row.get('Price', '0'))
                    price = float(price_str.replace(',', '').replace('$', ''))
                    
                    # Market value can be calculated or provided
                    value_str = row.get('market_value', row.get('Market Value', 
                                row.get('value', row.get('Value', ''))))
                    
                    if value_str:
                        market_value = float(value_str.replace(',', '').replace('$', ''))
                    else:
                        market_value = shares * price
                    
                    market_value = round_currency(market_value)
                    total_balance += market_value
                    
                    # Calculate percentage (will be recalculated later)
                    percentage = 0.0
                    
                    holding = RetirementHolding(
                        symbol=symbol,
                        name=name,
                        shares=shares,
                        price=price,
                        market_value=market_value,
                        percentage=percentage
                    )
                    holdings.append(holding)
                    
                except (ValueError, KeyError) as e:
                    logger.warning(f"Error parsing 401(k) holding row: {e}")
                    continue
        
        # Calculate percentages
        if total_balance > 0:
            for holding in holdings:
                holding.percentage = (holding.market_value / total_balance) * 100
        
        logger.info(f"Loaded {len(holdings)} 401(k) holdings, "
                   f"total: {format_currency(total_balance)}")
        
    except IOError as e:
        logger.error(f"Error reading 401(k) CSV: {e}")
        return [], 0.0
    
    return holdings, round_currency(total_balance)


def load_retirement_data(config: Retirement401k) -> RetirementSummary:
    """
    Load 401(k) data based on configuration.
    Supports balance-only and holdings CSV modes.
    """
    if not config.enabled:
        logger.debug("401(k) integration disabled")
        return RetirementSummary(
            total_balance=0.0,
            holdings=[],
            mode="disabled"
        )
    
    if config.mode == "balance_only":
        logger.info(f"401(k) balance-only mode: {format_currency(config.balance)}")
        return RetirementSummary(
            total_balance=config.balance,
            holdings=[],
            mode="balance_only"
        )
    
    elif config.mode == "holdings_csv":
        if not config.holdings_csv_path:
            logger.error("Holdings CSV mode enabled but no path specified")
            return RetirementSummary(
                total_balance=config.balance,
                holdings=[],
                mode="error"
            )
        
        holdings, total = load_401k_holdings_csv(config.holdings_csv_path)
        
        if not holdings:
            # Fall back to manual balance if CSV failed
            logger.warning("CSV load failed, using manual balance")
            return RetirementSummary(
                total_balance=config.balance,
                holdings=[],
                mode="balance_only_fallback"
            )
        
        return RetirementSummary(
            total_balance=total,
            holdings=holdings,
            mode="holdings_csv"
        )
    
    else:
        logger.warning(f"Unknown 401(k) mode: {config.mode}")
        return RetirementSummary(
            total_balance=config.balance,
            holdings=[],
            mode="unknown"
        )


def format_retirement_summary(summary: RetirementSummary) -> str:
    """Format 401(k) summary as readable text."""
    lines = [
        "=" * 50,
        "401(k) RETIREMENT ACCOUNT",
        "=" * 50,
        f"Mode: {summary.mode}",
        f"Total Balance: {format_currency(summary.total_balance)}",
    ]
    
    if summary.holdings:
        lines.append("")
        lines.append("Holdings:")
        lines.append("-" * 40)
        
        header = f"{'Symbol':<10} {'Name':<25} {'Value':>12} {'%':>6}"
        lines.append(header)
        lines.append("-" * 55)
        
        for h in sorted(summary.holdings, key=lambda x: -x.market_value):
            name_display = h.name[:23] + ".." if len(h.name) > 25 else h.name
            row = f"{h.symbol:<10} {name_display:<25} {format_currency(h.market_value):>12} {h.percentage:>5.1f}%"
            lines.append(row)
    
    lines.append("")
    lines.append("NOTE: 401(k) holdings are for tracking only.")
    lines.append("      No trading recommendations generated.")
    lines.append("=" * 50)
    
    return "\n".join(lines)


def create_sample_401k_csv(output_path: str) -> None:
    """Create a sample 401(k) holdings CSV template."""
    sample_data = [
        {
            'symbol': 'VFIAX',
            'name': 'Vanguard 500 Index Admiral',
            'shares': '100',
            'price': '450.00',
            'market_value': '45000.00'
        },
        {
            'symbol': 'VBTLX',
            'name': 'Vanguard Total Bond Market',
            'shares': '200',
            'price': '10.50',
            'market_value': '2100.00'
        },
        {
            'symbol': 'VTIAX',
            'name': 'Vanguard Total Intl Stock',
            'shares': '50',
            'price': '32.00',
            'market_value': '1600.00'
        }
    ]
    
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(path, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ['symbol', 'name', 'shares', 'price', 'market_value']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sample_data)
    
    logger.info(f"Created sample 401(k) CSV: {output_path}")
