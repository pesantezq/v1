# Social Sentiment Pipeline

## Purpose

`portfolio_automation/social_sentiment/pipeline.py` is the orchestrator of the
social-sentiment lane. For a set of tickers it: fetches text posts from the
configured free connectors (Bluesky / Mastodon / Lemmy), applies quality gates per
`(ticker, source)`, scores gate-passing posts with FinBERT, aggregates per-source
then cross-source (with the source cap), records the daily history and trend, and
writes two sandbox artifacts. It is the single entry point downstream callers use
(`run_social_sentiment_pipeline`).

---

## Two-Lane Governance

This module is the heart of the **simulation-active / production-gated /
sandbox-only** social-sentiment lane. The governance invariants are **hardcoded,
never conditional**:

```python
SIMULATION_ACTIVE = True
PRODUCTION_GATED = True
HUMAN_APPROVAL_REQUIRED = True
FEEDS_DECISION_ENGINE = False
SANDBOX_ONLY = True
```

Every artifact carries `feeds_decision_engine=false` and `sandbox_only=true`. The
pipeline writes only to `OutputNamespace.SANDBOX` — never `LATEST`, `POLICY`, or
`HISTORICAL`. The top-level `run_social_sentiment_pipeline` wraps the whole run in
`try/except` and returns a degraded status dict on failure (it never raises into
the daily run).

---

## Artifacts (SANDBOX)

| File | Path | Namespace |
|------|------|-----------|
| Status / crowd-bus extension | `outputs/sandbox/discovery/social_sentiment_status.json` | `OutputNamespace.SANDBOX` |
| Simulation adjustment | `outputs/sandbox/discovery/social_sentiment_simulation_adjustment.json` | `OutputNamespace.SANDBOX` |

**Status (`social_sentiment_status.json`)** carries the unified crowd-bus fields
per ticker: `social_sentiment_score`, `social_sentiment_confidence`,
`social_sentiment_source_count`, `social_attention_score` (from ApeWisdom
attention data), `social_quality_state` (trend state). `schema_version="2"`.

**Simulation adjustment** is written only when
`simulation_social_sentiment.enabled` (default `true`). Per ticker it computes a
**bounded** score nudge: `sentiment_score * max_score_adjustment`, clamped to
`[-max_score_adjustment, +max_score_adjustment]`. Tickers below
`min_confidence` / `min_source_count` get a zero adjustment with reason
`below_confidence_threshold`. This is a *simulation* artifact — it does not modify
production scores.

---

## Module API

- `run_social_sentiment_pipeline(tickers, root=".", *, cfg=None,
  text_connectors=None, attention_data=None, scorer=None) -> dict` — public entry
  point; returns a status dict (`status`, `run_id`, `tickers_processed`,
  `tickers_scored`, `artifacts`, `warnings`, governance flags). `status` is `ok`
  when at least one ticker scored, else `insufficient_data`.
  - `cfg` is the `config.json crowd_radar` block.
  - `text_connectors` lets tests inject connectors; otherwise `_build_connectors`
    builds Bluesky/Mastodon/Lemmy from `cfg.source_policy` (each source skipped if
    its `enabled` is false).
  - `attention_data` is the `{ticker: attention_score}` map from ApeWisdom.
- `@dataclass TickerSentimentResult` — `{ticker, aggregate, per_source,
  trend_state, sources_attempted, fetch_warnings}`; `to_dict()`.

---

## Pipeline Integration

Invoked by the Stage 9c4 CLI runner — see `docs/run_sentiment_pipeline.md`. The
runner is wired into `scripts/run_daily_safe.sh` (Stage 9c4) and reads its ticker
set from `crowd_multi_source_velocity.json` (Stage 9c1) plus current portfolio
holdings.

---

## Related Modules

`run_sentiment_pipeline` (CLI runner) · `aggregator` · `finbert_scorer` ·
`quality_gates` · `history` · `schema` · the text connectors
(`bluesky_connector`, `mastodon_connector`, `lemmy_connector`).

---

## Tests

Covered under `tests/` with the social-sentiment suite
(`python -m pytest -q tests -k sentiment`).
