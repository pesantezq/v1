# P&L Maximization Roadmap — 2026-05-15

This roadmap proposes additive observe-only layers designed to lift realized
returns on the existing advisory-only portfolio system without changing
protected scoring or decision semantics.

It was written after a survey of the codebase by a quant/architect/operator
read. Each entry is classified as **ADDITIVE** (can be built and shipped
under existing CLAUDE.md rules) or **PROTECTED** (requires explicit user
approval before scope is unlocked, because it touches decision_engine.py,
scoring.py, or conviction.py semantics).

## Working principles

1. **Money is made and lost on the gaps the current system does not see.**
   Entry signals are well-covered (theme/news/technical/fundamental composite,
   conviction band, allocation engine). The neglected surfaces are: when to
   exit, how to size by realized hit-rate, hidden correlation, tax timing,
   cash-deployment timing, and event-window risk.
2. **Each new layer is additive, observe-only, namespace-aware, and reads
   existing artifacts.** No layer mutates a protected score or writes outside
   its declared namespace.
3. **All new artifacts hardcode `observe_only: true` and are wrapped in
   try/except at the pipeline integration point** so a failure cannot stall
   the daily pipeline.
4. **Every new module gets tests in `tests/`** before integration.

## Hard boundaries (preserved)

- `signal_score`, `confidence_score`, `effective_score`, `conviction_score`,
  `final_rank_score`, `recommendation_score`: semantics unchanged.
- `portfolio_automation/decision_engine.py`: not modified.
- `scoring.py`, `conviction.py`, `allocation_engine.py`: not modified by
  additive layers.
- FMP registry/compliance: not bypassed; any new FMP endpoint goes through
  the existing `FMPClient` interface.
- No broker integration, no auto-execution, no trade placement.

## Identified gaps that cost realized P&L

| # | Gap | Why it bleeds money | Current state |
|---|---|---|---|
| G1 | No portfolio-level trailing-stop / time-stop advisor | Winners give back gains; losers overstay | `exit_engine.py` is single-position trigger-based, not equity-curve aware |
| G2 | No tax-aware harvesting layer | Taxable account leaves harvestable losses on the table | `is_taxable_account: true` + `avoid_taxable_sales: true` set, but no harvest logic |
| G3 | No cash-deployment plan | $1000/month contributions accrue idle; deployment is ad hoc | `monthly_contribution: 1000`, `has_regular_contributions: true` are set but no advisor |
| G4 | No correlation/covariance advisor | QQQ + QLD + NASA + tech names are NDX-correlated; sector_cap masks this | sector_cap exists; no per-pair correlation surface |
| G5 | No earnings calendar gating | Position-into-earnings risk is concentrated and asymmetric | No earnings layer exists |
| G6 | No Kelly-fractional sizing recommendation | Sizing multipliers are fixed (0.25/0.50/1.00) regardless of realized hit-rate | conviction.py uses static multipliers |
| G7 | No volatility regime advisor | Sizing doesn't adapt to VIX/realized-vol regime | `drawdown_regime` only |
| G8 | No factor exposure surface | Hidden growth/momentum factor tilt is invisible | No factor decomposition |
| G9 | No multi-timeframe trend confirmation | 1d + 5d only; misses weekly/monthly trend reversals | scanner.py 1d/5d only |
| G10 | No realized P&L attribution by alpha source | Can't tell whether structural/portfolio/finance/watchlist/market signals actually earn | `decision_performance_attribution.py` exists but reads `decision_outcomes.jsonl` only |

## Roadmap

### Phase 1 — Highest leverage, ship now (ADDITIVE, executes immediately)

These three modules are unblocked, have clean read-only inputs, fit the
existing namespace + observe-only pattern, and address the largest expected
P&L gaps for a single-operator taxable account with regular contributions.

#### P1.1 — Exit Advisor Layer  (`portfolio_automation/exit_advisor.py`)

**Why this is first:** Reduces giveback on winners and tail risk on losers.
Operates on positions, not entry signals — pure protection-of-realized-PnL.

