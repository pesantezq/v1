# VS-002 — designed, and deliberately not run

**Verdict: BLOCKED. No credible risk adjustment is computable from data durable in this repository today.**

VS-001 left one question open: was its +1.881 pp excess over SPY *skill*, or just beta? VS-002 was built to settle that. It cannot be settled here, and the honest output of this mission is the proof of why, plus the exact data that would unblock it.

---

## The four blockers, each measured

`portfolio_automation/vertical_slice/feasibility.py` recomputes these on every test run. They are not assertions in prose.

| # | Blocker | Measured | Threshold |
|---|---|---|---|
| 1 | Independent observations | **2** non-overlapping 7-day cohorts | ≥ 10 |
| 2 | Risk-estimation inputs | **10–13** usable joint return observations per ticker | ≥ 15 (`vol_regime_advisor`), ≥ 30 (`correlation_risk_advisor`) |
| 3 | Durable price history | `outputs/backtest/historical` **does not exist** | required for any pre-signal window |
| 4 | Interval comparability | gaps **0.02 h → 31.39 h**, 9 of 32 under two hours | identically scaled returns |

Thresholds 1 and 2 are not invented for this argument — 15 and 30 are this codebase's own `_MIN_OBSERVATIONS` gates.

## A correction to the VS-001 report

VS-002 reconnaissance measured something VS-001 described wrongly.

The VS-001 report said "one rising **20-day** window" and estimated the effective sample as "nearer 3 than 20". The **scored** population — the 440 rows with matured 7-day outcomes — spans **9.52 days across 11 calendar dates**, ending 2026-05-22. Twenty days is the span of the *file*, not the evidence.

Worse: four scans on 2026-05-12 fall within **31.7 minutes** of each other and share one identical outcome window; five more cluster on 2026-05-18. VS-001's twenty "clusters" are roughly **1.4 independent weeks** of market history.

**VS-001's artifacts and verdict are unchanged.** Retrofitting them is forbidden and would be dishonest. The correction lives in `DIJ-0007` and here. VS-001 passed 22 tests and full CI while carrying this understatement, because no test asserted anything about sample independence — the machinery was right and the description was not.

## What VS-002 would have been

Fully designed in `evals/vertical_slice/VS-002_blocked.json`, so the requirement is provable rather than hand-waved.

**Question:** Do the signals predict positive *risk-adjusted* 7-day excess returns, and does signal strength positively *rank* risk-adjusted outcomes, under a sampling design that does not treat overlapping horizons as independent?

**H1 (portfolio skill)** and **H2 (ranking skill)** judged separately and never collapsed — VS-001 met a portfolio criterion with IC = −0.059, exactly the case one blended claim hides.

**Risk model:** historical beta-adjusted excess, `r − [rf + β(r_SPY − rf)]`, β by OLS over 252 sessions ending strictly before `signal_time`, minimum 60 observations.

**Rejected, each with a reason:**

