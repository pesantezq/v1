# News Evidence Layer

## Overview

The News Evidence Layer (`portfolio_automation/news_evidence_layer.py`) converts existing structured news, narrative, and discovery evidence into decision-engine-adjacent **context**. It may enrich decision explanations, risk context, confidence notes, and operator-facing evidence summaries.

**Hard cap:** `news_evidence_influence_cap = "context_only"`. This layer cannot create, override, or mutate official BUY/SELL/HOLD decisions, allocation, scoring, recommendations, portfolio state, or watchlists.

## Safety Invariants

| Flag | Value |
|---|---|
| `observe_only` | `true` (hardcoded) |
| `no_trade` | `true` (hardcoded) |
| `not_recommendation` | `true` (hardcoded) |
| `no_decision_override` | `true` (hardcoded) |
| `no_score_mutation` | `true` (hardcoded) |
| `no_allocation_mutation` | `true` (hardcoded) |
| `no_watchlist_mutation` | `true` (hardcoded) |
| `influence_cap` | `"context_only"` |

No LLM/AI calls. All extraction is deterministic.

## Module Location

```
portfolio_automation/
  news_evidence_layer.py
```

## Public API

```python
from portfolio_automation.news_evidence_layer import run_news_evidence_layer

result = run_news_evidence_layer(base_dir="outputs", write_files=True)
```

### Functions

| Function | Purpose |
|---|---|
| `load_all_inputs(base_dir)` | Load all input artifacts safely; degrades on missing/malformed |
| `build_news_evidence_layer_report(inputs, base_dir)` | Build `NewsEvidenceLayerReport` from loaded inputs |
| `render_news_evidence_markdown(report)` | Render report as Markdown string |
| `write_news_evidence_layer_report(report, base_dir)` | Write JSON + MD to LATEST namespace (sanitizes & validates) |
| `run_news_evidence_layer(base_dir, write_files)` | Top-level orchestrator (catches and reports unsafe-write blocks) |
| `validate_news_evidence_safety(value)` | Walk a string/dict/list/dataclass and return prohibited phrases |
| `sanitize_news_evidence_text(value)` | Replace prohibited substrings with `[REDACTED]`, preserve disclaimer |
| `sanitize_label(value)` | Sanitize a label-style string (coerces non-strings) |
| `sanitize_nested_news_evidence_payload(payload)` | Recursively sanitize a JSON-serializable structure |

### Data Types

| Type | Purpose |
|---|---|
| `NewsEvidenceInputSummary` | Records per-input artifact availability |
| `TickerNewsEvidence` | Per-ticker aggregated evidence with strength + effect |
| `DecisionNewsContext` | Read-only decision context + news evidence strength/effect (no override) |
| `NewsRiskEvidence` | Aggregated risk signal across tickers |
| `NewsCatalystEvidence` | Aggregated catalyst signal across tickers |
| `NewsEvidenceLayerReport` | Full structured report |
| `UnsafeNewsEvidenceArtifactError` | Raised by the writer when prohibited language remains |

## Input Artifacts

All inputs degrade gracefully when missing, malformed, or non-object JSON.

| Artifact | Path | Purpose |
|---|---|---|
| News intelligence | `outputs/latest/news_intelligence.json` | Theme/risk/catalyst signals |
| Decision plan | `outputs/latest/decision_plan.json` | Existing decisions (read-only) |
| Decision explanations | `outputs/latest/decision_explanations.json` | Existing reasons (read-only) |
| Market narratives | `outputs/latest/market_narrative_{daily,weekly,monthly}.json` | Narrative context |
| System decision summary | `outputs/latest/system_decision_summary.json` | System health |
| Data quality report | `outputs/latest/data_quality_report.json` | Confidence context |
| Confidence calibration | `outputs/latest/confidence_calibration.json` | Calibration notes |
| Discovery enriched | `outputs/sandbox/discovery/news_enriched_candidates.json` | Sandbox candidate context |
| Discovery candidates | `outputs/sandbox/discovery/{emerging,rejected}_candidates.json` | Candidate state |
| Discovery replay | `outputs/sandbox/discovery/replay_results.json` | Replay backtest context |

## Output Artifacts

Both written to `OutputNamespace.LATEST`:

| Artifact | Path |
|---|---|
| News evidence layer (JSON) | `outputs/latest/news_evidence_layer.json` |
| News evidence layer (MD) | `outputs/latest/news_evidence_layer.md` |

### JSON shape

