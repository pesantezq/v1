# Design — Portfolio Tactic Backtest Engine (Sandbox Sub-Project 1)

> Status: design approved (brainstorm 2026-06-12). Sandbox-only · observe-only ·
> no auto-trading. Part of the "sandbox runs sims with my portfolio + other
> tactics" workstream, decomposed into 3 sequenced sub-projects; **this spec is
> sub-project 1 (the foundation)**.

## 1. Goal

Let the sandbox answer, for the operator's *real* portfolio: **"if I had run
tactic X over the last 1/3/5 years, what would my realized return, drawdown, and
Sharpe actually have been — vs my baseline and vs SPY/QQQ?"**

It evaluates concrete weight-vector tactics against the existing 5-year daily
price archive, reporting both a contribution-neutral time-weighted return and a
realistic dollar path that injects the operator's monthly DCA contribution.

Non-goals (deferred to later sub-projects): faithful time-varying / signal-driven
tactics (sub-project 2 — crowd-signal tactic), forward Monte-Carlo projection
(sub-project 3), and transaction-cost/tax/slippage modeling in the P&L.

## 1a. Operator objective (config-driven, added 2026-06-12)

Per the operator: the headline objective is **"make the most money possible vs
the S&P 500"** — i.e. maximize **excess return over SPY** — with strategies
**anchored on the operator's actual portfolio**, evaluated **across different
periods of the year**, and **as a function of how much is contributed**. This is
encoded in `config.json` → `portfolio_sim`:

- `objective: maximize_excess_vs_sp500`, `primary_benchmark: SPY` — the
  leaderboard ranks tactics by **excess vs SPY** (alpha over the S&P 500), with
  QQQ as a secondary benchmark.
- `anchor: actual_portfolio` — the `actual_baseline` tactic (real holdings) is
  the reference every other tactic is compared against.
- `windows: [ytd, trailing_1y, trailing_3y, trailing_5y, calendar_quarter,
  calendar_month]` — **"different periods of the year"**: trailing windows AND
  intra-year calendar periods (YTD, per-quarter, per-month) so seasonality is
  visible, not just multi-year trailing returns.
- `contribution_scenarios: [500, 1000, 2000]` — **"based on how much money I put
  in"**: each tactic is also evaluated at multiple monthly-DCA levels so the
  operator can see how outcomes scale with contribution size.
- `projection`, `universe`, `rebalance_policies` sub-blocks configure sub-projects
  1 & 3.

The engine therefore resolves named windows into `(start,end)` ranges from the
price calendar (new `windows.py`), runs each tactic × policy × window ×
contribution-scenario, and the primary sort key is `excess_vs_spy`.

## 2. Hard boundaries

- **Sandbox-only / observe-only.** All writes via `OutputNamespace.SANDBOX`.
  No trade verbs, never writes `decision_plan.json`, `config.json`, or
  `signal_registry.yaml`. Run-mode: `discovery`/`backtest`.
- **Read historical, surface through shadow.** Price *inputs* read from
  `outputs/backtest/historical/<TICKER>_5y.json` (HISTORICAL archive); result
  *artifacts* written to `outputs/sandbox/` (the shadow lane, next to
  `shadow_portfolios.json` and the Strategy Lab tab).
- **No look-ahead.** Only prices dated ≤ the simulation date are used; static
  weight-vector tactics do not peek at signals. The one approximate tactic
  (Short-Term Tactical) is flagged `approximate`.
- **Additive / non-blocking.** Wrapped in try/except; a failure degrades to a
  status dict and never aborts the pipeline.

## 3. Module layout

New package `portfolio_automation/portfolio_sim/` (small single-purpose units):

```
portfolio_sim/
  __init__.py
  tactics.py            # Tactic dataclass + materializers
  universe.py           # resolve simulable universe (holdings ∪ proxies ∪ universe_lists)
  rebalance.py          # RebalancePolicy: buy_and_hold · periodic · config_rules
  prices.py             # load *_5y.json archive, FMP fallback, calendar-align panel
  metrics.py            # CAGR · vol · max_drawdown · Sharpe · Sortino; TW + DCA paths
  backtest_engine.py    # (tactic, policy, price_panel, window) → series + metrics
  strategy_docs.py      # strategy-catalog producer (rule mechanism)
  run_portfolio_backtest.py  # orchestrator → artifacts
```

