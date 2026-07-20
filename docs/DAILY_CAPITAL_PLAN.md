# Today's Capital Plan (daily-memo capital sections)

**Module:** `portfolio_automation/capital_plan_view.py`
**Artifact:** `outputs/latest/daily_capital_plan.json` (audit copy; observe-only)
**Consumer:** `watchlist_scanner/daily_memo.py` (renders the same view in the memo)
**Pipeline stage:** `scripts/run_daily_safe.sh` Stage 9e2 (after memo coherence, before the memo)
**Status:** observe-only, read-only. Never mutates decisions, scores, action enums,
target allocations, approved capital, production state, simulation state, or
human-approval requirements.

## Why this exists

The daily memo previously rendered two decision-plan-derived sections — **Top
Decisions** and **Capital Actions** — that were technically correct but not
decision-ready. They showed an undifferentiated three-decimal priority, an
unexplained `SELL: 1` count with no detail, and a "Total recommended capital"
figure that summed *every* recommendation's intended sizing as though the
operator should deploy it all today. An operator could not tell how much capital
was actually available, which actions were funded now, how much to allocate to
each, why one action outranked another, or what was deferred and why.

This module replaces those two sections with a single, decision-ready
**Today's Capital Plan** block that answers, in order:

1. How much capital is available today?
2. What should I actually do today?
3. How much should I allocate to each action?
4. Why are these actions prioritized?
5. What is deferred, and why?
6. What is the difference between required and funded capital?

It also **replaces** the older "investor core" block (posture / monthly / weekly
/ funded / deferred), which it supersedes — the memo now carries one capital
narrative, not two (operator decision, 2026-07-20).

## Data flow

```
memo_coherence.compute_funding        → funding split (funded/deferred, sources)
cash_deployment_plan.monthly_envelope → cash_on_hand, incoming, reserve, deployable
decision_plan.decisions               → sell detail + raw unconstrained total
        ▼
capital_plan_view.build_capital_plan_view(coherence, cash_plan, decision_plan, config)
        ▼  (normalized, read-only view model)
capital_plan_view.render_capital_plan_md(view, markdown=…)  → 6 memo sections
daily_memo.py  (renders when funding is available; legacy Top Decisions / Capital
               Actions remain as the fallback when funding data is unavailable)
```

This module **recomputes no money** — it normalizes the funding numbers
`memo_coherence` already computed. That keeps the change additive and preserves
the coherence invariants (e.g. sale proceeds are never counted as deployable).

## Honesty rules (enforced by construction + tests)

- **Money is never a silent `$0`.** Every monetary field carries an explicit
  state: `confirmed` | `missing` | `not_calculated` | `not_applicable` | `blocked`.
  A confirmed zero (e.g. no incoming contributions scheduled) is distinguished
  from missing / blocked / uncomputed.
- **Gross recommended capital is never a spend-today instruction.** The
  unconstrained recommendation total is labeled "NOT a spend-today budget."
- **Deferred capital is `not_calculated` when the system defers before sizing.**
  This system's weekly/monthly pacing defers recommendations *before* they are
  sized, so per-action deferred capital is genuinely unknown — the plan says so
  rather than fabricating a number.
- **Sale proceeds are excluded from deployable capital** until execution is
  confirmed. A `SELL` with no shares/proceeds renders an explicit
  missing-detail message, never a bare count.
- **Reconciliation is validated.** `funded + deferred ≈ gross` and per-action
  sums are checked (tolerance `$1`); a mismatch emits a visible `⚠` warning in
  the memo and preserves the raw values. Status ∈ `ok` / `partial` (deferred
  unsized) / `mismatch` / `degraded` (funding unavailable).

## Investor-facing language

The machine-readable `decision` enum is unchanged; only memo-facing labels are
translated:

| Machine enum / state | Memo label |
|---|---|
| `SCALE` | `INCREASE` (increase an existing position) |
| `BUY` fully funded | `FULL BUY` |
| `BUY` starter tranche | `STARTER BUY` |
| `SELL` / `TRIM` | `REDUCE` |
| unfunded capital action | `DEFER` |
| informational (`WAIT`/`AVOID`) | `WATCH` |
| `source: portfolio`/`finance` | "Portfolio rebalance" |
| `source: market` | "New market opportunity" |
| raw momentum / RS | plain-language entry setup |

## Deterministic ranking

Funded actions are ordered by an operator-priority **category ladder**
(risk reduction → portfolio rebalance → cash-reserve → funded increase →
high-confidence starter → lower-conviction/extended → watch), then by priority
desc, then by symbol asc as the final stable tie-break. The category also drives
the plain-language "Why" line, so equal-priority actions have an explained order.
This does **not** modify decision-engine scores.

## Configuration (`config/base.json:capital_plan`)

| Key | Default | Meaning |
|---|---|---|
| `max_deferred_displayed` | `5` | Deferred actions listed individually before the rest are summarized by reason |
| `expand_technical_details` | `true` | Show the raw "Today / distance-from-52wk-high" line under funded market opps |
| `main_section_funded_only` | `true` | "What To Do Today" shows only funded, executable actions |

All defaults live in `DEFAULT_CONFIG`, so the module is safe when the config key
is absent.

## Tests

`tests/test_capital_plan_view.py` (22 cases) covers: gross-exceeds-available,
rebalance-over-market ranking, `SCALE`→`INCREASE`, sell-without-detail warning,
funded amounts + sources, deterministic deferral reasons, momentum/RS → entry
guidance, stable ordering under tied priority, missing fields not zeroed, sale
proceeds excluded, totals reconcile, mismatch advisory, no input mutation,
observe-only/no-trade, deterministic + idempotent render, empty-funded, no
incoming, cash-below-reserve, all-funded, deferred overflow, degraded funding,
and investor-label mapping.

## Health coverage

The audit artifact is registered in `portfolio_automation/artifacts_registry.py`
(`daily_capital_plan`, consumer `watchlist_scanner.daily_memo`, optional,
observe-only-required). The rendered memo — including any `⚠` reconciliation
warning — is reviewed every run by the always-on `portfolio-memo-reviewer`
agent (daily-tool-analysis Step 3).
