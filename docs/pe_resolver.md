# pe_resolver (research-only)

Last verified against `portfolio_automation/research/pe_resolver.py` (added
`74bf5d77`, 2026-08-03 — unchanged since). Last updated 2026-08-08.

## Purpose

A **canonical, provenance-carrying PE resolver for research use only**. It
exists so a champion/challenger experiment (`docs/pe_challenger.md`) can ask
*"what if the intended PE component were restored?"* without touching production
behaviour.

### Why production PE is inert (measured 2026-08-03)

`scanner/candidate_scanner.py` documents two PE components — a `pe > 50` bubble
guard in `_passes_hard_filters` and a 15-point PE attractiveness factor in
`_score` — both reading `metrics['peRatio']`. That key is **never populated**:
`get_fundamentals_v3` sources fundamentals from `stable/key-metrics` and looks
for `peRatio` / `priceEarningsRatio`, and key-metrics returns **neither** — it
returns `earningsYield` instead. The v3 fallback that would have supplied
`peRatio` only runs when key-metrics returns *nothing at all*, and it returns
plenty. So `peRatio` resolved **0 of 503** and both components have been dead.

This module resolves PE correctly. It deliberately does **not** repair
`get_fundamentals_v3`, because doing so would change which candidates pass —
i.e. production scanner behaviour.

## Governance posture

**Research-only, and never wired to production.** Verified by grep on
2026-08-08: the only importers of `portfolio_automation.research.pe_resolver`
are `tests/test_pe_research_resolver.py` and (by documented contract, not by
import) `pe_challenger.build_snapshot`, which consumes result dicts a caller
supplies. There is **no** caller in `main.py`, `scripts/run_daily_safe.sh`, any
other wrapper script, or any cron entry.

- **Not wired to any scheduler.** No stage, no cadence. It runs only when a
  human or a test invokes it.
- It writes **no artifact** and declares **no `OutputNamespace`** — it returns
  dicts. There is nothing for the artifact registry to govern.
- Every single-symbol result carries `research_only: true`; every batch result
  carries `research_only: true` **and** `feeds_production_scanner: false`.
- It never feeds `decision_plan.json`, the decision engine, scoring, or the
  watchlist.

Note that it does **not** set `observe_only` — that field is the convention for
observe-only *producers* that write artifacts. This module writes nothing, so it
uses the `research_only` / `feeds_production_scanner` vocabulary instead.

## Source authority

Validated live against the FMP Starter plan, in strict preference order:

1. **DIRECT — `stable/ratios` → `priceToEarningsRatio`.** `get_ratios` is
   already an approved method in `fmp_endpoint_compliance.STABLE_METHOD_MAP`, so
   **no new endpoint is introduced**. The field is spelled
   `priceToEarningsRatio`; `fmp_client`'s docstring claims `priceEarningsRatio`,
   which does not exist in the live payload — part of why this was missed.
2. **DERIVED — `1 / earningsYield`** from `stable/key-metrics`, as a *labelled*
   fallback. `earningsYield` is a **decimal** (AAPL 0.029). The reciprocal
   reconciles tightly for profitable names (AAPL 0.04%, NVDA 0.02%, XOM 0.00%,
   KO 0.13%) but **not universally**: BA diverged **15.07%** (87.20 direct vs
   74.05 derived) and INTC 7.12%, because the two use different earnings bases.
   Derived is therefore never presented as equivalent to direct.

**Period is `annual`**, matching the basis `get_fundamentals_v3` already uses for
key-metrics and financial-growth. TTM diverges materially (NVDA 37.8 annual vs
31.5 TTM; PLTR 257.6 vs 130.9), so mixing bases would be a silent inconsistency.

## Quality vocabulary

Exactly one of five values, and it is the field a consumer must branch on:

| `quality` | `pe_ratio` | Meaning |
|---|---|---|
| `direct` | float | Read from `stable/ratios`. Usable. |
| `derived` | float | `1/earningsYield`, rounded to 6 dp. Usable, but **not** equivalent to direct — the `reason` string says so. |
| `negative_earnings` | `None` | Non-positive PE or negative earnings yield. A first-class state, **not a number**. |
| `invalid` | `None` | Outside the plausibility band, or an earnings yield too close to zero to invert. |
| `unavailable` | `None` | No client, or neither source produced a usable field. |

**`negative_earnings` is why this vocabulary exists.** A negative PE (INTC ≈
−615) *passes* a `pe > 50` guard while meaning loss-making — strictly worse than
expensive. Handing it to the guard as a plain number would invert the guard's
intent, so it is never handed back as one.

### Plausibility band

`MIN_PLAUSIBLE_PE = 0.5`, `MAX_PLAUSIBLE_PE = 10_000.0`,
`MIN_ABS_EARNINGS_YIELD = 1e-4`. The band guards two distinct unit errors:

