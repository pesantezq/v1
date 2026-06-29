# FinBERT Sentiment Scorer

## Purpose

`portfolio_automation/social_sentiment/finbert_scorer.py` is the local,
no-paid-API sentiment scorer for the social-sentiment lane. It wraps the
financial-domain model `ProsusAI/finbert` (HuggingFace, fine-tuned on the
Financial PhraseBank) to label each post as positive / neutral / negative and to
derive a continuous `sentiment_score = positive_probability - negative_probability`
in `[-1.0, +1.0]`. It is **CPU-safe**, **lazy-loading**, and degrades gracefully
when the model or its dependencies are unavailable.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. Its scores never feed `outputs/latest/decision_plan.json`
and never touch any score semantics (`feeds_decision_engine=false`,
`sandbox_only=true`). It runs entirely on local compute — no network calls beyond
an optional one-time model download.

---

## Design Guarantees

- **Lazy loading:** the model is loaded on the first `score()` / `score_batch()`
  call, never at import. A module-level singleton (`get_scorer`) holds one model
  instance per process.
- **Graceful degradation:** if `transformers` / `torch` are not installed, or the
  model is not in the local cache (and download is disallowed), or inference
  raises, every record gets `scorer="scorer_unavailable"` with `label="neutral"`
  and `neutral_probability=1.0`. The scorer **never raises**.
- **Offline-safe:** with `allow_download=False`, the model is loaded only from the
  local cache (`local_files_only=True`); it never reaches out to the hub.
- **Batched inference** under `torch.no_grad()` for efficiency.

---

## Config (`crowd_radar.finbert`)

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | master switch |
| `model_name` | `ProsusAI/finbert` | HuggingFace model ID |
| `max_length` | `512` | max token length |
| `batch_size` | `8` | texts per forward pass |
| `allow_download` | `false` | when `false`, load only from local cache |
| `cache_dir` | `data/finbert` | local model cache directory |

---

## Key API

- `class FinBERTScorer(config=None)` — safe to instantiate at import time.
  - `score(text) -> ScorerResult`
  - `score_batch(texts) -> list[ScorerResult]` — one result per input; on any
    failure all texts in the batch fall back to `scorer_unavailable`.
  - `is_available() -> bool` — checks availability (may trigger a load attempt).
  - `status` (property) — `disabled` / `not_yet_loaded` / `ok` /
    `scorer_unavailable:<reason>` for health reporting.
- `@dataclass ScorerResult` — `{text, status, sentiment_score,
  positive/neutral/negative_probability, label, scorer, scorer_version, error}`;
  `is_ok` is `True` when `status == "ok"`.
- `get_scorer(config=None) -> FinBERTScorer` — process-wide singleton.
- `score_records(records, scorer=None, config=None) -> list[dict]` — scores a list
  of text records in place via `schema.attach_sentiment`. Records that already
  carry `sentiment_score` are skipped; records with empty text get
  `scorer_unavailable`.

Constant: `SCORER_VERSION = "finbert-1.0"`.

---

## Related Modules

`schema` (`attach_sentiment` writes the fields produced here) · `pipeline` (calls
`score_records` after quality gates pass) · `aggregator` (only records with
`scorer="finbert"` contribute to the weighted mean).

---

## Tests

Covered under `tests/` with the social-sentiment suite
(`python -m pytest -q tests -k finbert`).
