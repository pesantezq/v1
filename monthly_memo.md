# Capital Deployment Memo — March 2026
**Investor:** Enrique Pesantez | **Age:** 24 | **Horizon:** 35 years
**Generated:** 2026-03-03 | **Mode:** Monthly review (dry-run — no files persisted)

---

## Executive Summary

- **Portfolio reached a new all-time high of $7,051.69**, driven almost entirely by a 16.8% rally in GLD over the past 7 weeks — equity positions (QQQ, QLD) actually declined during this period.
- **Two structural guardrail violations persist:** QQQ concentration at 51.7% (cap 40%) and QLD effective leveraged exposure at 15.5% (cap 15%). Both are overridable in growth mode; the monthly contribution is directed at dilution, not forced selling.
- **Deploy $1,898 this month (contribution + cash on hand) entirely into VFH.** This adds the missing financial-sector position, closes the −15% drift gap, and dilutes the QLD leverage violation below the cap in a single move.

---

## Portfolio Headline

| Metric | Value | Note |
|---|---|---|
| Total value | **$7,051.69** | All-time high |
| Previous ATH | $7,052 | Effectively at peak |
| Drawdown from ATH | **0.0%** | |
| Drawdown from 12m-high | **0.0%** | |
| Regime | `normal` | No anti-panic restrictions |
| Expected CAGR (current alloc) | **~7.9%** | Below 9% target — see Risk section |
| Max drift position | VFH −15.0% | 0 shares vs 15% target |
| Rebalance needed | **Yes** | Structural violations active |
| Price freshness | QQQ/VFH/VXUS live (16:00); GLD/QLD from 00:25 cache | GLD/QLD ~15h stale |
| Output freshness | **Stale** — outputs/latest/ empty | All runs today used --dry-run |

### Current vs Target Allocation

| Symbol | Shares | Price | Value | Actual Wt | Target Wt | Drift |
|---|---|---|---|---|---|---|
| QQQ | 6 | $608.09 | $3,648.54 | 51.7% | 45% | **+6.7%** ⚠ |
| VFH | 0 | $125.67 | $0.00 | 0.0% | 15% | **−15.0%** ⚠ |
| VXUS | 0 | $82.40 | $0.00 | 0.0% | 10% | **−10.0%** |
| GLD | 4 | $490.00 | $1,960.00 | 27.8% | 20% | **+7.8%** |
| QLD | 8 | $68.13 | $545.04 | 7.7% | 5% | **+2.7%** |
| Cash | — | — | $898.11 | 12.7% | 5% | +7.7% |
| **Total** | | | **$7,051.69** | 100% | 100% | |

*QLD effective leveraged exposure = 7.7% × 2× = **15.5%** (cap: 15%)*

---

## Contribution Plan — March 2026

**Monthly contribution:** $1,000 | **Cash on hand:** $898.11 | **Total deployable:** **$1,898.11**
**Regime:** normal | **Drawdown gate:** clear

### Recommended Deployment

| Priority | Symbol | Action | Amount | Shares (approx) | Rationale |
|---|---|---|---|---|---|
| **1** | **VFH** | Buy | **$1,885.05** | **15 shares @ $125.67** | Max drift −15%; adds missing sector; dilutes QLD leverage violation below cap |
| — | Cash reserve | Hold | $13.06 | — | Residual; below target 5% weight — acceptable |

### What This Does After Deployment

| Symbol | New Value | New Weight | Target | New Drift | Guardrail |
|---|---|---|---|---|---|
| QQQ | $3,648.54 | 45.3% | 45% | +0.3% | Still over 40% cap (+5.3%) |
| VFH | $1,885.05 | 23.4% | 15% | +8.4% | Clear |
| VXUS | $0 | 0.0% | 10% | −10.0% | Clear |
| GLD | $1,960.00 | 24.4% | 20% | +4.4% | Clear |
| QLD (effective) | $545.04 | **13.5%** eff. | 15% | — | **Violation cleared** ✓ |
| Cash | $13.06 | 0.2% | 5% | −4.8% | Clear |
| **New Total** | **$8,051.69** | | | | |

**Buying 15 shares of VFH clears the QLD leverage cap violation by dilution — without selling a single share.**
QQQ concentration improves from 51.7% to 45.3% but remains above the 40% cap; full clearance via dilution requires approximately one more month of contributions directed to VFH/VXUS.

> **Do not add** QQQ or QLD this month. Both positions are at or above their structural caps.

---

## Sleeve Section

**Speculative sleeve:** Enabled in config (max 10% total, 5% per position, 1 new position/month)
**Scanner:** Enabled in config (S&P 500, top-100 watchlist, min mkt cap $5B, min rev growth 15%)

| Condition | Status |
|---|---|
| Drawdown gate (>20%) | ✅ Clear — 0.0% drawdown |
| Watchlist built | ❌ No — must run `--run-mode weekly` first |
| FMP API calls (today) | 0 / 230 budget |
| Top candidates | None available |
| Spec sleeve plan | **Blocked — no watchlist** |

**Sleeve additions: operationally blocked.** The anti-panic gate is clear, but the FMP scanner watchlist has never been built. No candidates can be evaluated. First action: run `python main.py --run-mode weekly` (uses ~3 FMP calls) to build the `top100_watchlist.json`. Once built, daily runs will refresh quotes and the spec sleeve plan will activate.

---

## Risk & Guardrails Status

