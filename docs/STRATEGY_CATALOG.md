# Strategy Catalog

_Sandbox simulation strategies — observe-only. Not trade recommendations._

Tactics documented: 16 · coverage complete: True

## Actual Baseline (`shadow_actual_baseline`)
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: The operator's real holdings — the anchor every tactic is measured against.
- Explanation: Actual Baseline: weight vector [('NASA', 0.4054), ('QLD', 0.2162), ('QQQ', 0.1622), ('GLD', 0.1081), ('CHAT', 0.1081)].
- Latest: excess vs SPY 0.192419, CAGR 0.669443, maxDD -0.160046 (YTD 2026)

## Target Allocation Baseline (`shadow_target_allocation_baseline`)
- Universe: CHAT, GLD, NASA, QLD, QQQ, VFH, VXUS
- Rationale: Config target weights — where the portfolio is steering.
- Explanation: Target Allocation Baseline: weight vector [('QQQ', 0.35), ('GLD', 0.2), ('VFH', 0.15), ('NASA', 0.1), ('VXUS', 0.1)].
- Latest: excess vs SPY 0.041495, CAGR 0.293896, maxDD -0.12453 (YTD 2026)

## Engine Followed (`shadow_engine_followed`)
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: What the decision engine would hold (advisory reference).
- Explanation: Engine Followed: weight vector [('NASA', 0.4054), ('QLD', 0.2162), ('QQQ', 0.1622), ('GLD', 0.1081), ('CHAT', 0.1081)].
- Latest: excess vs SPY 0.192419, CAGR 0.669443, maxDD -0.160046 (YTD 2026)

## Lower Risk (`shadow_lower_risk`)
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: Trims the largest position toward equal-weight to show a de-risked variant.
- Explanation: Lower Risk: weight vector [('NASA', 0.3230676932306769), ('QLD', 0.24607539246075394), ('QQQ', 0.18468153184681532), ('GLD', 0.12308769123087691), ('CHAT', 0.12308769123087691)].
- Latest: excess vs SPY 0.192413, CAGR 0.669428, maxDD -0.160036 (YTD 2026)

## Discovery Enhanced (`shadow_discovery_enhanced`)
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: Core + a capped sleeve of qualified discovery names.
- Explanation: Discovery Enhanced: weight vector [('NASA', 0.4054), ('QLD', 0.2162), ('QQQ', 0.1622), ('GLD', 0.1081), ('CHAT', 0.1081)].
- Latest: excess vs SPY 0.192419, CAGR 0.669443, maxDD -0.160046 (YTD 2026)

## Boom Bucket (`shadow_boom_bucket`)
- Universe: CHAT, GLD, NASA, QLD, QQQ, XLE, XLF, XLRE, XLV
- Rationale: Core + a capped speculative sleeve (≤15%/≤5% per idea).
- Explanation: Boom Bucket: weight vector [('NASA', 0.34456554344565543), ('QLD', 0.1837816218378162), ('QQQ', 0.13788621137886212), ('GLD', 0.0918908109189081), ('CHAT', 0.0918908109189081)].
- Latest: excess vs SPY 0.141387, CAGR 0.537004, maxDD -0.120265 (YTD 2026)

## Aggressive Growth (`profile_aggressive_growth`)
- Objective: Maximize upside and capital appreciation
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: Growth/leverage tilt within the leverage cap — max upside objective.
- Explanation: Aggressive Growth: weights derived from the actual portfolio with tilts [equity ×1.5, leveraged ×1.4, gold ×0.4, bond ×0.2], normalized and clamped to config caps.
- Latest: excess vs SPY 0.240941, CAGR 0.800551, maxDD -0.157691 (YTD 2026)

## Short-Term Tactical (`profile_short_term_tactical`)
> ⚠️ Approximate static stand-in.
- Objective: Capture shorter-term market opportunities
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: APPROXIMATE static stand-in for a signal-driven tactic; faithful version deferred (look-ahead risk).
- Explanation: Short-Term Tactical: weights derived from the actual portfolio with tilts [equity ×1.3, leveraged ×1.2, gold ×0.5], normalized and clamped to config caps.
- Latest: excess vs SPY 0.231887, CAGR 0.775701, maxDD -0.15749 (YTD 2026)