**Inputs (read-only):**
- `outputs/latest/portfolio_snapshot.json` (current holdings, cost basis,
  unrealized P&L proxy)
- `outputs/latest/watchlist_signals.json` (current technicals per held symbol)
- `outputs/latest/decision_plan.json` (existing decisions to avoid contradiction)

**Outputs (LATEST namespace):**
- `outputs/latest/exit_advisor.json`
- `outputs/latest/exit_advisor.md`

**Logic (deterministic, no LLM):**
- For each position: compute trailing peak from price history, drawdown
  from peak, and time-in-position (using state_store entry-date if
  available, else fall back to "unknown").
- Emit advisory `EXIT_FULL`, `EXIT_HALF`, `TIGHTEN_STOP`, `HOLD` per
  position based on:
  - drawdown-from-peak > strategy-specific threshold (compounder 15%,
    momentum 8%)
  - time-stop: momentum positions >120 days without new high → review
  - signal decay: signal_score now < entry signal_score - 0.20
- Never produces a SELL — only advisory `recommendation` strings.
- Honors `observe_only: true` hardcoded in artifact.

**Test coverage target:** ≥15 tests covering trailing-stop math, time-stop,
signal-decay, missing-data graceful degradation, and namespace policy.

#### P1.2 — Cash Deployment Plan  (`portfolio_automation/cash_deployment_plan.py`)

**Why second:** $1,000/month contribution is a deterministic capital inflow
that the system currently does not allocate. Idle cash is a known drag.

**Inputs (read-only):**
- `config.json` (monthly_contribution, target_cash_weight)
- `outputs/latest/portfolio_snapshot.json` (current cash %)
- `outputs/latest/decision_plan.json` (ranked BUY/SCALE decisions)
- `outputs/latest/watchlist_signals.json` (conviction bands)

**Outputs (LATEST namespace):**
- `outputs/latest/cash_deployment_plan.json`
- `outputs/latest/cash_deployment_plan.md`

**Logic (deterministic):**
- Compute `excess_cash = current_cash% - target_cash_weight%`.
- Add `incoming_contribution_30d = monthly_contribution × 1`.
- Available deployable = excess + incoming - safety floor (5%).
- Distribute available across top-N BUY/SCALE decisions from decision_plan,
  respecting per-position max (allocation_engine `max_position_cap`) and
  conviction-band sizing multiplier.
- Output a ranked deployment schedule per position, **labeled advisory only**.
- Skip when degraded_mode or low_confidence to prevent bad-data deployment.

**Test coverage target:** ≥12 tests covering excess-cash math, safety
floor, conviction-band sizing pass-through, degraded-mode safety, namespace
policy.

#### P1.3 — Correlation Risk Advisor  (`portfolio_automation/correlation_risk_advisor.py`)

**Why third:** The current portfolio (QQQ 35%, QLD 5%, NASA 10% — implicit
tech, NDX-correlated) has hidden concentration that sector_cap does not
catch. A single rolling-correlation matrix surfaces this.

**Inputs (read-only):**
- `outputs/latest/portfolio_snapshot.json` (holdings with weights)
- FMP historical prices via `FMPClient.get_historical_prices()` (cached;
  no new endpoints; existing TTL respected)

**Outputs (LATEST namespace):**
- `outputs/latest/correlation_risk_advisor.json`
- `outputs/latest/correlation_risk_advisor.md`

**Logic (deterministic):**
- 90-day daily return correlation across all current holdings.
- Compute weighted effective number of independent bets:
  `1 / sum(w_i × w_j × corr_ij)`.
- Flag any pair with `|corr| > 0.85 and combined weight > 25%`.
- Flag overall concentration when effective bets < 4.
- Pure observation — does not adjust any score or allocation.

**Test coverage target:** ≥10 tests covering correlation math on synthetic
series, effective-bet calc, missing-data degradation, namespace policy.

### Phase 2 — Next wave (ADDITIVE, ship after Phase 1 lands)

