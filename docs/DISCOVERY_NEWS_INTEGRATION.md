# Discovery News Integration

## Overview

The Discovery News Integration layer (`portfolio_automation/discovery/news_integration.py`) enriches sandbox discovery candidates with structured evidence from the FMP News Intelligence layer.

**This layer is sandbox-only, observe-only, and produces no official state changes.**

Safety invariants (all hardcoded):
- `observe_only: true`
- `no_trade: true`
- `not_recommendation: true`
- `discovery_only: true`
- No BUY/SELL/HOLD/PROMOTED/VALIDATED/ACTIONABLE statuses
- No official portfolio, watchlist, allocation, recommendation, or scoring mutation
- No discovery candidate promotion
- Writes only to `OutputNamespace.SANDBOX`
- Reads `outputs/latest/news_intelligence.json` as read-only input
- No LLM/AI calls — deterministic rules only

## Module Location

```
portfolio_automation/
  discovery/
    news_integration.py
```

## Public API

```python
from portfolio_automation.discovery.news_integration import run_discovery_news_integration

result = run_discovery_news_integration(
    base_dir="outputs",
    run_mode="discovery",   # Only DISCOVERY or BACKTEST may write
    run_id="2026-05-11_discovery",
    dry_run=False,
)
```

### Individual functions

| Function | Purpose |
|---|---|
| `load_news_intelligence(base_dir)` | Load `outputs/latest/news_intelligence.json` safely |
| `load_news_candidate_evidence(base_dir)` | Load `outputs/sandbox/discovery/news_candidate_evidence.json` safely |
| `load_emerging_candidates(base_dir)` | Load current emerging discovery candidates |
| `load_rejected_candidates(base_dir)` | Load rejected discovery candidates |
| `match_evidence_to_candidates(evidence_packets, candidates)` | Match news packets to candidates by ticker |
| `enrich_candidates(candidates, matched_evidence, all_evidence_packets)` | Build enriched candidate records |
| `build_integration_summary(enriched, run_mode, generated_at)` | Build Markdown summary |
| `write_news_integration_artifacts(base_dir, enriched, summary_md, run_mode, run_id)` | Write sandbox artifacts |
| `run_discovery_news_integration(base_dir, run_mode, run_id, dry_run)` | Top-level orchestrator |

## Processing Pipeline

```
Inputs:
  outputs/latest/news_intelligence.json          (LATEST, read-only)
  outputs/sandbox/discovery/news_candidate_evidence.json (SANDBOX, read-only)
  outputs/sandbox/discovery/emerging_candidates.json     (SANDBOX, read-only)
  outputs/sandbox/discovery/rejected_candidates.json     (SANDBOX, read-only)
    ↓
match_evidence_to_candidates()   # ticker-based matching
    ↓
enrich_candidates()              # build enriched records with news context
    ↓
build_integration_summary()      # human-readable summary
    ↓
Outputs (SANDBOX only):
  outputs/sandbox/discovery/news_enriched_candidates.json
  outputs/sandbox/discovery/news_integration_summary.md
```

## Inputs

### News Intelligence Artifact (read-only)
- `outputs/latest/news_intelligence.json` — evidence packets from FMP news
- `outputs/sandbox/discovery/news_candidate_evidence.json` — sandbox-lane evidence

Both are consumed as read-only. This layer never writes to `outputs/latest`.

### Discovery Candidate Artifacts (read-only)
- `outputs/sandbox/discovery/emerging_candidates.json` — WATCH and DISCOVERED candidates
- `outputs/sandbox/discovery/rejected_candidates.json` — REJECTED candidates

All artifacts degrade gracefully when missing or malformed.

## Matching Logic

Tickers are matched by:
1. **entity_key** — primary ticker symbol in evidence packet
2. **related_tickers** — secondary tickers in evidence packet

Matching is case-insensitive and deterministic.

## Enriched Candidate Fields

Each enriched record contains:

