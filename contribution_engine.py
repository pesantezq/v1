"""
Contribution Optimization Engine

Allocates the monthly investment contribution across portfolio holdings to
maximize long-term wealth growth.

Strategy (Aggressive Wealth Growth Mode):
- Direct 100% of contribution to the most underweight *core* (non-leveraged) holding.
- Split across multiple holdings only when a concentration cap would be exceeded.
- During drawdowns, tilt allocation toward equity-class assets.
- Leveraged holdings are never targets for new contributions.
- Selling is DISABLED here; this engine only advises on where to BUY.
"""

import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

logger = logging.getLogger('portfolio_automation.contribution_engine')

# Asset classes treated as equity for drawdown-tilt logic
EQUITY_ASSET_CLASSES = frozenset({
    'us_equity',
    'us_equity_sector',
    'international_equity',
    'us_equity_leveraged',  # included for equity-exposure tracking, excluded from new buys
})


@dataclass
class ContributionAllocation:
    """Recommended contribution dollar amount for a single holding."""
    symbol: str
    asset_class: str
    current_weight: float   # Actual weight in portfolio
    target_weight: float    # Config target weight
    drift: float            # Negative = underweight
    recommended_dollars: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'Symbol': self.symbol,
            'AssetClass': self.asset_class,
            'CurrentWeight': f"{self.current_weight:.2%}",
            'TargetWeight': f"{self.target_weight:.2%}",
            'Drift': f"{self.drift:+.2%}",
            'RecommendedContributionDollars': round(self.recommended_dollars, 2),
            'Reason': self.reason,
        }


class ContributionEngine:
    """
    Computes an optimal contribution allocation plan.

    Parameters
    ----------
    concentration_cap : float
        Maximum allowed weight for any single holding (default 0.40 = 40%).
        Contributions that would push a holding above this cap are redirected.
    leverage_cap : float
        Maximum leveraged-exposure fraction (informational; not used for
        contribution targeting — leveraged holdings are always skipped).
    """

    def __init__(
        self,
        concentration_cap: float = 0.40,
        leverage_cap: float = 0.15,
    ):
        self.concentration_cap = concentration_cap
        self.leverage_cap = leverage_cap

    def allocate(
        self,
        holdings: list,   # List[Holding] with actual_weight set
        analyses: list,   # List[HoldingAnalysis] with drift set
        total_portfolio: float,
        monthly_contribution: float,
        drawdown_regime: str = 'normal',
    ) -> List[ContributionAllocation]:
        """
        Return a list of ContributionAllocation objects that sum to
        (at most) monthly_contribution.

        Algorithm:
        1. Collect eligible candidates: underweight, non-leveraged, priced.
        2. Apply drawdown tilt: equity-class assets sort first during dips.
        3. Greedily fill from most-underweight, respecting concentration cap.
        4. In 'severe_dip' mode, remaining undeplored dollars are added to the
           top equity candidate (deploy all available capital).
        """
        if monthly_contribution <= 0 or total_portfolio <= 0:
            return []

        prefer_equity = drawdown_regime in ('modest_dip', 'significant_dip', 'severe_dip')

        # Build candidate list
        candidates = []
        for holding, analysis in zip(holdings, analyses):
            if holding.current_price is None or analysis.drift is None:
                continue
            if holding.is_leveraged:
                continue  # Never direct new contributions to leveraged positions
            if analysis.drift >= 0:
                continue  # Skip at-weight or overweight holdings

            current_weight = holding.actual_weight or 0.0
            cap_headroom_pct = self.concentration_cap - current_weight
            if cap_headroom_pct <= 0:
                continue  # Already at or above concentration cap

            is_equity = holding.asset_class in EQUITY_ASSET_CLASSES

            candidates.append({
                'holding': holding,
                'analysis': analysis,
                'drift': analysis.drift,
                'abs_drift': abs(analysis.drift),
                'current_weight': current_weight,
                'cap_headroom_pct': cap_headroom_pct,
                'is_equity': is_equity,
            })

        if not candidates:
            logger.info("No eligible underweight holdings for contribution allocation")
            return []

        # Sort: during drawdowns equity gets priority, then by magnitude of underweight
        if prefer_equity:
            candidates.sort(key=lambda c: (0 if c['is_equity'] else 1, -c['abs_drift']))
        else:
            candidates.sort(key=lambda c: -c['abs_drift'])

        tilt_label = {
            'modest_dip': "equity tilt (10-20% drawdown)",
            'significant_dip': "aggressive equity tilt (20-30% drawdown)",
            'severe_dip': "maximum equity tilt — deploying all cash (>30% drawdown)",
        }.get(drawdown_regime, "")

        # Greedy allocation
        allocations: List[ContributionAllocation] = []
        remaining = monthly_contribution

        for cand in candidates:
            if remaining < 1.0:
                break

            # Maximum dollars before hitting concentration cap
            max_by_cap = cand['cap_headroom_pct'] * total_portfolio
            # Optimal amount to fill the drift deficit
            deficit_dollars = cand['abs_drift'] * total_portfolio
            dollars = min(remaining, deficit_dollars, max_by_cap)

            if dollars < 1.0:
                continue

            reason_parts = [f"Underweight {cand['abs_drift']:.1%}"]
            if tilt_label:
                reason_parts.append(tilt_label)
            if cand['cap_headroom_pct'] < 0.10:
                reason_parts.append(
                    f"Near concentration cap ({self.concentration_cap:.0%}); capped"
                )

            allocations.append(ContributionAllocation(
                symbol=cand['holding'].symbol,
                asset_class=cand['holding'].asset_class,
                current_weight=cand['current_weight'],
                target_weight=cand['holding'].target_weight,
                drift=cand['drift'],
                recommended_dollars=round(dollars, 2),
                reason="; ".join(reason_parts),
            ))
            remaining -= dollars

        # In severe drawdown, sweep any remaining dollars into the top equity candidate
        if remaining >= 1.0 and drawdown_regime == 'severe_dip' and allocations:
            top = allocations[0]
            allocations[0] = ContributionAllocation(
                symbol=top.symbol,
                asset_class=top.asset_class,
                current_weight=top.current_weight,
                target_weight=top.target_weight,
                drift=top.drift,
                recommended_dollars=round(top.recommended_dollars + remaining, 2),
                reason=top.reason + "; all remaining cash deployed (severe drawdown)",
            )

        total_allocated = sum(a.recommended_dollars for a in allocations)
        logger.info(
            f"Contribution plan: ${total_allocated:,.2f} of ${monthly_contribution:,.2f} "
            f"allocated to {len(allocations)} holdings (regime: {drawdown_regime})"
        )
        return allocations
