# Social Sentiment Record Schema

## Purpose

`portfolio_automation/social_sentiment/schema.py` defines the normalized,
privacy-safe record contract that every text-producing connector
(`bluesky_connector`, `mastodon_connector`, `lemmy_connector`) emits and that the
rest of the sentiment lane consumes. It is the shared vocabulary for the pipeline:
connectors `make_text_record(...)`, the FinBERT scorer `attach_sentiment(...)`,
and the quality gates / aggregator read these fields. Text records are
`schema_version="2"` and carry `source_type="text"`; attention-producing sources
(ApeWisdom) stay `schema_version="1"` / `source_type="attention"` and carry no
sentiment fields.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. Records produced under this schema never feed
`outputs/latest/decision_plan.json` and never touch any score semantics
(`feeds_decision_engine=false`, `sandbox_only=true`). It is a pure data contract —
no I/O, no network, no config.

---

## Key Functions

- `make_text_record(*, source, ticker, post_id_hash, author_hash, created_at, text,
  text_len, like_count=0, reply_count=0, repost_count=0, engagement_score=0.0,
  language="en", extra=None) -> dict` — builds a validated text record. Bounds are
  enforced on construction: `post_id_hash[:16]`, `author_hash[:12]`, `text[:500]`,
  counts clamped `>= 0`, `engagement_score` clamped to `[0.0, 1.0]` and rounded.
  Source-specific `extra` keys (e.g. `instance` for Mastodon/Lemmy) are merged in
  only if they do not collide with a reserved field. Sentiment fields are **absent**
  until the scorer runs.
- `is_valid_text_record(record) -> bool` — tolerant validator. Requires the minimum
  fields `{source, source_type, ticker, post_id_hash, created_at, text}` plus a
  non-empty `ticker` and `text`. Unknown extra fields are accepted
  (forward-compatible readers).
- `is_sentiment_scored(record) -> bool` — `True` once `sentiment_score` and
  `scorer` are present.
- `attach_sentiment(record, *, sentiment_score, positive_probability,
  neutral_probability, negative_probability, label, scorer, scorer_version) -> dict`
  — attaches FinBERT fields in-place (rounded to 4 dp) and returns the record.

---

## Record Shape

| Field | Type | Notes |
|-------|------|-------|
| `schema_version` | str | `"2"` for text records |
| `source` | str | `bluesky` / `mastodon` / `lemmy` |
| `source_type` | str | `"text"` |
| `ticker` | str | upper-cased |
| `post_id_hash` | str | sha256-derived, `[:16]` |
| `author_hash` | str | sha256-derived, `[:12]` (privacy-safe) |
| `created_at` | str | ISO-8601 timestamp |
| `text` | str | bounded to 500 chars |
| `text_len` | int | original length |
| `like_count` / `reply_count` / `repost_count` | int | `>= 0` |
| `engagement_score` | float | `[0.0, 1.0]` |
| `language` | str | bounded to 8 chars |
| `sentiment_score` | float | `[-1.0, +1.0]`, added by scorer |
| `positive/neutral/negative_probability` | float | added by scorer |
| `label` | str | `positive` / `neutral` / `negative` |
| `scorer` / `scorer_version` | str | `finbert` / `scorer_unavailable` |

Constants: `SCHEMA_VERSION = "2"`, `ATTENTION_SCHEMA_VERSION = "1"`.

---

## Related Modules

`finbert_scorer` (fills sentiment fields) · `quality_gates` (reads author/text/
created_at) · `aggregator` (consumes scored records) · `pipeline` (orchestrator).

---

## Tests

Covered under `tests/` with the social-sentiment suite
(`python -m pytest -q tests -k sentiment`).
