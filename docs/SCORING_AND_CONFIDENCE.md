# Scoring And Confidence

Last verified against `watchlist_scanner/scanner.py`, `watchlist_scanner/confidence.py`, `watchlist_scanner/postprocess.py`, `watchlist_scanner/conviction.py`, `watchlist_scanner/alert_ranking.py`, `allocation_engine.py`, `policy_evaluator/*`, and `scoring.py`.

## Core Principle

`confidence_score` is not attractiveness.

- `signal_score`
  How interesting the opportunity is.
- `confidence_score`
  How trustworthy today's evidence is.

The system intentionally keeps them separate all the way through alerting, ranking, conviction, and evaluation.

## Watchlist Scores

### `signal_score`

Location:
`watchlist_scanner/scanner.py:_compute_signal_score`

Range:
`0.0` to `1.0`

Meaning:
Opportunity attractiveness for a watchlist symbol.

Formula:

- `0.45 * theme_news_score`
- `0.30 * technical_score`
- `0.25 * fundamental_context_score`

Sub-components:

- `theme_news_score`
  Theme strength, positive sentiment, and headline volume.
- `technical_score`
  1d momentum, 5d momentum, volume spike, SMA20/SMA50 position.
- `fundamental_context_score`
  Sector relevance, size/liquidity proxy, quality, and PE attractiveness.

Invariant:
Do not reinterpret this as data quality or reliability.

### `confidence_score`

Location:
`watchlist_scanner/confidence.py:compute_confidence`

Range:
`0.30` floor to `1.0`

Meaning:
Trustworthiness of the current signal based on provenance and completeness.

Primary inputs:

- freshness from `data_quality`
- completeness of price/technicals/news/fundamentals
- cache age when available
- budget/provenance source

Formula with cache age:

- `0.45 * freshness`
- `0.30 * completeness`
- `0.15 * cache_age`
- `0.10 * budget`

Formula without cache age:

- `0.53 * freshness`
- `0.32 * completeness`
- `0.15 * budget`

Bands:

- `high >= 0.85`
- `medium >= 0.65`
- `low < 0.65`

Invariant:
Confidence is about evidence quality, not expected return.

### `effective_score`

Location:
`watchlist_scanner/postprocess.py:_annotate_signal_meta`

Range:
Derived, then clamped at `>= 0.0`

Meaning:
Confidence-aware actionability score used after alert routing.

Formula:

- base: `signal_score * confidence_score`
- degraded mode: multiply by `(1 - degraded_confidence_penalty)`

Invariant:
This is a derived actionability metric. It must not replace the stored base scores.

### `conviction_score`

Location:
`watchlist_scanner/conviction.py:apply_conviction_layer`

Range:
`0.0` to `1.0`

Meaning:
Observe-only advisory sizing conviction.

Formula before caps:

- `0.50 * effective_score`
- `0.40 * confidence_score`
- `+ historical_performance_adjustment`
- `- degraded_mode_penalty`
- `- cooldown_penalty`

Additional caps:

- degraded-mode cap
- cooldown cap
- low-confidence cap on `high_conviction`
- weak-history cap to `observe`

Bands:

- `defer`
- `observe`
- `starter`
- `normal`
- `high_conviction`

Invariant:
This score is downstream and advisory. It must not feed back into `signal_score`.

### `final_rank_score`

Location:
`watchlist_scanner/alert_ranking.py:apply_priority_score`

Range:
Typically `0.0` to `1.0`

Meaning:
Holistic operator ordering score for watchlist opportunities.

Default formula:

- `0.40 * augmented_signal_score`
- `0.25 * confidence_score`
- `0.15 * theme_alignment_score`
- `0.20 * portfolio_fit_score`

Approved-weight override:

- `outputs/performance/approved_ranking_config.json` can provide replacement weights when `_valid == true`.

Invariant:
Used for ranking, not for changing the definitions of base scores.

### Related watchlist ranking scores

- `priority_score`
  Earlier alert-ordering score using signal, confidence, evidence, and freshness.
- `augmented_signal_score`
  Signal plus theme component.
- `trusted_signal_score`
  `signal_score * (0.7 + 0.3 * confidence_score)` for explainable ordering support.

## Policy Recommendation Score

### `recommendation_score`

Location:
`outputs/policy/policy_recommendation.json`

Meaning:
Advisory score for recommended policy/profile selection, not symbol attractiveness.

Produced by:
policy recommendation logic that combines:

- regime alignment
- available performance support
- safety

Important distinction:

- Watchlist `signal_score` ranks symbols.
- Policy `recommendation_score` ranks policy/profile choices.

These are different domains and must not be merged.

## Broader-Market And Legacy Scores

### Candidate scanner `score`

Location:
`scanner/candidate_scanner.py`

Range:
`0` to `100`

Meaning:
Broader-market candidate attractiveness based on revenue growth, FCF yield, ROE, PE attractiveness, and 200-DMA trend.

### Finance recommendation `final_score`

Location:
`scoring.py`

Range:
`0` to `100`

Meaning:
Legacy finance recommendation urgency after applying confidence to severity, persistence, impact, and priority.

Do not confuse this with watchlist `signal_score`.

## What Must Not Change

- `signal_score` and `confidence_score` must remain separate fields and separate concepts.
- `effective_score`, `conviction_score`, and `final_rank_score` are derived metrics and must stay clearly labeled as derived.
- `recommendation_score` must remain scoped to policy/profile recommendation artifacts.
- If new composite scores are added, keep base scores intact and document the derivation explicitly.
