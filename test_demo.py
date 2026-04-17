#!/usr/bin/env python3
"""
Test/Demo script for Portfolio Automation System.
Uses mock price data to demonstrate full workflow without API calls.

Usage:
    python test_demo.py
"""

import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from utils import (
    load_config, get_timestamp, Holding, Retirement401k
)
from portfolio import (
    generate_portfolio_summary, calculate_allocations,
    analyze_holdings, format_summary_text, format_holdings_table
)
from recommendations import (
    generate_recommendations, format_recommendations_text
)
from file_output import (
    write_csv_snapshot, create_excel_workbook
)


# Mock prices for demonstration
MOCK_PRICES = {
    'QQQ': 485.50,
    'VFH': 112.25,
    'VXUS': 62.80,
    'GLD': 238.90,
    'QLD': 98.75
}


def inject_mock_prices(holdings: list[Holding]) -> list[Holding]:
    """Inject mock prices into holdings for testing."""
    for holding in holdings:
        if holding.symbol in MOCK_PRICES:
            holding.current_price = MOCK_PRICES[holding.symbol]
            holding.market_value = holding.current_price * holding.shares
    return holdings


def run_demo():
    """Run full demo with mock data."""
    print("=" * 60)
    print("PORTFOLIO AUTOMATION SYSTEM - TEST/DEMO")
    print("=" * 60)
    print("\nUsing mock prices - no API calls made")
    print()
    
    # Load config
    config = load_config('config.json')
    
    # Set up test holdings with shares
    test_holdings = [
        Holding(symbol='QQQ', shares=10, target_weight=0.45, 
                asset_class='us_equity', is_leveraged=False, leverage_factor=1),
        Holding(symbol='VFH', shares=15, target_weight=0.15,
                asset_class='us_equity_sector', is_leveraged=False, leverage_factor=1),
        Holding(symbol='VXUS', shares=20, target_weight=0.10,
                asset_class='international_equity', is_leveraged=False, leverage_factor=1),
        Holding(symbol='GLD', shares=8, target_weight=0.20,
                asset_class='commodity', is_leveraged=False, leverage_factor=1),
        Holding(symbol='QLD', shares=5, target_weight=0.05,
                asset_class='us_equity_leveraged', is_leveraged=True, leverage_factor=2),
    ]
    
    # Inject mock prices
    test_holdings = inject_mock_prices(test_holdings)
    
    # Set cash available
    test_cash = 500.00
    
    # Set up retirement account
    test_retirement = Retirement401k(
        enabled=True,
        mode='balance_only',
        balance=25000.00,
        include_in_net_worth=True
    )
    
    timestamp = get_timestamp()
    
    # Generate summary
    print("Generating portfolio summary...")
    summary = generate_portfolio_summary(
        holdings=test_holdings,
        cash_available=test_cash,
        target_cash_weight=config.target_cash_weight,
        retirement_401k=test_retirement,
        band_threshold=config.rebalance_rules.band_threshold,
        timestamp=timestamp
    )
    
    # Calculate allocations
    test_holdings, cash_weight, cash_drift = calculate_allocations(
        test_holdings,
        summary.total_portfolio_value,
        test_cash,
        config.target_cash_weight
    )
    
    # Analyze holdings
    analyses = analyze_holdings(
        test_holdings,
        summary.total_portfolio_value,
        config.rebalance_rules.band_threshold
    )
    
    # Generate recommendations
    recommendations = generate_recommendations(
        holdings=test_holdings,
        analyses=analyses,
        summary=summary,
        rules=config.rebalance_rules,
        cash_available=test_cash,
        cash_weight=cash_weight,
        target_cash_weight=config.target_cash_weight,
        context_notes=["Demo run with mock data"]
    )
    
    # Print results
    print("\n" + format_summary_text(summary))
    print("\n" + format_holdings_table(analyses))
    print("\n" + format_recommendations_text(recommendations))
    
    # Write output files
    print("\nWriting output files...")
    
    Path('output').mkdir(exist_ok=True)
    
    csv_ok = write_csv_snapshot(
        filepath='output/demo_snapshot.csv',
        holdings=test_holdings,
        analyses=analyses,
        summary=summary,
        cash_available=test_cash
    )
    
    excel_ok = create_excel_workbook(
        filepath='output/demo_tracker.xlsx',
        holdings=test_holdings,
        analyses=analyses,
        summary=summary,
        recommendations=recommendations,
        cash_available=test_cash,
        append_history=True
    )
    
    print(f"\nCSV written: {'✓' if csv_ok else '✗'} output/demo_snapshot.csv")
    print(f"Excel written: {'✓' if excel_ok else '✗'} output/demo_tracker.xlsx")
    
    print("\n" + "=" * 60)
    print("DEMO COMPLETE")
    print("=" * 60)
    
    return summary, recommendations


if __name__ == '__main__':
    run_demo()
