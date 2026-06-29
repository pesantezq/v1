# Social Sentiment Quality Gates

## Purpose

`portfolio_automation/social_sentiment/quality_gates.py` is the
anti-manipulation layer for the social-sentiment lane. Gates are applied per
`(ticker, source)` batch **before** any sentiment is aggregated; a batch that
fails any gate contributes **no** sentiment to the aggregate and is quarantined
with explicit failure reasons. This blocks brigading, bot floods, copy-paste spam,
and stale data from skewing the simulation signal.

---

## Two-Lane Governance

This module is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. Its decisions never feed `outputs/latest/decision_plan.json`
and never touch any score semantics (`feeds_decision_engine=false`,
`sandbox_only=true`).

---

## The Gates

A batch passes only if **every** gate clears. Thresholds are overridable via the
`crowd_radar.quality_gates` config dict (defaults shown).

| Gate | Default | Failure reason token |
|------|---------|----------------------|
| Minimum post count | `min_posts=10` | `too_few_posts:N<M` |
| Minimum unique authors | `min_unique_authors=6` | `too_few_authors:N<M` |
| Single-author concentration | `max_author_concentration=0.20` | `high_author_concentration:X>Y` |
| Duplicate-text ratio | `max_duplicate_ratio=0.35` | `high_duplicate_ratio:X>Y` |
| Spam ratio | `max_spam_ratio=0.40` | `high_spam_ratio:X>Y` |
| Stale ratio (>50% older than window) | `max_age_hours=24.0` | `too_old:P%_older_than_Hh` |
| Empty batch | — | `no_records` |

Detection helpers:

- **Duplicate detection** — short md5 fingerprint of normalized, lower-cased,
  whitespace-collapsed text `[:200]` (used for de-dup only, not security).
- **Spam heuristic** (`_is_likely_spam`) — text shorter than 20 chars, >70%
  upper-case, or more than 5 `!`/`?` characters.
- **Age** — `created_at` parsed across several ISO formats; un-parseable
  timestamps are skipped.

> Note: the production `config.json` currently relaxes two gates to
> `min_posts=1` and `min_unique_authors=0` while the lane bootstraps coverage —
> see `docs/ALLOCATION_POLICY.md` / `config.json crowd_radar.quality_gates` for
> the live values.

---

## Key API

- `class QualityGateChecker(config=None)`
  - `check(records, *, source="", ticker="") -> QualityGateResult` — runs all
    gates and returns the result.
- `@dataclass QualityGateResult` — `{passed: bool, failure_reasons: list[str],
  stats: dict}`; `to_dict()` for the audit trail. `stats` carries the computed
  metrics (`n`, `unique_authors`, `author_concentration`, `top_author_hash`,
  `duplicate_ratio`, `spam_ratio`, `old_ratio`, `mean_age_hours`).

---

## Related Modules

`schema` (provides `author_hash`, `text`, `created_at`) · `aggregator` (a failed
`QualityGateResult` yields a neutral, non-contributing `PerSourceResult`) ·
`pipeline` (calls `check` per `(ticker, source)` then scores only on pass).

---

## Tests

Covered under `tests/` with the social-sentiment suite
(`python -m pytest -q tests -k quality`).