## Long-Term Compounding (`profile_long_term_compounding`)
- Objective: Maximize long-term after-tax compounding
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: Broad-ETF, low-turnover tilt for long-horizon after-tax compounding.
- Explanation: Long-Term Compounding: weights derived from the actual portfolio with tilts [equity ×1.2, leveraged ×0.5, gold ×0.8], normalized and clamped to config caps.
- Latest: excess vs SPY 0.205091, CAGR 0.703196, maxDD -0.139474 (YTD 2026)

## Tax-Aware (`profile_tax_aware`)
- Objective: Maximize after-tax returns
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: Broad ETFs + new-cash rebalancing bias to minimize taxable churn.
- Explanation: Tax-Aware: weights derived from the actual portfolio with tilts [equity ×1.2, leveraged ×0.5, gold ×0.8], normalized and clamped to config caps.
- Latest: excess vs SPY 0.205091, CAGR 0.703196, maxDD -0.139474 (YTD 2026)

## Defensive / Capital Preservation (`profile_defensive_capital_preservation`)
- Objective: Reduce drawdown and protect capital in weak regimes
- Universe: BND, CHAT, GLD, NASA, QQQ, TLT, USMV
- Rationale: Zeroes leverage, raises gold/bonds/low-vol — drawdown protection.
- Explanation: Defensive / Capital Preservation: weights derived from the actual portfolio with tilts [leveraged ×0.0, equity ×0.6, gold ×1.5, bond floor 0.2, low_vol floor 0.15], normalized and clamped to config caps.
- Latest: excess vs SPY -0.030614, CAGR 0.131642, maxDD -0.072304 (YTD 2026)

## Income / Dividend (`profile_income_dividend`)
- Objective: Generate yield while maintaining acceptable growth
- Universe: BND, CHAT, GLD, NASA, QQQ, SCHD, TLT
- Rationale: Dividend + bond floors for yield with acceptable growth.
- Explanation: Income / Dividend: weights derived from the actual portfolio with tilts [leveraged ×0.0, equity ×0.7, dividend floor 0.3, bond floor 0.2], normalized and clamped to config caps.
- Latest: excess vs SPY 0.024784, CAGR 0.255307, maxDD -0.058745 (YTD 2026)

## Balanced Core-Satellite (`profile_balanced_core_satellite`)
- Objective: Stable diversified core + smaller tactical/opportunity satellite
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: Diversified core + a small tactical satellite within caps.
- Explanation: Balanced Core-Satellite: weights derived from the actual portfolio with tilts [equity ×1.1, leveraged ×0.8, gold ×1.0], normalized and clamped to config caps.
- Latest: excess vs SPY 0.19309, CAGR 0.671223, maxDD -0.152204 (YTD 2026)

## Boom Bucket (`profile_boom_bucket`)
- Objective: Maximize asymmetric upside from high-risk/high-reward ideas
- Universe: CHAT, GLD, NASA, QLD, QQQ
- Rationale: Asymmetric-upside tilt to leverage/growth within hard caps.
- Explanation: Boom Bucket: weights derived from the actual portfolio with tilts [leveraged ×1.5, equity ×1.1, gold ×0.6], normalized and clamped to config caps.
- Latest: excess vs SPY 0.221713, CAGR 0.74799, maxDD -0.163407 (YTD 2026)

## S&P 500 (SPY) (`benchmark_spy`)
- Universe: SPY
- Rationale: The S&P 500 — the operator's primary beat-the-market benchmark.
- Explanation: S&P 500 (SPY): weight vector [('SPY', 1.0)].
- Latest: excess vs SPY -0.0, CAGR 0.199173, maxDD -0.091331 (YTD 2026)

## Nasdaq-100 (QQQ) (`benchmark_qqq`)
- Universe: QQQ
- Rationale: Nasdaq-100 — secondary benchmark.
- Explanation: Nasdaq-100 (QQQ): weight vector [('QQQ', 1.0)].
- Latest: excess vs SPY 0.107975, CAGR 0.453314, maxDD -0.118347 (YTD 2026)
