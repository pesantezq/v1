# VS-001 — the first Northstar vertical slice

**Verdict: SUPPORTED, and you should not believe it means what it sounds like.**

The frozen acceptance rule returned SUPPORTED. The same run also shows the
signal score has no cross-sectional predictive power. Both are true, and the
gap between them is the most useful thing this slice produced.

---

## The question

> Over 2026-05-12 to 2026-06-01, did the watchlist signals earn a positive
> 7-day return **in excess of SPY over the same interval**, net of friction —
> or is the published 60.7% raw win rate explained by market drift?

The repository already published a 60.7% raw 7-day win rate and a +2.76% mean
7-day return. Nothing in the repository had ever subtracted a benchmark from
those numbers. That subtraction is the entire experiment.

Frozen before execution as `VS-001`, digest
`vsfreeze_f1c6ad413651834dff1dec548394a2d1`, over evidence
`sha256:960f7f42…`, committed at `fde3749` — one commit before any result
existed.

## What the run found

| metric | value |
|---|---|
| **scan-clustered mean net excess (primary)** | **+1.881 pp**, 95% CI **[1.061, 2.701]**, n = 20 scans |
| naive per-row mean net excess (diagnostic) | +1.881 pp, CI [1.209, 2.553], n = 420 |
| excess win rate | 53.8% (226/420), CI [0.490, 0.585] |
| raw win rate | 60.2% (253/420) |
| **Spearman IC, signal_score vs net excess** | **−0.059** |
| mean raw return / mean SPY return | +2.850% / +0.869% |
| worst scan | −1.071 pp |
| scans with negative mean | 20% (4/20) |

Population: 726 rows loaded → 440 matured 7-day outcomes → 420 scored across
20 scans and 21 tickers. 286 unmatured outcomes excluded, never imputed.

## Why SUPPORTED overstates the case

Three things undercut the headline, and the preregistration declared two of
them in advance:

1. **The score carries no information.** IC is **−0.059** — indistinguishable
   from zero, slightly negative. The excess win rate is 53.8% with a CI that
   nearly touches 0.49. The mean is carried by a right tail, not by ranking.
   Whatever produced the excess, it was not the number the system uses to rank.

2. **This is a beta result, not a skill result.** The watchlist is 21 high-beta
   names (NVDA, MARA, RIOT, COIN, PLTR, SMCI, TSLA…) measured in a single
   rising 20-day window where SPY returned +0.87%. Holding higher-beta assets
   in an up market beats SPY mechanically. **No risk adjustment was
   preregistered**, so the frozen rule is satisfiable with no selection skill
   whatsoever. This is journaled as `DIJ-0006`.

3. **Twenty scans is not twenty independent observations.** Scans are ~1 day
   apart with a 7-day horizon, so the windows overlap roughly 6/7. Clustering
   by scan fixes the cross-sectional correlation (21 tickers in one scan are
   close to one bet) but not the serial correlation. The effective independent
   sample is nearer 3 than 20, and a CI computed on 3 would include zero.

**The rule was not changed after seeing this.** Retrofitting a beta adjustment
or an IC requirement onto VS-001 now is exactly the failure preregistration
exists to prevent. The fix belongs to VS-002.

## What was actually exercised

```
signal_outcomes.csv
  → DataSourceDescriptor (provenance, pit_capability=reconstructable)
  → 2 × EvidenceSnapshot per row  (signal known_at=signal_time,
                                   outcome known_at=evaluated_at_7d)
  → EvidenceGateway.is_admissible at explicit as_of
  → ResearchClaim (rcl_199c314d…) — refused until it cited its evidence
  → ExperimentSpec (exs_c241cb53…)
  → scoring: excess vs same-scan SPY, minus 10 bps friction
  → ExperimentResult (exr_c9418305…) — authority-screened observations
```

**The leakage control is part of the result, not a claim in a comment.** Every
one of the 440 outcome snapshots was offered to the gateway at its own signal
instant and refused:

```json
{"outcome_snapshots_tested_at_signal_as_of": 440,
 "admitted_before_resolution": 0,
 "refusal_reasons": {"KNOWN_AT_AFTER_AS_OF": 440}}
```

An implementation that let the future in would report a nonzero admitted count
and fail its test, rather than quietly producing better numbers.

Friction: 10 bps round trip (5 bps each side), charged to the signal arm only —
the do-nothing alternative does not trade. Verified by a test asserting gross
minus net equals exactly the frozen assumption.

## What this proves about 0C

The slice ran to completion **without** the Research Store, which is not merged
at HEAD. That is evidence, not an opinion:

| 0C remaining work | verdict from the slice |
|---|---|
| revision/supersession safety | **not required by this class of experiment** — the source has no restatement mechanism. Required before any vendor fundamental data. |
| Research Store persistence/query | **not required to execute**; required to *scale*. 420 snapshots held in memory was sufficient. |
| historical as-of reads over the store | **required for correctness of the next experiment**, not this one — a single as_of sufficed here, a sweep needs the store. |
| lookahead audit over store reads | **partially satisfied in a different place** — the refusal ledger above *is* a lookahead audit, produced without a store. |
| replay/reproducibility | **satisfied for this slice** by content-addressed identity; two runs agree on spec id and every metric. |

**What the slice proved is actually missing** is not on the 0C list: there is no
price data. `outputs/backtest/historical/` is a documented contract with an
empty directory behind it, `data/fmp_cache/` is empty, and every table in
`data/portfolio.db` has zero rows. Any experiment needing a return series
rather than a pre-computed outcome column requires data acquisition first.

## Recommended minimum remaining 0C scope

1. An **evidence adapter contract** — VS-001's adapter is bespoke; the second
   source will need a shared shape.
2. **Research Store persistence** — bounded to what an as_of sweep needs.
3. **Historical as-of reads** with the refusal ledger promoted to a first-class
   artifact, since it already exists in ad-hoc form here.

Deferred on evidence: revision/supersession winner policy (no restatement
source yet), corporate actions (needed only when a return series is computed
rather than read).

## What the engines will need

Observations only — this authorizes nothing.

- **Prediction.** Needs the abstention path that already exists, and an
  uncertainty representation that survives being wrong: this slice's signal
  score would have scored ~0 on any calibration metric. A prediction contract
  that cannot express "ranked, but with no demonstrated ranking power" will
  encode false precision.
- **Capital/Risk.** Needs a **risk-adjusted** comparison, not a raw one. VS-001
  is the proof: a raw benchmark lets beta masquerade as skill. `NO_ACTION` must
  be an evaluated arm with a computed return, not merely a label — here the
  do-nothing arm was zero by construction, which was adequate only because
  nothing was allocated.
- **Exit/Replacement.** Needs friction as a first-class input. There is no
  cost model anywhere in the repository; the 10 bps used here was frozen by
  assertion, not measured.

## Honest limitations

One 20-day window · one regime (`regime_label` is `neutral` in all 726 rows, a
known upstream collapse, so no conditioning was attempted) · `confidence_score`
spans only [0.835, 1.0] · returns unadjusted for corporate actions · zero
execution delay assumed, which flatters the strategy · the labels are
upstream-recorded and were not recomputed from prices.

**A null result would have been equally valid.** So would this one, read
correctly: the machinery works, and the question was too weak to distinguish
skill from beta.
