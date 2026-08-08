# Intraday Strategy Lab — Data Foundation (Session 1)

**Status: `REAL_DATA_READY`** (5-minute bars). Research-only. No production path.

This document is the **foundation contract**. Later Intraday Lab sessions must
follow it; where a later session needs to deviate, it changes this document
first and says why.

---

## 1. Source findings

Probed 2026-08-08, read-only, against the **configured account** — every value
below was measured, not read off provider documentation. Entitlement and
documentation disagree routinely, which is exactly why 1min was probed.

| Field | Result |
|---|---|
| Provider | FMP |
| Endpoint | `/stable/historical-chart/{timeframe}` |
| Registry status | **REGISTERED** as `intraday_chart` (`fmp_endpoint_registry.py`), `required_daily: False` |
| Account access | **5min ENTITLED** · **1min NOT ENTITLED (HTTP 402 Payment Required)** |
| Tested symbols | SPY, AAPL |
| Historical depth | ≥ **2017-08-07** verified; full sessions at 2017 / 2020 / 2023 / 2025 / 2026 |
| Bars per full session | **78** (09:30 → 15:55) |
| OHLC / Volume | Complete |
| Timestamp semantics | **`BAR_OPEN`** |
| Timezone | naive **US/Eastern wall-clock**, no offset supplied |
| Session coverage | **REGULAR_ONLY** — no extended hours |
| Adjustment | **SPLIT BACK-ADJUSTED** |
| Final source status | **`REAL_DATA_READY`** |

**Timestamp evidence (not assumed).** 2025-11-28 — the day after Thanksgiving,
a 13:00 early close — returned **42 bars**, last `12:55`. A normal session runs
`09:30…15:55`. Both are only consistent with **bar-open** labelling, and they
also prove the provider reflects real early closes rather than padding sessions.

**Adjustment evidence.** AAPL on 2020-08-27 (before the 4:1 split) closes ≈ `125`.
The price that actually printed that day was ≈ `500`. The history is therefore
back-adjusted.

---

## 2. Intraday bar contract

`portfolio_automation/intraday_lab/models.py::IntradayBar` — frozen. A bar is an
observation; a correction is a *new* bar with a new fingerprint, never an
in-place edit.

```
symbol · timeframe · bar_start_at (UTC, aware)
open · high · low · close · volume
source · source_endpoint · retrieved_at
adjustment_state · quality_flags
```

Derived: `bar_end_at = bar_start_at + timeframe`, `event_at = bar_start_at`,
`known_at` (below).

Validation refuses `high < low`, open/close outside `[low, high]`, negative
volume, non-finite values, non-positive prices, and unsupported timeframes.

**`1min` is deliberately absent from `TIMEFRAMES`.** Declaring a timeframe this
account cannot serve would let a later session build research on data that
returns 402.

---

## 3. Temporal semantics

Five times, none interchangeable:

| Concept | Meaning |
|---|---|
| `bar_start_at` | The interval opens. This is FMP's `date` field. |
| `bar_end_at` | The interval closes. The bar is complete. |
| `event_at` | When the underlying thing happened (for a bar, `bar_start_at`). |
| `known_at` | First instant a decision-maker could have **acted** on it. |
| `retrieved_at` | When *we* fetched it. Bookkeeping only. |

### Invariants

```
known_at >= bar_end_at          a bar is not knowable before it closes
known_at <= decision_time       an input must be knowable to be used
known_at != retrieved_at        retrieval time is not knowledge time
```

The third is the one most easily got wrong, and it fails in both directions:
treating `retrieved_at` as `known_at` makes every historical bar look
unknowable and silently empties the backtest; treating `bar_start_at` as
`known_at` lets a strategy trade a bar before it finished forming.

`known_at = bar_end_at + 60s` (`DEFAULT_PUBLICATION_DELAY`). FMP publishes no
emission timestamp, so this is a **documented conservative floor**, not a
measurement. If real latency is ever measured it may only move *later*.

---

## 4. Point-in-time enforcement

`validation.admissible_inputs(inputs, decision_time)` is **generic**, not
bar-specific — the inputs most likely to leak are news, sentiment, analyst
revisions and regime labels, not prices. Anything exposing `is_known_at` works;
anything **without** it is **rejected**, because an input that cannot state when
it became knowable cannot be proven leak-free, and defaulting to "allow" is how
leakage enters.

`validation.earliest_order_time(bar)` fixes the boundary the (not-yet-built)
simulator must respect: a signal from the 10:00–10:05 bar cannot be filled
before that bar was knowable. A future simulator granting an earlier fill
produces an **INVALID** result regardless of profitability.

---

## 5. Leakage defenses and their tests