**Overall status: FAIL — 2 structural violations**

| # | Rule | Actual | Limit | Overage | Action |
|---|---|---|---|---|---|
| 1 | QQQ concentration cap | **51.7%** | 40% | **+11.7%** | Dilute via contributions to non-QQQ assets; selling permitted in growth mode |
| 2 | QLD effective leveraged exposure | **15.5%** | 15% | **+0.5%** | **Cleared by this month's VFH purchase** (new: 13.5%) |

**Violation #2 resolves this month** with the recommended VFH deployment.
**Violation #1 trajectory:** At current contribution rate of $1,000/month directed to non-QQQ assets, QQQ concentration reaches 40% in approximately **2 more months** (assuming flat QQQ price). Selling QQQ is permitted as a structural override, but the contribution-dilution path avoids a taxable event in this taxable account.

**Anti-panic gate:** Not triggered. Drawdown is 0.0%. All guardrail sell exemptions are active.

**Finance flags (from recommendations engine):**
- `[Action Required]` Emergency fund below target — score 88. At $3,000/month expenses, 3-month target ≈ $9,000. Current: 0.30 months (≈$898). Significant gap.
- `[Recommended]` VFH underweight — score 61. Addressed by this month's contribution plan.

---

## Projections & Milestones

**Assumptions:** $7,051.69 starting value · $1,000/month contribution · 7.9% CAGR (current allocation) / 9.0% CAGR (target allocation post-rebalance)

### 10-Year Projection

| Scenario | 10-Year Value | Note |
|---|---|---|
| Current CAGR (7.9%) | **~$197,000** | Overweight GLD/cash drag |
| Target CAGR (9.0%) | **~$211,000** | After rebalancing to targets |
| 35-year horizon (9.0%) | **~$3,100,000** | Full investment horizon |

*The gap between scenarios (~$14K at 10 years) underscores the cost of holding excess GLD and cash.*

### Milestone Estimates (7.9% CAGR, $1,000/month)

| Milestone | Estimated Date | Months Away |
|---|---|---|
| $100,000 | **~March 2032** | ~72 months |
| $250,000 | **~March 2038** | ~144 months |
| $500,000 | **~March 2044** | ~216 months |

### Impact of +$200/Month Additional Contribution

| Metric | $1,000/mo | $1,200/mo | Gain |
|---|---|---|---|
| Time to $100K | ~72 months | **~63 months** | **9 months sooner** |
| 10-year value | ~$197K | **~$221K** | +$24K |

Even a small additional $200/month compresses the $100K milestone by 9 months and adds ~$24K over the decade.

---

## What Changed Since Last Month

*Comparison: 2026-01-14 ($6,891.09) → 2026-03-03 ($7,051.69) — approximately 7 weeks*

| Item | Jan 14 | Mar 3 | Change |
|---|---|---|---|
| Portfolio value | $6,891.09 | $7,051.69 | **+$160.60 (+2.3%)** |
| QQQ price (est.) | ~$623 | $608.09 | −$14.91 (−2.4%) |
| GLD price (est.) | ~$419 | $490.00 | **+$70.51 (+16.8%)** |
| QLD price (est.) | ~$73 | $68.13 | −$4.49 (−6.2%) |
| QQQ drift | +8.94% | +6.74% | Improved (QQQ fell) |
| GLD drift | +4.72% | +7.79% | Widened (GLD rallied) |
| QLD drift | +3.43% | +2.73% | Improved (QLD fell) |
| VFH shares | 0 | 0 | No action taken |
| VXUS shares | 0 | 0 | No action taken |
| Cash on hand | $898.11 | $898.11 | Unchanged — no deployments |
| Structural violations | 2 | 2 | Unchanged |

**Key takeaway:** GLD's 16.8% rally over 7 weeks is the sole reason the portfolio hit a new ATH. Equity positions detracted. This is a healthy reminder that GLD is pulling above its 20% target weight (now 27.8%) — once the current month's VFH deployment is executed, GLD's drift will partially self-correct via dilution.

No capital was deployed since January. Cash has sat idle at $898.11. This month's deployment plan addresses that.

---

## Action Checklist

- [ ] **[CRITICAL] Run without --dry-run** — `python main.py --run-mode monthly` (no dry-run flag) to write output files, persist state, and archive to `outputs/history/2026-03-03/`.
- [ ] **[HIGH] Buy 15 shares of VFH at ~$125.67** = $1,885.05 — deploys cash on hand + March contribution, clears QLD leverage cap violation.
- [ ] **[HIGH] Build scanner watchlist** — `python main.py --run-mode weekly` to execute full S&P 500 scan (~3 FMP API calls). Spec sleeve remains blocked without this.
- [ ] **[MEDIUM] Begin emergency fund** — Target: $9,000 (3 months × $3,000 expenses). Currently at ~$898. Consider directing a portion of monthly income above the $1,000 contribution toward this before taxable account adds.
- [ ] **[MEDIUM] Plan VXUS entry** — 0 shares vs 10% target (−10% drift). Next month's contribution should split between VXUS and any remaining VFH needed.
- [ ] **[LOW] Fix Task Scheduler** — confirm production run command does not include `--dry-run`. All today's runs used dry-run; outputs/latest/ has been empty all day.
- [ ] **[LOW] Verify GLD/QLD prices** — cached from 00:25; confirm live prices before executing VFH buy if GLD/QLD movements affect total portfolio weight calculations.
