# Intraday Strategy Lab — Session 3.0

**`SESSION_3_0_POLICY_READY`** · **`SESSION_3_1_GO`** · research-only ·
`strategy_validation_allowed = false`

Session 2 answered *"is this an exact, trustworthy continuous-session dataset?"*
Session 3.0 answers a different question:

> **Which real market sessions belong in the population our future strategy
> claims to survive?**

No strategy, signal, entry, exit, fill, cost or P&L exists in this session.

---

## 1. The problem Session 2 correctly created

Session 2 admits a session only when observed bar-starts EQUAL the calendar
grid. A market-wide halt produces intervals with no trades, so a legitimately
halted session has missing nominal bars and is rejected as
`REJECTED_MISSING_BARS`.

That is right for data integrity and wrong for population coverage: the sessions
most likely to be halted are the most violent ones, so a strategy validated on
the survivors has been tested disproportionately on uninterrupted markets.

Session 2 is **not** relaxed. Session 3 builds a separate derived view.

---

## 2. Authoritative MWCB registry

`INTRADAY_MWCB_REGISTRY_V1` — verified against the **primary document**, not a
summary, blog or the shape of the data:

| | |
|---|---|
| Authority | MWCB Working Group (NYSE, Nasdaq, Cboe, FINRA, SEC, CFTC, DTCC, OCC) |
| Title | *Report of the Market-Wide Circuit Breaker ("MWCB") Working Group Regarding the March 2020 MWCB Events* |
| Identifier | SEC Release No. 34-92428, Exhibit 3 |
| Published | 2021-03-31 |
| Evidence | pp. 4–5 trigger/reopen times; p. 11 contemporaneous Cboe + NYSE notices stating ET explicitly |

| Date | Halt (ET) | Reopen (ET) | Level |
|---|---|---|---|
| 2020-03-09 | 09:34:13 | 09:49:13 | 1 |
| 2020-03-12 | 09:35:44 | 09:50:44 | 1 |
| 2020-03-16 | 09:30:01 | 09:45:01 | 1 |
| 2020-03-18 | 12:56:17 | 13:11:17 | 1 |

Registry identity covers every event date, instant, level, scope and the
claiming authority — so a later source correction mints a **different research
object** rather than silently reinterpreting published results. Retrieval facts
are excluded: when we looked is not what the registry means.

---

## 3. Classification contract

| State | Rule |
|---|---|
| `VALID_CONTINUOUS_SESSION` | Session 2 `ADMITTED` |
| `VALID_MARKET_WIDE_HALT_SESSION` | Session 2 `REJECTED_MISSING_BARS`, a market-wide registry event exists for the date, **and every** missing nominal bar lies entirely inside the halt |
| `REJECTED_UNEXPLAINED_GAP` | missing bars with no event, wrong scope, partial overlap, or any residual unexplained absence |
| `REJECTED_SOURCE_ERROR` | `REJECTED_PROVIDER_ERROR` / `REJECTED_NORMALIZATION_ERROR` — we could not obtain or interpret data, which says nothing about market structure |
| `REJECTED_OTHER_DATA_DEFECT` | any other Session 2 rejection; **a halt never repairs a defect** |
| `NOT_A_TRADING_SESSION` | calendar says closed |

### Exact containment (never rounded)

    bar_start >= halt_start  AND  bar_end <= reopen_start

For 2020-03-09 (09:34:13 → 09:49:13): `09:35` and `09:40` are explained;
`09:30` and `09:45` **partially overlap** and are not. A 15-minute halt
therefore explains only **10 minutes** of absence — rounding the trigger down to
09:30 would silently admit an interval that contained tradable time.

### Inference is never authority

Missing bars are **never** evidence of a halt by themselves, including when
identical across symbols. Only a registry event explains an absence.

---

## 4. Real 2020 proof

All eight halt symbol-sessions classify as `VALID_MARKET_WIDE_HALT_SESSION`
(2 explained absences, 0 unexplained, 76 observed bars of 78 nominal), and
**2020-03-17 — one of the most volatile sessions in history — remains
`VALID_CONTINUOUS_SESSION`**. Volatility ≠ halt.

---

## 5. No synthetic bars

