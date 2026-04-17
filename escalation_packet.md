# Escalation Packet
**Date:** 2026-03-03
**Severity:** HIGH
**Escalation triggers:** Guardrail structural violations (2) + Stale/empty outputs

---

## Reason

Two simultaneous escalation conditions are active:

1. **Guardrail structural violations (persistent)** — QQQ concentration and leveraged exposure cap both breached across every run today.
2. **Stale outputs** — `outputs/latest/` is empty; all today's runs used `--dry-run`. No contribution plan, snapshot, or projection files have been written. The system is producing decisions without persisting them.

---

## Evidence

### Structural Violations (all 3 runs, 2026-03-03)

```
GUARDRAILS [STRUCTURAL_VIOLATION]: 2 structural violation(s):
  Trim QQQ: weight 51.7% exceeds 40% cap by 11.7%
  Reduce total leveraged exposure 15.5% to below 15% cap (excess: 0.5%)
```

**QQQ** (6 shares × $608.09 = $3,648.54):
- Actual weight: **51.7%** vs 40% cap
- Overage: **+11.7%** (~$824 excess)
- Consistent across all 3 runs (00:33, 00:35, 00:36)

**QLD** (8 shares, leveraged 2×):
- Total leveraged exposure: **15.5%** vs 15% cap
- Overage: **+0.5%** (~$35 excess)
- Consistent across all 3 runs

### Empty Output Directory

```
outputs/latest/ is empty or does not exist.
```

All runs confirmed `Dry run - skipping file writes`. Last persisted state unknown.

### In-Progress Run Interrupted

```
2026-03-03 15:59:41 | Fetching price for GLD
2026-03-03 16:00:05 | Rate limiting: waiting 11.9s before next API call
2026-03-03 16:00:27 | Run lock is active (PID 105680, held for 0m 46s) — exiting.
```

The 15:59 live-price run was mid-fetch when a concurrent start was blocked. GLD and QLD prices unconfirmed.

### Key Metrics Summary

| Metric | Value |
|---|---|
| Portfolio total | $7,051.69 |
| Drawdown | 0.0% (at ATH — NOT the cause) |
| QQQ weight | 51.7% (cap: 40%) |
| Leveraged exposure | 15.5% (cap: 15%) |
| VFH actual weight | 0.0% (target: 15%) |
| outputs/latest/ files | 0 |
| Scanner watchlist | Missing |

---

## Recommended Fix Steps

1. **Run live (non-dry-run) immediately** — execute `python main.py --run-mode daily` to write output files and update state store. Verify `outputs/latest/contribution_plan.csv` is created.

2. **Trim QQQ** — sell 1 share of QQQ (~$608) and redeploy proceeds into VFH. This reduces QQQ from 51.7% toward 49%; repeat next month to reach 45% target. Full remediation to 40% cap via contributions alone would require ~$2,070 of non-QQQ purchases.

3. **Trim QLD** — reduce leveraged exposure below 15% cap. Sell a fractional share (~$35 worth) or halt any new QLD purchases until contribution dilution closes the gap (~1 month at current $1,000/month).

4. **Build scanner watchlist** — run `python main.py --run-mode weekly` (uses ~3 FMP API calls, within 230/day budget). Without this, daily scanner is permanently blocked and speculative sleeve produces no candidates.

5. **Review dry-run flag in scheduler** — Task Scheduler may be passing `--dry-run` unintentionally. Confirm the production schedule task uses the command without that flag to prevent recurrence of empty `outputs/latest/`.