| Field | Type | Description |
|---|---|---|
| `ticker` | string | Canonical ticker symbol |
| `candidate_status` | string | Original status or `news_only` for new tickers |
| `discovery_only` | bool | Always `true` |
| `observe_only` | bool | Always `true` |
| `no_trade` | bool | Always `true` |
| `not_recommendation` | bool | Always `true` |
| `matched_news_count` | int | Total articles mentioning this ticker |
| `matched_evidence_packets` | int | Evidence packets matched |
| `source_diversity` | int | Total unique source count |
| `matched_themes` | array | Aggregated theme names |
| `catalyst_flags` | array | Aggregated catalyst keywords |
| `risk_flags` | array | Aggregated risk keywords |
| `news_relevance_score` | float | 0.0–1.0 relevance based on article count and source diversity |
| `corroboration_news_score` | float | 0.0–1.0 corroboration bonus from news diversity |
| `news_context` | string | `research_supported`, `research_caution`, `research_neutral`, or `no_news` |
| `latest_news_headlines` | array | Top article headlines (max 5) |
| `integration_reason` | string | Human-readable match explanation |
| `safety_disclaimer` | string | Mandatory disclaimer text |
| `original_score` | float/null | Original discovery score |
| `original_mention_count` | int/null | Original discovery mention count |
| `original_corroboration_score` | float/null | Original corroboration score |
| `first_seen` | string/null | First seen timestamp |
| `last_seen` | string/null | Last seen timestamp |

### News Context Values

| Value | Condition |
|---|---|
| `no_news` | No matching articles found |
| `research_supported` | More catalyst signals than risk signals |
| `research_caution` | ≥2 risk signals and risk > catalyst count |
| `research_neutral` | Balanced risk/catalyst or single signals |

No `PROMOTED`, `VALIDATED`, `ACTIONABLE`, `BUY`, or `SELL` values are ever emitted.

## News-Only Tickers

Tickers appearing in sandbox-lane news evidence but not in any existing discovery candidate are added as `candidate_status: "news_only"` records. These are flagged as needing independent corroboration and are never auto-promoted.

## Artifacts Produced

| Artifact | Namespace | Path |
|---|---|---|
| `news_enriched_candidates.json` | SANDBOX | `outputs/sandbox/discovery/news_enriched_candidates.json` |
| `news_integration_summary.md` | SANDBOX | `outputs/sandbox/discovery/news_integration_summary.md` |

Both artifacts include all safety flags and the sandbox disclaimer.

## Run Mode Governance

| Mode | Sandbox write allowed? |
|---|---|
| `DISCOVERY` | Yes |
| `BACKTEST` | Yes |
| `DAILY` | No — treated as dry_run |
| `MANUAL_UPDATE` | No — treated as dry_run |
| `WEEKLY_REVIEW` | No — treated as dry_run |
| `HISTORICAL_REPLAY` | No — treated as dry_run |

`write_news_integration_artifacts()` raises `RunModeViolation` when called directly with a non-sandbox mode.  The orchestrator (`run_discovery_news_integration()`) handles this gracefully by setting `dry_run=True`.

## Safety Boundaries

- **Never promotes**: `candidate_status` can only be `discovered`, `watch`, `rejected`, or `news_only`
- **Never writes official state**: no LATEST, POLICY, PORTFOLIO writes
- **Never emits recommendations**: no BUY/SELL/HOLD outputs
- **Read-only on inputs**: news intelligence artifacts are never modified
- **Deterministic**: same inputs always produce same enriched output

## Tests

File: `tests/discovery/test_news_integration.py`
Count: 72 tests across 7 test classes

Coverage: missing/malformed inputs, enrichment, matching by entity_key/related_tickers, theme/flag aggregation, source diversity, news-only tickers, forbidden status guards, official namespace write blocking, run-mode governance, dry_run, markdown disclaimer, determinism.

## Relationship to Later Phases

| Future phase | How it uses this layer |
|---|---|
| `daily_weekly_monthly_ai_market_narratives` | Uses enriched evidence as narrative context input |
| `news_evidence_layer_for_decision_engine` | Attaches enriched evidence to decision plan entries as context |
| `automatic_promotion_governance_layer` | Enriched news context feeds news_relevance_score / risk-flag aggregation used by the automatic promotion gates (replaces the previously planned `manual_promotion_proposal`) |