`bars added = 0 · interpolated = 0 · forward-filled = 0`, enforced by an AST
test asserting that Session 3 modules call no fill/resample operation and
construct no bars at all. A halted interval is **absence plus an authoritative
event**; the discontinuity is the information.

`bar_start_at`, `bar_end_at` and `known_at` are carried through byte-identically.

---

## 6. Feature segmentation — stated accurately

The frozen Session 2 engine **already** refuses to bridge a halt:
`features._contiguous` checks adjacency in *time*, so a rolling window spanning
the gap returns explicit absence. **Session 3 did not close a leak here.**

`segment_bars` / `segmented_features` earn their place by making the segment
structure explicit (Session 3.1 needs it), expressing the invariant
independently, and keeping the reopening discontinuity out of within-segment
realized volatility — a session-level computation Session 2 never performs.

---

## 7. Population audit

Two different epistemic statuses, deliberately not blended.

**Exact** (no provider calls — the registry is complete for the window):

| | |
|---|---|
| Certified trading dates 2017-01-01…2026-08-07 | 2,412 |
| Certified symbol-sessions (SPY, AAPL) | 4,824 |
| MWCB symbol-sessions | 8 (**0.166%**) |

**Sampled** — a deterministic stratified sample, **not a census**. FMP's
`/stable/historical-chart/5min` returns at most ~432 rows (~6 sessions) for
*any* window, always the tail (measured: year, quarter and month requests
returned the identical 6 days). A census of two symbols over 2017–2026 needs
~800 calls against a 40-call registered budget, so **unexplained-gap prevalence
is not estimated** and must not be inferred.

| state | n | % of sample |
|---|---:|---:|
| `VALID_CONTINUOUS_SESSION` | 108 | 93.10% |
| `VALID_MARKET_WIDE_HALT_SESSION` | 8 | 6.90% |
| `REJECTED_UNEXPLAINED_GAP` | 0 | 0% |
| `REJECTED_SOURCE_ERROR` | 0 | 0% |
| `REJECTED_OTHER_DATA_DEFECT` | 0 | 0% |

116 requested = 116 accounted. Nothing dropped.

---

## 8. Selection bias

Halt sessions' percentile rank within the sampled continuous distribution:

| metric | continuous median | halt percentile ranks |
|---|---:|---|
| intraday range % | 1.28% | **91.7 – 99.1** |
| largest observed step | 0.32% | **94.4 – 100.0** (four at 100.0) |
| within-segment realized vol | 0.09% | **95.4 – 100.0** |
| \|open-close return\| | 0.51% | 16.7 – 99.1 |
| max down excursion | −0.59% | 4.6 – 71.3 (low rank = deeper) |

Reopening discontinuity returns: −1.37% … +0.01%. Absent minutes: 10.0 each.

**Interpretation.** Excluding authoritative MWCB sessions removes sessions that
are extreme in **range, step size and realized volatility** — the halt cohort
sits above the continuous p99 on several measures, and four of eight had a
larger single observed step than *every* sampled continuous session. It does
**not** uniformly remove extreme *net* returns: `|open-close|` percentile ranks
span 16.7–99.1, because a session can be violently volatile and still close near
its open.

So the bias is real and directional in **path** terms, not in **net-return**
terms. N=8 across 4 dates: tail observations, not an inferential sample. No
significance is claimed.

---

## 8b. Durable evidence and derivation proof (foundation hardening)

Review of the committed foundation found two invariants weaker than the
architecture claimed. Both were reproduced before being fixed.

**A — graduation was caller-authoritative.** `session3_0_status(audit_dict)`
returned `SESSION_3_0_POLICY_READY` / `SESSION_3_1_GO` with **zero blockers**
from a fabricated dictionary, and still did so after the rendered population
JSON was deleted. A rendered report is a convenience; it is not authority.

Now: the audit is content-addressed under
`session3/population/content/<fp>/`, selected by an explicit pointer at
`session3/graduation/pointer.json`, and **re-verified on every call** —
accounting recomputed from the persisted per-chunk rows, exact MWCB prevalence
recomputed from the calendar and registry, policy and registry fingerprints
re-matched. The `audit` argument is retained for display and **cannot move the
verdict**. A pointer is selection, not authority.

