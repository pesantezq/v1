# Portfolio Decision Memo
**Generated:** 2026-03-03 (post-16:00 run cycle)
**Investor:** Enrique Pesantez

---

## 1. Run Metadata

| Field | Value |
|---|---|
| Run mode | `daily` (dry-run × 3) |
| First run | 2026-03-03 00:33:31 (cached prices) |
| Last completed run | 2026-03-03 00:36:59 (cached prices) |
| In-progress run | 2026-03-03 15:59:41 (live prices, interrupted mid-fetch at GLD) |
| Blocked concurrent run | 2026-03-03 16:00:27 (run lock, 0m 46s held) |
| Output freshness | **STALE — outputs/latest/ is empty** (all runs used `--dry-run`; no files written) |
| Price freshness | Live prices confirmed for QQQ, VFH, VXUS; GLD fetch was in-progress |
| FMP API calls today | 0 (scanner watchlist absent; daily scan skipped) |

> **Warning:** All three runs today were dry-runs. No `contribution_plan.csv`, `compounding_dashboard.txt`, or snapshot files were written. Output TTL is effectively expired.

---

## 2. Portfolio Headline

| Metric | Value |
|---|---|
| Total portfolio value | **$7,051.69** |
| All-time high | $7,052 (today — at ATH) |
| Drawdown from ATH | **0.0%** |
| Drawdown from 12m-high | **0.0%** |
| Regime | `normal` |
| Expected CAGR | Unknown — projections computed but not persisted (dry-run) |
| Max drift position | **VFH −15.0%** (target 15%, actual 0 shares) |
| Rebalance needed | **Yes** |

### Estimated Position Breakdown (from log + config)

| Symbol | Shares | Live Price | Est. Value | Est. Weight | Target | Drift |
|---|---|---|---|---|---|---|
| QQQ | 6 | $608.09 | $3,648.54 | 51.7% | 45% | **+6.7%** |
| VFH | 0 | $125.67 | $0.00 | 0.0% | 15% | **−15.0%** ⚠ |
| VXUS | 0 | $82.40 | $0.00 | 0.0% | 10% | **−10.0%** |
| GLD | 4 | ~$353 (est.) | ~$1,412 | ~20.0% | 20% | ~0.0% |
| QLD | 8 | ~$137 (est.) | ~$1,093 | ~15.5% | 5% | **+10.5%** ⚠ |
| Cash | — | — | $898.11 | 12.7% | 5% | +7.7% |

*GLD and QLD prices are back-calculated estimates from ATH total and structural violation data; verify against actual prices.*

---

## 3. Contribution Plan

**Monthly contribution:** $1,000 | **Regime:** normal | **Targets:** 1 holding (dry-run — target name not persisted)

Based on max-drift logic and recommendations, the contribution is directed to **VFH** (highest underweight at −15.0%, explicitly flagged as "Action Required" + "Recommended").

| Priority | Target | Rationale | Est. Amount |
|---|---|---|---|
| 1 | **VFH** | Max drift −15.0%; 0 shares vs 15% target; flagged score 61 | $1,000 |
| — | VXUS | Second-most underweight at −10.0%; 0 shares vs 10% target | Future months |
| — | QQQ | **Do not add** — structural violation active (51.7% > 40% cap) | $0 |
| — | QLD | **Do not add** — leverage cap violated (15.5% > 15%) | $0 |

> Contribution engine confirms $1,000.00 of $1,000.00 allocated to 1 holding (regime: normal).
> $898.11 cash on hand is also available to deploy into VFH (≈7 additional shares at $125.67).

---

## 4. Guardrails Status

**Status: FAIL — 2 structural violations**

| # | Violation | Actual | Cap | Excess |
|---|---|---|---|---|
| 1 | **QQQ concentration** | 51.7% | 40.0% | **+11.7%** |
| 2 | **Leveraged exposure (QLD)** | 15.5% | 15.0% | **+0.5%** |

- Violations are **consistent across all 3 runs today** — not transient.
- Growth mode `accumulation_aggressive`: selling is normally suppressed **except** for concentration and leverage cap violations. Both active violations qualify for remediation via sell.
- Drawdown is 0.0% — anti-panic gate is **not** triggered; sell recommendations are permitted for these violations.

---

## 5. Sleeve Status

**Speculative sleeve:** `enabled: true` (config)
**Scanner:** `enabled: true` (config)
**Scanner status: BLOCKED — no watchlist built**

| Condition | Status |
|---|---|
| Drawdown gate (>20%) | Clear — 0.0% drawdown |
| Watchlist present | **No** — must run `weekly` or `monthly` mode first |
| FMP API calls today | 0 / 230 budget |
| Top candidates | None available |
| Spec sleeve plan | Not generated |

> Action required: Run `python main.py --run-mode weekly` to build the S&P 500 watchlist (~3 FMP API calls). Until then, daily scanner is inoperable.

---

## 6. Changes Since Last Run

No output-file delta is available (all dry-runs). Intra-day log comparison across the 3 completed runs shows **no change** in any computed metric — consistent data across 00:33, 00:35, and 00:36 runs (cached prices used).

The 15:59 run represents the first live-price fetch today:

| Symbol | Cached → Live | Delta |
|---|---|---|
| QQQ | unknown → **$608.09** | — |
| VFH | unknown → **$125.67** | — |
| VXUS | unknown → **$82.40** | — |
| GLD | unknown → **unknown** (fetch interrupted) | — |
| QLD | unknown → **unknown** | — |

> No portfolio value shift confirmed since all runs produced $7,051.69 — prices were either cached or mid-fetch.

---

## 7. Action Checklist

- [ ] **[URGENT] Run without dry-run** — execute `python main.py --run-mode daily` (no `--dry-run` flag) to write `outputs/latest/` files and persist state.
- [ ] **[URGENT] Trim QQQ** — QQQ at 51.7% exceeds 40% concentration cap by 11.7%. Consider selling 1 share (~$608) to reduce weight to ~49%. Full remediation to 45% target requires ~$819 in proceeds redeployed to underweight positions.
- [ ] **[URGENT] Reduce QLD** — leveraged exposure at 15.5% vs 15% cap. Sell 0.5% of portfolio (~$35 worth ≈ partial share) or do not add QLD until diluted by contributions.
- [ ] **[HIGH] Build scanner watchlist** — run `python main.py --run-mode weekly` to execute full S&P 500 scan and build `data/fmp_cache/top100_watchlist.json` (~3 FMP calls).
- [ ] **[HIGH] Deploy cash + contribution into VFH** — $898.11 cash + $1,000 contribution = $1,898.11 available → buy ~15 shares VFH at $125.67. Reduces cash weight from 12.7% to ~5% and starts closing VFH gap.
- [ ] **[MEDIUM] Fund emergency fund** — flagged Action Required, score 88. At $96K income + $3K/month expenses, 3-month emergency fund target = ~$9,000 (verify current balance).
- [ ] **Check GLD price** — 15:59 run was interrupted mid-fetch; GLD and QLD live prices unconfirmed.
