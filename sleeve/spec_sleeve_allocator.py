"""
Speculative Sleeve Allocator

Converts a ranked list of S&P 500 candidates into a concise monthly buy
plan constrained by the speculative-sleeve guardrails.

Rules enforced:
  sleeve_total_max         — all spec positions combined ≤ X % of portfolio
  max_per_stock            — no single position > Y % of portfolio
  max_new_positions        — at most N *new* symbols added per month
  no_sells                 — sleeve is buy-only (never recommends trims)
  no_rebuy_core_holdings   — symbols held in any core asset class are skipped

Sleeve holdings are identified by  asset_class == 'speculative'  in config.
Core holdings (any other asset_class) are blocked from spec-sleeve buys.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

logger = logging.getLogger('portfolio_automation.sleeve')


@dataclass
class SleeveRecommendation:
    """A single buy recommendation for the speculative sleeve."""

    symbol: str
    score: float
    sector: str
    max_add_dollars: float
    is_new_position: bool
    current_position_dollars: float   # 0.0 if brand-new position
    reason: str

    def to_dict(self) -> Dict:
        return {
            'Symbol': self.symbol,
            'Score': round(self.score, 1),
            'Sector': self.sector,
            'MaxAddDollars': round(self.max_add_dollars, 2),
            'IsNewPosition': self.is_new_position,
            'CurrentPositionDollars': round(self.current_position_dollars, 2),
            'Reason': self.reason,
        }


class SpecSleeveAllocator:
    """
    Allocates speculative-sleeve buy capacity across ranked candidates.

    Args:
        sleeve_total_max:           Max total spec-sleeve weight (0–1).
        max_per_stock:              Max weight for a single spec position (0–1).
        max_new_positions_per_month: Max new symbols to add in one run.
        min_position_dollars:       Minimum meaningful position size in $.
    """

    def __init__(
        self,
        sleeve_total_max: float = 0.10,
        max_per_stock: float = 0.05,
        max_new_positions_per_month: int = 1,
        min_position_dollars: float = 200.0,
    ) -> None:
        self.sleeve_total_max = sleeve_total_max
        self.max_per_stock = max_per_stock
        self.max_new_positions = max_new_positions_per_month
        self.min_position_dollars = min_position_dollars

    def allocate(
        self,
        candidates: List[Dict],
        holdings,                    # List[Holding] with market_value set
        total_portfolio: float,
        available_cash: float,
        drawdown_regime: str = 'normal',
    ) -> List[SleeveRecommendation]:
        """
        Produce buy recommendations for the speculative sleeve.

        Args:
            candidates:      Scored candidates sorted by score descending.
            holdings:        All current portfolio holdings (Holding objects).
            total_portfolio: Total portfolio value in dollars.
            available_cash:  Cash available to deploy.
            drawdown_regime: Current regime (buys are NOT suppressed during
                             drawdowns — buying dips is intentional here).

        Returns:
            List of SleeveRecommendation (may be empty).
        """
        if total_portfolio <= 0:
            return []

        # Index current holdings
        held_symbols: Set[str] = set()
        spec_holdings: Dict[str, float] = {}   # symbol → market_value

        for h in holdings:
            sym = getattr(h, 'symbol', None)
            if sym:
                held_symbols.add(sym)
            if getattr(h, 'asset_class', '') == 'speculative':
                mv = getattr(h, 'market_value', None)
                if mv is not None:
                    spec_holdings[sym] = float(mv)

        # Remaining sleeve capacity
        current_sleeve_value = sum(spec_holdings.values())
        sleeve_capacity = total_portfolio * self.sleeve_total_max - current_sleeve_value

        if sleeve_capacity <= 0:
            logger.info(
                f"Spec sleeve at capacity: "
                f"${current_sleeve_value:,.0f} / "
                f"${total_portfolio * self.sleeve_total_max:,.0f}"
            )
            return []

        new_positions_added = 0
        recommendations: List[SleeveRecommendation] = []

        for candidate in candidates:
            symbol = candidate.get('symbol', '')
            if not symbol:
                continue

            is_spec_topup = symbol in spec_holdings
            is_core_held = symbol in held_symbols and not is_spec_topup

            # Skip core-portfolio holdings — don't double-dip
            if is_core_held:
                continue

            is_new = symbol not in held_symbols

            # Respect new-position monthly cap
            if is_new and new_positions_added >= self.max_new_positions:
                continue

            # Per-position budget
            per_stock_max = total_portfolio * self.max_per_stock
            current_pos = spec_holdings.get(symbol, 0.0)
            remaining_per_stock = per_stock_max - current_pos

            max_add = min(sleeve_capacity, remaining_per_stock, available_cash * 0.5)
            if max_add < self.min_position_dollars:
                continue

            # Build reason string
            parts = [f"Score {candidate.get('score', 0):.1f}/100"]
            rev_growth = candidate.get('rev_growth', 0)
            if rev_growth:
                parts.append(f"RevGrowth {rev_growth:.0%}")
            if candidate.get('above_200dma'):
                parts.append("Above 200 DMA")
            reason = '; '.join(parts)
            if not is_new:
                reason = f"Top-up existing spec position; {reason}"

            recommendations.append(SleeveRecommendation(
                symbol=symbol,
                score=float(candidate.get('score', 0)),
                sector=candidate.get('sector', ''),
                max_add_dollars=round(max_add, 2),
                is_new_position=is_new,
                current_position_dollars=current_pos,
                reason=reason,
            ))

            sleeve_capacity -= max_add
            if is_new:
                new_positions_added += 1
            if sleeve_capacity < self.min_position_dollars:
                break

        return recommendations