**B — verification proved nothing about derivation.** A content hash proves an
object has not changed since minting; it does not prove it was minted
correctly. **Eight of eight** self-consistent-but-wrong views passed the old
verifier — faked `known_at`, faked close, faked `bar_end_at`, faked dataset
fingerprint, faked calendar identity, emptied `explained_missing`, downgraded
classification, and an unrelated valid raw object swapped in.

`verify_irregular_view` now **recomputes** the derivation: manifest binding,
canonical-dataset binding, calendar binding, raw lineage (the *manifest*
decides, as the exact relevant subset for that symbol), the exact reconciliation
row, a **re-run of `classify_session`**, and observed bars rebuilt from the raw
evidence through the **frozen Session 2 normalizer** and compared canonically —
proving OHLCV, `bar_start_at`, `bar_end_at`, `known_at` and `adjustment_state`
are unchanged. All eight bypasses now fail; the honest view still verifies.

The identity schema moved to `intraday_irregular_session_v2` because the
verification contract changed. No v1 objects were ever persisted, so nothing
required reminting; a v1-schema object is reported **archival**, never silently
current.

---

## 8c. Halt-boundary bar semantics

A nominal bar that *partially* overlaps a halt is genuine evidence — never
deleted, never synthesized. Measured tradable time inside the eight real
boundary bars ranges from **1 second** (2020-03-16 09:30) to 299 seconds.

The 1-second case decides the policy. A bar's **close** is a real traded price
at a real instant, and consecutive closes are exactly one bar-width apart
whatever happened inside. A bar's **intra-bar geometry** summarises the interval
itself, and over 1 second of trading it is a single print wearing a 5-minute
label.

| Primitive | Partial halt-boundary bar | Why |
|---|---|---|
| close endpoint | **ALLOWED** | a real traded price at a real instant |
| close-to-close return | **ALLOWED** | closes are one bar-width apart regardless of intra-interval halting |
| N-bar displacement | **ALLOWED** | built from close-to-close steps inside one segment |
| within-segment realized vol | **ALLOWED** | equally spaced closes; the reopening jump is reported separately |
| normalized_range | **BLOCKED** | high-low over 1s is not a 5-minute range; including it understates the most violent sessions |
| intra-bar open→close | **BLOCKED** | the open is the reopening auction print, the close may be seconds later |
| opening-range construction | **BLOCKED** | an interrupted window is not the opening range |

Decisions rest on temporal and economic meaning only. No strategy performance
was consulted, and none exists.

**Opening-window rule.** If an authoritative halt intersects a strategy's
*required* opening observation or range window, that session is
`FEATURE_UNAVAILABLE` for that strategy. `09:30, 09:45, 09:50` are never
compressed into a fake uninterrupted opening range.

---

## 9. Policy decision

**`HALT_AWARE_PRIMARY_CONTINUOUS_COMPARISON`**

Halt sessions are admissible only under a registry-backed, fully-explained-
absence contract, so admitting them costs no data integrity — and excluding them
removes precisely the regimes a strategy most needs to survive. **Both** cohorts
are always reported, because the halt cohort is tiny and execution during a halt
is prohibited.

Any future strategy result must name its population contract. "Historical
performance" without one is not a claim this lab accepts.

---

## 10. Limitations

- **Symbol-specific halts (LULD, news, regulatory, IPO pauses) are not
  classifiable.** No authoritative historical source is sanctioned, so such gaps
  remain `REJECTED_UNEXPLAINED_GAP`. Public NYSE/Nasdaq halt interfaces expose
  only limited recent history, insufficient for a 2017+ contract.
- Unexplained-gap prevalence is not measured (see §7).
- Comprehensive historical CTA/UTP/TAQ halt data would solve symbol-specific
  classification: **`RECOMMENDED_LATER`**, not required now. Nothing purchased.
- Execution during an authoritative halt is prohibited — frozen for Session 3.2.
  Whether resting orders remain queued or cancellable is venue-specific and is
  deliberately not invented here.

---

## 11. Session 3.1 handoff

Session 3.1 may implement a small, deterministic family of falsifiable intraday
strategy definitions **on this frozen population contract**. Still no production
authority, no AI-selected entries/exits, and `strategy_validation_allowed`
remains `false` until a strategy is actually validated.
