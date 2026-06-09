"""The eight strategy profiles (spec §24.2). Advisory configuration only.

Profiles are declarative objective definitions — tilts, hard caps, horizon — that
the comparator scores against the actual portfolio. They are NEVER executed: no
profile trades, rebalances, or mutates holdings. The boom/aggressive caps come
from resolved decision §23.5 (≤15% total speculative, ≤5% per idea).
"""
from __future__ import annotations

from typing import Any

from portfolio_automation.next_stage.contracts import (
    StrategyProfile, StrategyId, CandidateType,
    BOOM_BUCKET_TOTAL_CAP, BOOM_BUCKET_PER_IDEA_CAP, observe_only_envelope,
)

_PUBLIC = [CandidateType.PUBLIC_TICKER.value, CandidateType.ETF.value]
_BROAD = [CandidateType.ETF.value]
_ALL = [t.value for t in CandidateType]


def _p(sid, name, objective, characteristics, **kw) -> StrategyProfile:
    return StrategyProfile(strategy_id=sid.value, name=name, objective=objective,
                           characteristics=list(characteristics), **kw)


SEED_PROFILES: dict[str, StrategyProfile] = {
    StrategyId.AGGRESSIVE_GROWTH.value: _p(
        StrategyId.AGGRESSIVE_GROWTH, "Aggressive Growth",
        "Maximize upside and capital appreciation",
        ["higher tech/growth exposure", "limited leveraged-ETF use within hard cap",
         "momentum/breakout sensitive", "active boom bucket", "higher drawdown tolerance"],
        max_total_speculative=BOOM_BUCKET_TOTAL_CAP, max_per_idea=BOOM_BUCKET_PER_IDEA_CAP,
        drawdown_tolerance="high", horizon="long_term", eligible_candidate_types=_PUBLIC),
    StrategyId.SHORT_TERM_TACTICAL.value: _p(
        StrategyId.SHORT_TERM_TACTICAL, "Short-Term Tactical",
        "Capture shorter-term market opportunities",
        ["price/volume confirmation", "news catalysts", "sector rotation",
         "1/3/7/30d windows", "small sizing", "exit criteria required before entry",
         "sandbox-heavy before review"],
        max_total_speculative=BOOM_BUCKET_TOTAL_CAP, max_per_idea=BOOM_BUCKET_PER_IDEA_CAP,
        drawdown_tolerance="medium", horizon="short_term", eligible_candidate_types=_PUBLIC),
    StrategyId.LONG_TERM_COMPOUNDING.value: _p(
        StrategyId.LONG_TERM_COMPOUNDING, "Long-Term Compounding",
        "Maximize long-term after-tax compounding",
        ["broad-market ETFs + quality growth", "new cash before selling", "low turnover",
         "avoid short-term taxable churn", "rebalancing bands", "5/10/20/30y horizon"],
        max_total_speculative=0.05, max_per_idea=0.02,
        drawdown_tolerance="normal", horizon="very_long_term", eligible_candidate_types=_BROAD),
    StrategyId.TAX_AWARE.value: _p(
        StrategyId.TAX_AWARE, "Tax-Aware",
        "Maximize after-tax returns",
        ["avoid unnecessary taxable sales", "new-cash rebalancing", "ST vs LT gain flags",
         "TLH candidates when appropriate", "wash-sale informational",
         "account-type aware if data available"],
        max_total_speculative=0.05, max_per_idea=0.02,
        drawdown_tolerance="normal", horizon="long_term", eligible_candidate_types=_BROAD),
    StrategyId.DEFENSIVE.value: _p(
        StrategyId.DEFENSIVE, "Defensive / Capital Preservation",
        "Reduce drawdown and protect capital in weak regimes",
        ["lower equity risk in stress regimes", "higher cash/treasury/defensive",
         "reduce leverage first", "tighter concentration cap", "quality/low-vol/gold hedges",
         "stricter risk-off triggers"],
        max_total_speculative=0.0, max_per_idea=0.0,
        drawdown_tolerance="low", horizon="long_term", eligible_candidate_types=_BROAD),
    StrategyId.INCOME_DIVIDEND.value: _p(
        StrategyId.INCOME_DIVIDEND, "Income / Dividend",
        "Generate yield while maintaining acceptable growth",
        ["dividend ETFs / quality dividend growth", "cash yield / bonds", "track payout quality",
         "avoid unsafe-yield chasing", "compare vs growth opportunity cost"],
        max_total_speculative=0.05, max_per_idea=0.02,
        drawdown_tolerance="normal", horizon="long_term", eligible_candidate_types=_BROAD),
    StrategyId.BALANCED_CORE_SATELLITE.value: _p(
        StrategyId.BALANCED_CORE_SATELLITE, "Balanced Core-Satellite",
        "Stable diversified core + smaller tactical/opportunity satellite",
        ["long-term diversified core", "tactical satellite captures themes",
         "strict satellite/boom max allocation"],
        max_total_speculative=BOOM_BUCKET_TOTAL_CAP, max_per_idea=BOOM_BUCKET_PER_IDEA_CAP,
        drawdown_tolerance="medium", horizon="long_term", eligible_candidate_types=_PUBLIC),
    StrategyId.BOOM_BUCKET.value: _p(
        StrategyId.BOOM_BUCKET, "Boom Bucket",
        "Maximize asymmetric upside from high-risk/high-reward ideas",
        ["speculative ideas tracked separately", "uses universe scanner + opportunity radar",
         "sandbox tracking required", "hard cap per idea + total",
         "high boom score is not enough (needs investability + evidence + risk controls)",
         "private/IPO are watch-only unless investability confirmed"],
        max_total_speculative=BOOM_BUCKET_TOTAL_CAP, max_per_idea=BOOM_BUCKET_PER_IDEA_CAP,
        drawdown_tolerance="high", horizon="medium_term", eligible_candidate_types=_ALL),
}


def build_strategy_profiles(now_iso: str) -> dict[str, Any]:
    payload = observe_only_envelope(now_iso, source="strategy_profiles",
                                    boom_total_cap=BOOM_BUCKET_TOTAL_CAP,
                                    boom_per_idea_cap=BOOM_BUCKET_PER_IDEA_CAP)
    payload["profiles"] = [p.to_dict() for p in SEED_PROFILES.values()]
    payload["profile_count"] = len(SEED_PROFILES)
    return payload