| # | Module | Inputs | Outputs |
|---|---|---|---|
| P2.1 | Earnings Calendar Gate (`earnings_gate.py`) | Holdings + FMP earnings-calendar endpoint | `outputs/latest/earnings_gate.json` — positions within X days of earnings, suggested hold-cap |
| P2.2 | Volatility Regime Advisor (`vol_regime_advisor.py`) | VIX proxy via SPY 20-day realized vol + ATR per symbol | `outputs/latest/vol_regime_advisor.json` — risk-on/neutral/risk-off label and suggested aggregate sizing multiplier (advisory only) |
| P2.3 | Tax-Loss Harvest Advisor (`tax_harvest_advisor.py`) | Holdings + cost basis + 30-day price history + wash-sale window | `outputs/latest/tax_harvest_advisor.json` — symbols with harvestable losses + replacement candidate (e.g. QQQ↔VGT) for tax-deferred basis reset |

Why later: P2.1/P2.2 are additive but lower frequency-of-impact. P2.3 needs
cost-basis tracking depth this system does not yet store reliably; lot-level
tracking should be confirmed before harvesting math runs.

### Phase 3 — Calibration-gated (ADDITIVE, ship when ≥20 resolved decisions)

| # | Module | Gate |
|---|---|---|
| P3.1 | Kelly Sizing Advisor (`kelly_sizing_advisor.py`) | Needs `decision_outcome_summary.json` with ≥20 resolved decisions; reads per-signal hit-rate + avg return; outputs fractional-Kelly sizing recommendation per band — observe-only, never auto-applied. |
| P3.2 | Per-alpha-source Sharpe + IR (`alpha_attribution_report.py`) | Same gate. Extends `decision_performance_attribution.py` with Sharpe and information ratio per source bucket so the operator can see which alpha source actually earns. |

### Phase 4 — Protected-semantics (REQUIRES USER APPROVAL before any scope)

These modules deliver meaningful P&L but cannot be touched under current
CLAUDE.md rules without explicit scope unlock. Listed for visibility only.

| # | Change | What it would do |
|---|---|---|
| P4.1 | Sizing multipliers in `conviction.py` become outcome-driven | Replace static 0.25/0.50/1.00 with calibrated Kelly fraction × confidence floor — needs explicit approval because it changes sizing semantics |
| P4.2 | Decision Engine integrates exit_advisor as a downgrade source | Allow trailing-stop signal to map to a `TRIM` decision — needs approval because it changes decision_engine.py decision derivation |
| P4.3 | Multi-timeframe confirmation in `scanner.py` | Add weekly + monthly trend gate to signal_score composite — changes signal_score semantics |
| P4.4 | Regime-aware allocation in `allocation_engine.py` | Vol-regime advisor feeds back into sizing — changes allocation behavior |

Phase 4 is documented so the user knows the path exists; nothing in Phase 4
ships without explicit scope approval.

## Build sequence

Phase 1 (this session): P1.1 → P1.2 → P1.3, each with tests, py_compile,
targeted pytest before moving on. Final-report block per CLAUDE.md.

Phase 2 / Phase 3: separate sessions, separate plans, separate approvals.

Phase 4: spec discussion before any code change.

## Success metrics (proposed; not measured by this roadmap)

- Reduced winner giveback: median exit price vs trailing peak.
- Reduced idle cash drag: cash % closer to `target_cash_weight`.
- Improved diversification: effective independent bets ≥ 4.
- (Phase 3) Calibrated sizing: per-band hit-rate within ±0.05 of advisory
  confidence band.

## Out of scope (explicit)

- Auto-trading, broker integration, order placement.
- Modifying any of the six protected score fields.
- Modifying `decision_engine.py`, `scoring.py`, `conviction.py`,
  `allocation_engine.py`.
- Writing to namespaces outside each module's declared purpose.
- Replacing existing recommendation outputs.

## Final notes

Phase 1 is the only phase being executed in this session.