| Defense | Test |
|---|---|
| Future bar excluded | `test_future_bar_is_excluded_from_admissible_inputs` |
| Incomplete bar unusable mid-interval | `test_incomplete_bar_is_not_knowable_mid_interval` |
| Future feature rejected | `test_future_feature_is_rejected` |
| EOD aggregate unavailable to a morning decision | `test_eod_aggregate_is_not_available_to_a_morning_decision` |
| Hindsight regime label rejected | `test_hindsight_regime_label_is_not_intraday_eligible` |
| `retrieved_at != known_at` | `test_known_at_is_not_retrieved_at` |
| Untimestamped input refused | `test_input_without_known_at_is_refused_not_assumed_safe` |
| Knowable-before-it-happened refused | `test_feature_known_before_it_happened_is_rejected` |
| Timezone instant preserved | `test_eastern_to_utc_preserves_the_instant` |
| DST via calendar, not fixed offset | `test_dst_boundary_uses_the_calendar_not_a_fixed_offset` |
| Fill boundary | `test_earliest_order_time_forbids_a_fill_before_the_bar_was_known` |

---

## 6. Data quality rules

- **Ordering** — out-of-order input is **sorted**, not rejected (providers
  legitimately return newest-first).
- **Duplicates** — an exact repeat is collapsed; a *conflicting* duplicate
  **raises**, because keeping either would make the fingerprint depend on
  arrival order.
- **Coverage** — `profile_session` takes `expected_bars` from the caller rather
  than inferring it; inferring from observed data would make a truncated
  session look complete.
- **Gaps** — `MARKET_CLOSED` / `EARLY_CLOSE` / `MISSING_BAR` / `PROVIDER_GAP` /
  `UNKNOWN_GAP`. **`HALT` is deliberately not in the automatic vocabulary** —
  OHLCV alone cannot distinguish a halt from a provider gap, and naming it
  would manufacture evidence.
- **Zero volume** — counted as a quality condition, never a crash.

---

## 7. Dataset fingerprinting

`dataset_fingerprint` hashes identity + OHLCV only, and **excludes
`retrieved_at`** — otherwise re-fetching identical history would produce a new
fingerprint and every experiment would be irreproducible by construction.

```
PROVIDER → RAW → VALIDATION → CANONICAL DATASET → FINGERPRINT → EXPERIMENT
```

No bulk backfill was performed this session.

---

## 8. Signal-family point-in-time assessment

Conservative by default: uncertain never means `PIT_READY`.

| Family | Status | Reason |
|---|---|---|
| Price-derived | `PIT_READY` | Bar times are explicit and knowability is derivable |
| News | `PIT_POSSIBLE_WITH_WORK` | Publication timestamps exist but ingestion-vs-publication needs proving |
| FinBERT | `PIT_POSSIBLE_WITH_WORK` | Inherits the news timestamp; scoring time is separate |
| Crowd / attention | `PIT_UNSAFE` | Current aggregates can contain within-session future observations |
| Analyst | `PIT_POSSIBLE_WITH_WORK` | Revisions are restated; original publication time must be preserved |
| Insider | `PIT_POSSIBLE_WITH_WORK` | Filing time ≠ transaction time; filing time is the knowable one |
| Congress | `PIT_POSSIBLE_WITH_WORK` | Long, variable disclosure lag |
| Regime | `PIT_UNSAFE` | Daily labels are hindsight relative to an intraday decision |

None are integrated. Session 2+ must justify any promotion with evidence.

---

## 9. Limitations

1. **1min not entitled** (HTTP 402). 5min is the finest available timeframe.
2. **Split back-adjustment is retroactive.** Safe for return-based research;
   **not** point-in-time safe for any rule keyed to absolute price levels or
   round-number thresholds.
3. **Dividend adjustment behaviour was not established** for intraday bars.
4. **Depth was spot-probed** at five points, not exhaustively scanned.
5. **`known_at` uses a 60s floor**, not measured provider latency.
6. **REGULAR_ONLY** — gap/pre-market research is out of scope without another
   sanctioned source.
7. One probed window (2026-05-05..09) was missing a session — real coverage
   gaps exist and must be profiled per window, not assumed away.

---

## 10. Session 2 preconditions

Session 2 **may** assume: the bar contract, UTC normalization, the temporal
model, `admissible_inputs`, `earliest_order_time`, canonicalization,
`profile_session`, `dataset_fingerprint`, and that 5-minute FMP history back to
2017 is obtainable and compliant.

Session 2 **must not** assume: 1min data, extended-hours data, dividend
adjustment semantics, any non-price feature being PIT-safe, or that any dataset
has been backfilled — none has.

Session 2 scope is the execution simulator, cost model, risk model and trade
ledger. Every fill it produces must satisfy `order_eligible_at >= earliest_order_time(bar)`.

---

## 11. Artifacts

| Path | Namespace | Consumer |
|---|---|---|
| `outputs/backtest/intraday_provider_assessment.json` | HISTORICAL | this doc; Session 2 go/no-go |
| `outputs/backtest/intraday_foundation_status.json` | HISTORICAL | `assess_foundation_health` |
| `outputs/backtest/intraday_foundation_health.json` | HISTORICAL | health review |

Health distinguishes `SYSTEM_FAILURE` from `SOURCE_UNAVAILABLE` /
`SOURCE_LIMITED` / `HEALTHY`: an unentitled account is a **correct diagnosis**,
not a software crash. Current: `HEALTHY`.