## 4. Tactic interface + materializers

```python
@dataclass
class Tactic:
    tactic_id: str
    name: str
    source: str                       # shadow | strategy_profile | benchmark | baseline
    target_weights: dict[str, float]  # normalized
    metadata: dict                    # caps, horizon, drawdown_tolerance, materialization map
    approximate: bool = False         # True for the static stand-in of a rules tactic
```

Materializers (pure functions):

- `from_shadow_portfolios(root)` → the 6 weight-vectors `shadow_tracker`
  already builds from real holdings (reuse `build_shadow_portfolios`; DRY).
- `from_strategy_profiles(root)` → the 8 `SEED_PROFILES`, materialized into
  concrete weights over the **resolved universe** via bounded tilt multipliers
  (see §5). Each profile records its tilt map in `metadata.materialization`.
- `benchmark_tactics()` → SPY 100%, QQQ 100%.

## 5. Profile → weights materialization (the new modeling)

The 8 profiles are declarative objective definitions, not weights. Materialize
each deterministically from the resolved universe (§6) by applying its
declarative tilts as **bounded multipliers**, then normalize and clamp to the
config caps (`concentration_cap 0.60`, `leverage_cap 0.25`):

| Profile | Tilt rule (illustrative) |
|---|---|
| Aggressive Growth | ×1.5 growth/tech (QQQ/QLD/watchlist growth), trim GLD/bonds; leverage ≤ cap |
| Short-Term Tactical | **`approximate`** static tilt toward recent-momentum names; faithful version deferred |
| Long-Term Compounding | broad ETFs only, low turnover (pairs with buy_and_hold), spec ≤5% |
| Tax-Aware | broad ETFs, new-cash rebalancing bias, spec ≤5% |
| Defensive | QLD→~0, raise GLD + bonds + low-vol, equity ↓, tighter concentration |
| Income / Dividend | tilt to dividend ETF + bonds |
| Balanced Core-Satellite | broad core + capped (≤15%/≤5%) satellite from radar names |
| Boom Bucket | core + capped speculative sleeve from opportunity radar |

The exact numeric tilt map for every profile is written into the artifact and
the Strategy Catalog (§9) — **no undocumented magic numbers** (enforced by §10).

## 6. Resolved simulable universe (dynamic, not fixed)

Per run, the universe = **operator holdings ∪ proxy-ETF set ∪ optional extras
from `config/universe_lists.yaml` / watchlist**. Proxy set (config-overridable):
bond/treasury (e.g. BND/TLT), dividend (e.g. SCHD), low-vol (e.g. USMV). A
ticker lacking a `*_5y.json` archive is backfilled once via the free FMP
`get_historical_prices` loader (reuse `historical_replay.replay_data_loader`).
Universe membership is a config input so the operator can widen/narrow it to
"try different strategies" without code edits (honors the no-static-variables
rule).

## 7. Rebalance policies

`RebalancePolicy.apply(holdings_value_by_ticker, target_weights, date, cash_in)
→ new_holdings`. Three implementations behind one interface:

- `buy_and_hold` — weights set once at t0; contributions added to cash/pro-rata.
- `periodic(freq=monthly)` — rebalance to target on each period boundary.
- `config_rules` — the operator's real `rebalance_rules` (0.12 band,
  cash-before-selling, contributions-first, avoid-taxable-sales). Built behind
  the same interface; ships after the two simple policies.

Default run evaluates each tactic under `buy_and_hold` + `periodic`;
`config_rules` is opt-in.

## 8. Engine + metrics

`backtest_engine.run(tactic, policy, price_panel, window, start_value,
monthly_contribution)`:

- Walk the daily trading calendar; track shares per ticker; mark-to-market →
  daily value series.
- Two paths: **time-weighted** (start $1, no injections — clean comparison) and
  **DCA dollar path** (start at current portfolio value + $1,000/mo injected).
- Apply the rebalance policy on its schedule.

Output per **tactic × policy × window** (1y/3y/5y): `cagr`, `annual_vol`,
`max_drawdown`, `sharpe`, `sortino`, `time_weighted_return`, `final_balance_dca`,
`total_contributed`, `excess_vs_spy`, `excess_vs_qqq`, downsampled `value_series`,
and `degraded:[tickers]` (any dropped-for-missing-history, renormalized — never
silent).

## 9. Strategy Documentation layer (rule mechanism)

