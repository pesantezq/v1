# Social Sentiment Aggregator

## Purpose

`portfolio_automation/social_sentiment/aggregator.py` collapses scored,
gate-passed posts into per-source and then cross-source sentiment aggregates per
ticker. It computes an engagement-weighted mean sentiment per source, then a
cross-source weighted mean where **no single source may dominate**
(`MAX_SOURCE_CONTRIBUTION = 0.40`), and derives a confidence score from sample
size, source count, and scorer availability.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. Its aggregates never feed
`outputs/latest/decision_plan.json` and never touch any score semantics
(`feeds_decision_engine=false`, `sandbox_only=true`). Pure functions — no I/O.

---

## Aggregation Logic

1. **Per-source** (`aggregate_source`): only records with `scorer="finbert"`
   contribute; `engagement_score` is the weight (floored at `0.01`). If the
   quality gate failed, a neutral non-contributing `PerSourceResult` is returned.
   If the gate passed but all records are `scorer_unavailable`, the source counts
   for quality but contributes zero sentiment.
2. **Cross-source** (`aggregate_cross_source`): base weights are each source's
   share of total posts, then `_apply_source_cap` iteratively trims any source
   above the 0.40 cap and redistributes the excess to under-cap sources before
   normalizing to sum 1.0. Failed sources are excluded but still reported.
3. **Single source:** if exactly one source contributes, `is_single_source=True`.
4. **Confidence** (`_compute_confidence`): `0.4 * size_conf + 0.3 * source_conf +
   0.3 * scorer_conf - gate_penalty`, clamped to `[0, 1]`. `size_conf` asymptotes
   at ~100 posts; `source_conf` at 3 sources; `scorer_conf` is the scored fraction;
   `gate_penalty` is up to 0.3 when sources failed gates.

---

## Key API

- `aggregate_source(records, source, ticker, quality_result) -> PerSourceResult`
- `aggregate_cross_source(per_source_results, ticker) -> AggregateResult`
- `@dataclass PerSourceResult` — per-source aggregate
  (`sentiment_score`, prob fields, `sample_size`, `engagement_weighted`,
  `quality_passed`, `quality_stats`, `failure_reasons`,
  `scorer_unavailable_count`); `to_dict()`.
- `@dataclass AggregateResult` — cross-source aggregate
  (`sentiment_score`, prob fields, `confidence`, `source_count`, `total_posts`,
  `is_single_source`, `sources_contributing`, `sources_failed`, `per_source`);
  `to_dict()`.

Constant: `MAX_SOURCE_CONTRIBUTION = 0.40`.

---

## Related Modules

`finbert_scorer` (produces the `scorer="finbert"` records this consumes) ·
`quality_gates` (`QualityGateResult` gates each source) · `pipeline` (calls both
aggregation functions per ticker) · `schema`.

---

## Tests

Covered under `tests/` with the social-sentiment suite
(`python -m pytest -q tests -k aggregat`).