- *stock − SPY* (VS-001's method) — not a risk adjustment at all
- *sector-matched* — `sector_mapping.py` covers 3 of 22 tickers; the 19 single names need `data/fmp_cache/profile_stable_<SYM>.json`, and that cache is empty
- *volatility-matched* — needs ≥ 15 observations; 10–13 available, fewer after reserving a PIT window
- *Fama-French attribution* — `factor_attribution.py` exists and is tested, but requires **monthly** returns gated at ≥ 9 months against a 9.5-**day** window, and `data/factors/ff_monthly.csv` is absent
- *beta from `price_at_signal`* — the closest to computable, and refused. 4–6 point-in-time observations would yield a standard error wider than the effect. **A falsely precise number would be less honest than the raw figure it was meant to correct.**

**Acceptance vocabulary**, replacing VS-001's ambiguous `SUPPORTED`:
`PREREGISTERED_CRITERIA_MET` / `NOT_MET` / `EVIDENCE_INCONCLUSIVE`, and separately `ECONOMIC_SKILL_EVIDENCE_PRESENT` / `NOT_ESTABLISHED`. Skill evidence requires **H1 AND H2 AND a beat over NO_ACTION AND ≥ 10 independent cohorts**.

**Friction:** 10 bps round trip, carried from VS-001 — not because it was convenient, but because the instruments are unchanged and 5 bps per side is conservative for large-cap US equities. It is an **assumption, never a measurement**; the repository contains no measured execution cost anywhere.

## Minimum evidence contract to unblock

Full specification in the artifact. Essentials:

**Per bar:** `symbol`, `session_date`, `close`, **`adj_close`** (required — a split inside a 252-day β window silently destroys the estimate), `volume`, `known_at`, `known_at_basis` (`unknown` refused), `source_id`.

**Coverage:** 22 watchlist tickers **+ SPY**; ≥ 252 sessions before the earliest signal; forward sessions covering each `signal_time + 7d` so numerator and risk adjustment share one calendar.

**PIT:** admitted through the existing `EvidenceGateway`. No synthesis, no forward-fill across gaps, no back-dating.

**Storage:** durable, queryable by `(symbol, session_date)` and by `as_of`. **This is the first genuine demand for the 0C Research Store** — VS-001 held 420 snapshots in memory; this needs ~5,800 bars per experiment and must not be re-fetched per run.

**Validation:** tz-aware bars, no gap > 5 sessions, `adj_close/close` monotone within an action-free span, ≥ 252 sessions asserted not assumed, and a lookahead refusal ledger in the VS-001 style.

Out of scope: intraday bars, options, fundamentals, news, a vendor abstraction layer, paid data.

## 0C scope, decided by evidence

| Capability | Verdict |
|---|---|
| Historical daily adjusted closes + `known_at` | **REQUIRED_NOW** — the binding constraint on every risk-adjusted experiment |
| Research Store persistence + `(symbol, date)` / `as_of` query | **REQUIRED_FOR_NEXT_CREDIBLE_EXPERIMENT** — ~5,800 bars/run |
| Corporate actions | **REQUIRED_NOW** — promoted from VS-001's deferral, because a 252-day β window spans splits |
| Risk-free series | **REQUIRED_FOR_NEXT_CREDIBLE_EXPERIMENT** — `config.json`'s 4% constant is not a PIT rate |
| IC standard error | **REQUIRED_FOR_NEXT_CREDIBLE_EXPERIMENT** — `strat_lab_adapter.py` gates promotion on bare `ic >= 0.05` with no error bar |
| Sector map for 19 single names | **USEFUL_LATER** — only if sector-matched benchmarking is chosen over beta |
| Revision/supersession winner policy | **NOT_JUSTIFIED** — daily closes are not restated |
| Intraday bars, options, fundamentals, news | **NOT_JUSTIFIED** |

## Subagent maturity — VS-001 + VS-002

**5 tasks, 0 file writes, 0 scope violations, 0 authority violations.**

Useful findings: 5 of 5. Load-bearing errors: **0 reached a result** — 3 caught before use (two API-shape errors in VS-001, one cohort-direction discrepancy here), all by controller re-verification or the interpreter. VS-002-RECON-B produced the mission's single most valuable finding: the correction to VS-001's stated window.

**Recommendation: do not grant write access yet.** Five read-only tasks with a perfect scope record is encouraging and is not evidence about *write* behaviour — no observation here bears on what happens when an agent can modify state. The errors that did occur were caught precisely because every claim was recomputed before use, a check that does not survive contact with a writing agent. Revisit after the 0C price-data task, where read-only recon can run against a real adapter.

## An uncomfortable pattern worth naming

`DIJ-0005` recorded that grepping source text for scary words is a brittle control. In this mission the same mistake recurred **three more times in one file** — a substring ban on `outcome_return` that fired on a legitimate column read, a case-mismatched string assertion, and a field-name ban on `ic` that matched `pr**ic**e_points_per_ticker`.

The test suite caught all three. The journal entry prevented none of them. That is the clearest evidence so far that **a journal is evidence, not a control** — and an argument that deterministic controls beat remembered advice. Recorded as `DIJ-0009`, not fixed: a lint rule or shared assertion helper is outside this mission.