```jsonc
{
  "generated_at": "2026-05-11T00:00:00Z",
  "observe_only": true,
  "no_trade": true,
  "not_recommendation": true,
  "no_decision_override": true,
  "no_score_mutation": true,
  "no_allocation_mutation": true,
  "no_watchlist_mutation": true,
  "source": "news_evidence_layer",
  "influence_cap": "context_only",
  "data_available": true,
  "inputs_used": [...],
  "missing_inputs": [...],
  "portfolio_context": "...",
  "ticker_contexts": [
    {
      "ticker": "NVDA",
      "source": "news_intelligence",
      "matched_article_count": 5,
      "source_diversity": 3,
      "themes": [...],
      "risk_flags": [...],
      "catalyst_flags": [...],
      "context_note": "...",
      "evidence_strength": "moderate",   // none | weak | moderate | strong
      "context_effect": "catalyst_context" // informational | risk_context | catalyst_context | confidence_context
    }
  ],
  "decision_contexts": [
    {
      "ticker": "NVDA",
      "decision_action": "maintain",     // read-only copy
      "decision_reason": "...",          // read-only copy
      "news_evidence_strength": "moderate",
      "news_context_effect": "catalyst_context",
      "context_note": "...",
      "no_decision_override": true
    }
  ],
  "risk_evidence": [...],
  "catalyst_evidence": [...],
  "discovery_context_summary": "...",
  "confidence_context": [...],
  "operator_review_flags": [...],
  "memo_bullets": [...],
  "prohibited_actions_detected": [],
  "safety_disclaimer": "..."
}
```

## Evidence Strength Classification

| Band | Condition |
|---|---|
| `none` | 0 matched articles |
| `weak` | <4 articles or <2 sources |
| `moderate` | ≥4 articles and ≥2 sources |
| `strong` | ≥8 articles and ≥4 sources |

## Context Effect Classification

| Value | Condition |
|---|---|
| `informational` | Balanced or single signals |
| `risk_context` | ≥2 risk flags and risk > catalyst count |
| `catalyst_context` | ≥1 catalyst flag and catalyst > risk count |
| `confidence_context` | No matched articles (strength = `none`) |

No `BUY`/`SELL`/`HOLD`/`PROMOTED`/`VALIDATED`/`ACTIONABLE` values are ever emitted.

## Sanitizer & Validator

Three layers of defense against prohibited language:

1. **Label-level sanitization** — every input-derived label (theme names, risk/catalyst labels, ticker fields, severities, decision actions, decision reasons) passes through `sanitize_label()` or `sanitize_news_evidence_text()` before being inserted into the report.

2. **Full-payload sanitization** — the serialized JSON payload is recursively scrubbed by `sanitize_nested_news_evidence_payload()` immediately before write. The rendered Markdown is sanitized as well.

3. **Pre-write validation** — `validate_news_evidence_safety()` walks the full payload and the rendered Markdown. If any prohibited phrase remains, `write_news_evidence_layer_report()` raises `UnsafeNewsEvidenceArtifactError` and **no artifact is written**.

### Prohibited patterns

Includes (non-exhaustive): `buy now`, `sell now`, `hold now`, `trim now`, `trim position`, `rebalance now`, `add shares`, `buy shares`, `sell shares`, `reduce shares`, `execute trade`, `execute order`, `place trade`, `place order`, `promote candidate`, `promote to watchlist`, `actionable buy`, `actionable sell`, `validated buy`, `validated sell`, `official recommendation`, `recommend buying`, `recommend selling`, `recommend holding`, `i recommend`, `you should buy/sell/hold`, `consider buying/selling`.

### Allowed exception

The fixed `_SAFETY_DISCLAIMER` and discovery disclaimer may legitimately contain "buy/sell/hold recommendation" wording (they explicitly state the artifact is **not** such a recommendation). The sanitizer carves them out via placeholder splicing.

## No-Mutation Boundary

The layer is read-only against all upstream artifacts:
- Decision actions and reasons are copied verbatim into `decision_contexts` and never modified
- No `signal_score`, `confidence_score`, `effective_score`, or other scoring fields are emitted
- No `allocation`, `target_weight`, or allocation-mutation fields are emitted
- No `watchlist`, `watchlist_add`, or watchlist-mutation fields are emitted

## Tests

File: `tests/test_news_evidence_layer.py`
Count: 74 tests across 8 test classes

Coverage: input loading, sanitizers, report building, ticker matching, evidence strength/effect classification, markdown rendering, artifact writing, orchestrator, adversarial input protection, no-mutation boundary verification, determinism.