- an `earningsYield` delivered as a **percentage** (2.9 meaning 2.9%) would
  derive PE = 0.34 — far below the floor;
- a near-zero yield (1e-9) would derive PE = 1e9 — far above the ceiling.

## Module API

```python
resolve_pe(client, symbol, *, as_of=None, period="annual", ttl_days=30) -> dict
resolve_pe_batch(client, symbols, *, as_of=None, period="annual", ttl_days=30) -> dict
```

`resolve_pe` contract:

- **Never raises.** Both client calls are individually wrapped; an exception is
  logged at DEBUG and the resolver falls through to the next source.
- **Never returns `0.0` as a stand-in for missing.** Every non-usable outcome
  returns `pe_ratio: None` with a `quality` and a human-readable `reason`.
- **Never returns a negative PE as a usable number.**
- Always returns the same key set: `symbol`, `pe_ratio`, `source`,
  `source_field`, `period`, `as_of`, `quality`, `reason`, `raw_value`,
  `research_only`.
- `_num` coercion is strict: `bool` is explicitly rejected — it is never a ratio.

`resolve_pe_batch` returns `by_symbol` (symbol → result dict) plus a `summary`
of `eligible`, the five quality counts, `usable`, and `coverage`.

**`coverage` counts only USABLE PEs** (`direct + derived`) over the eligible set.
`negative_earnings` / `invalid` / `unavailable` are explicitly not usable and are
never folded in, so coverage can never be inflated by rows that merely returned
*something*.

Live research coverage over 503 eligible symbols (2026-08-03): **471 direct · 0
derived · 30 negative_earnings · 2 unavailable → 93.6% usable.**

`REQUIRED_CLIENT_METHODS = ("get_ratios", "get_key_metrics")` is a pinned
constant: a test asserts both are present in
`fmp_endpoint_compliance.STABLE_METHOD_MAP`, so the resolver cannot silently
start needing a new endpoint.

## Failure / degraded behavior

| Condition | Result |
|---|---|
| `client is None` | `unavailable`, `reason: "no client supplied"` |
| `get_ratios` raises | Logged at DEBUG, falls through to derived |
| `get_ratios` returns a list | First dict element is used; an empty/non-dict list is treated as absent |
| Direct PE ≤ 0 | `negative_earnings`, `raw_value` preserved |
| Direct PE outside the band | `invalid`, `raw_value` preserved |
| `get_key_metrics` raises | Logged at DEBUG, falls through to `unavailable` |
| `earningsYield` < 0 | `negative_earnings` |
| `abs(earningsYield)` < 1e-4 | `invalid` — "not invertible" |
| Derived PE outside the band | `invalid` — "likely a percentage-vs-decimal unit error" |
| Neither source | `unavailable` |

Malformed or non-numeric values are coerced to `None` by `_num` and follow the
absent path, never the zero path.

## Known limitations

- **Derived is not equivalent to direct.** Up to ~15% divergence observed (BA).
  A consumer that treats `derived` as interchangeable with `direct` is misusing
  it; the `source` and `reason` fields exist to prevent that.
- **No caching layer of its own.** `ttl_days=30` is passed straight through to
  the client; the resolver has no memoization, so a batch over N symbols issues
  up to 2N client calls before cache effects.
- **`as_of` is a pass-through label**, not a point-in-time query. It is stamped
  onto results for lineage; the client still returns whatever the current
  `period="annual"` row is. This is **not** a historical PE resolver.
- **The production inertness it documents is still unfixed by design.** Nothing
  here repairs `get_fundamentals_v3`; the guard remains non-binding in
  production, and that is recorded via `inert_fields` /
  `scanner.factor_liveness` (see `docs/scanner_canary.md`) so it cannot be
  forgotten.

## Tests

```
.venv/bin/python -m pytest -q tests/test_pe_research_resolver.py
# 16 passed
```

Covers: direct resolution from `priceToEarningsRatio`; direct preferred over
derived; negative direct PE flagged `negative_earnings` rather than returned as a
number; derived provenance; negative / zero / near-zero earnings yield; the
percentage-scaled unit-error catch; no-source → `unavailable` (not zero);
malformed values never becoming `pe_ratio: 0`; client exceptions degrading
without raising; `client=None`; the full result key set; batch coverage
arithmetic; the `research_only` / `feeds_production_scanner: false` declaration;
and that `REQUIRED_CLIENT_METHODS` are all already-approved `STABLE_METHOD_MAP`
entries.

## See also

- `docs/pe_challenger.md` — the champion/challenger experiment that consumes
  these results.
- `docs/DATA_AND_FMP_ENDPOINTS.md` → "PE source authority (research only)" and
  "Known inert guard" — the same findings in the endpoint-authority doc.
- `docs/scanner_canary.md` — where factor inertness surfaces operationally.