`strategy_docs.py` — for every Tactic + latest backtest result, emit a **strategy
card**: objective · resolved universe · materialization explanation (plain
language) · rebalance assumptions · caps applied · latest metrics per window ·
look-ahead/degraded notes · **decision & rationale for every tunable parameter**.

Artifacts:
- `outputs/sandbox/strategy_catalog.json` (machine).
- `docs/STRATEGY_CATALOG.md` (human, auto-generated).
- Major parameter decisions appended to `docs/CHANGELOG_DECISIONS.md`.

Skill `/strategy-catalog` (`.claude/commands/strategy-catalog.md`): regenerates
the catalog from the tactic registry + latest backtest, writes plain-language
explanations, routes prose-quality findings to the existing
`portfolio-doc-writer` agent (same pattern as `/doc-audit`). Observe-only.

**Rule** (added to `CLAUDE.md`): *Strategy Documentation Requirement* — every
Tactic must ship with a strategy-catalog entry and every tunable parameter must
record its rationale; an undocumented tactic is incomplete and must not surface
in the Strategy Lab. The doc-audit tier verifies coverage.

## 10. Outputs / surfacing

- `outputs/sandbox/portfolio_backtest.json` — per tactic×policy×window metrics +
  series + materialization map + degraded notes + observe-only envelope.
- `outputs/sandbox/portfolio_backtest_summary.md` — operator-readable leaderboard.
- `outputs/sandbox/strategy_catalog.json` + `docs/STRATEGY_CATALOG.md` (§9).
- Feeds the `would_have_helped_portfolio` field already waiting in
  `shadow_portfolios.json` (this is the "richer scorecards" piece).
- **GUI Strategy Lab tab**: add a "Backtest" section — leaderboard table
  (tactic · policy · window → CAGR / maxDD / Sharpe / final $) + value sparklines.
  Reuses the existing tab; no new tab.
- **Daily memo**: optional one-line heartbeat ("Backtest: best risk-adj tactic
  over 3y = X, Sharpe S") — research-framed, never a recommendation.

## 11. Cadence + health coverage (CLAUDE.md requirement)

- Cadence: **weekly** (Monday, after the price-archive refresh) + on-demand CLI.
  Wrapped non-blocking. (Results barely move daily; the archive refreshes weekly.)
- Register `portfolio_backtest.json`, `portfolio_backtest_summary.md`,
  `strategy_catalog.json` in `artifact_registry.yaml` (weekly cadence).
- Extend `monthly-tool-analysis` (quant lens, owning agent
  `portfolio-attribution-analyst`) with a backtest trend read + a content-liveness
  check (engine ran but every tactic degraded → empty result). `/strategy-catalog`
  coverage verified by the doc-audit tier.

## 12. Tests

- `metrics.py`: known synthetic series → known CAGR / max_drawdown / Sharpe /
  Sortino; TW vs DCA path divergence.
- `rebalance.py`: buy_and_hold drift vs periodic reset vs config_rules band.
- `prices.py`: archive load + calendar alignment + forward-fill ≤N days;
  FMP-fallback path mocked (no live key).
- look-ahead guard: engine never reads a price dated > sim date.
- missing-ticker degradation: dropped + renormalized + recorded.
- profile materialization: each of 8 profiles → normalized, capped weights;
  Short-Term Tactical flagged `approximate`.
- end-to-end on a tiny 2-ticker synthetic panel → both artifacts written,
  `observe_only=True`, `decision_plan.json` untouched (no-mutation invariant).
- `strategy_docs`: every tactic produces a card; a tactic with no rationale
  fails the coverage check (the rule, under test).

## 13. Decomposition / sequencing

1. **This spec** — Tactic interface + historical backtest engine + dynamic
   universe + strategy-docs layer (folds in richer scorecards).
2. Crowd-signal tactic (plugs into the Tactic interface; needs point-in-time
   crowd history).
3. Forward Monte-Carlo projection (separate engine; reuses tactics + metrics).

## 14. Risks / open items

- Profile materialization is a modeling choice; mitigated by writing the full
  tilt map into the catalog (auditable) and flagging the one approximate tactic.
- Backtests exclude costs/taxes/slippage in v1 — explicitly labeled in artifacts
  so results are not over-trusted.
- Proxy-ETF backfill adds a one-time FMP cost (free tier) the first run.
