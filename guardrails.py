"""
Guardrails Module

Pre-flight structural checks that run BEFORE portfolio analysis is computed,
using only raw Holding data (market_value already set from live prices).

This is a lighter, earlier check than adjustment.detect_structural_violations(),
which runs post-analysis with full drift context.  Both can fire independently.

Checks:
  1. Concentration cap — any single holding > concentration_cap of total portfolio.
  2. Leverage cap     — aggregate effective leveraged exposure (weight × leverage_factor)
                        exceeds leverage_cap.

Output is a GuardrailResult stored in the run result dict.  Execution always
continues regardless of status — guardrails never block the pipeline.
"""

import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger('portfolio_automation.guardrails')


@dataclass
class GuardrailViolation:
    """
    A single structural violation detected during pre-flight checks.

    Attributes
    ----------
    symbol:          Ticker symbol of the offending holding, or 'PORTFOLIO'
                     for aggregate violations (e.g. total leverage cap).
    violation_type:  'concentration' or 'leverage'.
    current_pct:     Actual weight (or effective exposure) as a fraction (0-1).
    cap_pct:         The cap that was breached.
    required_action: Human-readable description of what must happen.
    """
    symbol: str
    violation_type: str          # 'concentration' | 'leverage'
    current_pct: float
    cap_pct: float
    required_action: str


@dataclass
class GuardrailResult:
    """
    Output of run_guardrail_checks().

    Attributes
    ----------
    status:     'ok' if no violations found, 'structural_violation' otherwise.
    violations: List of individual GuardrailViolation objects (may be empty).
    summary:    Single-line human-readable status for logging and the result dict.
    """
    status: str                              # 'ok' | 'structural_violation'
    violations: List[GuardrailViolation] = field(default_factory=list)
    summary: str = ""

    @property
    def has_violations(self) -> bool:
        """True if one or more violations were detected."""
        return len(self.violations) > 0

    def to_dict(self) -> dict:
        """Serialise for inclusion in the run result dict."""
        return {
            'status': self.status,
            'violation_count': len(self.violations),
            'violations': [
                {
                    'symbol': v.symbol,
                    'violation_type': v.violation_type,
                    'current_pct': round(v.current_pct, 4),
                    'cap_pct': round(v.cap_pct, 4),
                    'required_action': v.required_action,
                }
                for v in self.violations
            ],
            'summary': self.summary,
        }


def run_guardrail_checks(
    holdings: list,
    total_portfolio: float,
    concentration_cap: float = 0.40,
    leverage_cap: float = 0.15,
) -> GuardrailResult:
    """
    Execute pre-flight structural checks on holdings with live prices.

    Called in run_portfolio_update() after market prices are fetched (Step 4c),
    before scoring and recommendations are computed.  Execution continues
    regardless of the result — violations are logged and stored but do not
    halt the pipeline.

    Does NOT import from adjustment.py.

    Args:
        holdings:         List of Holding objects.  Holdings where
                          market_value is None are skipped.
        total_portfolio:  Total portfolio value in dollars. If zero or
                          negative, checks are skipped and status='ok'.
        concentration_cap: Max allowed weight for any single holding (0-1).
        leverage_cap:      Max allowed aggregate effective leveraged exposure (0-1).

    Returns:
        GuardrailResult with status='ok' or status='structural_violation'.
    """
    if total_portfolio <= 0:
        logger.warning("Guardrails: total_portfolio is zero or negative — skipping checks")
        return GuardrailResult(
            status='ok',
            summary="Skipped (zero or negative portfolio value)",
        )

    violations: List[GuardrailViolation] = []

    # ------------------------------------------------------------------
    # Check 1: Per-holding concentration cap
    # ------------------------------------------------------------------
    for holding in holdings:
        if holding.market_value is None:
            continue
        weight = holding.market_value / total_portfolio
        if weight > concentration_cap:
            excess = weight - concentration_cap
            violations.append(GuardrailViolation(
                symbol=holding.symbol,
                violation_type='concentration',
                current_pct=weight,
                cap_pct=concentration_cap,
                required_action=(
                    f"Trim {holding.symbol}: weight {weight:.1%} exceeds "
                    f"{concentration_cap:.0%} cap by {excess:.1%}"
                ),
            ))

    # ------------------------------------------------------------------
    # Check 2: Aggregate effective leveraged exposure
    # Uses the same formula as adjustment.detect_structural_violations():
    #   exposure = weight × leverage_factor  for each leveraged holding.
    # ------------------------------------------------------------------
    total_leveraged_exposure = 0.0
    for holding in holdings:
        if holding.is_leveraged and holding.market_value is not None:
            weight = holding.market_value / total_portfolio
            total_leveraged_exposure += weight * holding.leverage_factor

    if total_leveraged_exposure > leverage_cap:
        excess = total_leveraged_exposure - leverage_cap
        violations.append(GuardrailViolation(
            symbol='PORTFOLIO',
            violation_type='leverage',
            current_pct=total_leveraged_exposure,
            cap_pct=leverage_cap,
            required_action=(
                f"Reduce total leveraged exposure {total_leveraged_exposure:.1%} "
                f"to below {leverage_cap:.0%} cap (excess: {excess:.1%})"
            ),
        ))

    # ------------------------------------------------------------------
    # Build result and log outcome
    # ------------------------------------------------------------------
    if violations:
        status = 'structural_violation'
        summary = (
            f"{len(violations)} structural violation(s): "
            + "; ".join(v.required_action for v in violations)
        )
        logger.warning(f"GUARDRAILS [{status.upper()}]: {summary}")
    else:
        status = 'ok'
        summary = "All guardrail checks passed"
        logger.info(f"Guardrails: {summary}")

    return GuardrailResult(status=status, violations=violations, summary=summary)
