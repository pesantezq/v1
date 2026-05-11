# Automatic Promotion Governance

## Overview

The Automatic Promotion Governance layer (`portfolio_automation/discovery/automatic_promotion_governance.py`) replaces the previously planned `manual_promotion_proposal` step with a **deterministic, observe-only, sandbox-only** governance layer that automatically evaluates discovery candidates against explicit gates and graduates qualified candidates to a safer **MONITOR** research state.

**This layer never creates BUY/SELL/HOLD/ACTIONABLE/PROMOTED/VALIDATED/APPROVED/TRADE/RECOMMENDATION outputs. It never mutates portfolio, watchlist, scoring, allocation, recommendation, or decision state.**

## Safety Invariants

| Flag | Value |
|---|---|
| `observe_only` | `true` (hardcoded) |
| `no_trade` | `true` (hardcoded) |
| `not_recommendation` | `true` (hardcoded) |
| `discovery_only` | `true` (hardcoded) |
| `no_portfolio_mutation` | `true` (hardcoded) |
| `no_watchlist_mutation` | `true` (hardcoded) |
| `no_decision_override` | `true` (hardcoded) |
| `no_score_mutation` | `true` (hardcoded) |
| `no_allocation_mutation` | `true` (hardcoded) |

No LLM/AI calls. Pure deterministic gates.

## Status Model

| Allowed (emitted) | Forbidden (never emitted) |
|---|---|
| `DISCOVERED` | `BUY` |
| `WATCH` | `SELL` |
| `MONITOR` | `HOLD` |
| `REJECTED` | `ACTIONABLE` |
| `EXPIRED` | `PROMOTED` |
| `NEEDS_REVIEW` | `VALIDATED` |
| | `APPROVED` |
| | `TRADE` |
| | `RECOMMENDATION` |

## Public API

```python
from portfolio_automation.discovery.automatic_promotion_governance import (
    run_automatic_promotion_governance,
)

result = run_automatic_promotion_governance(
    base_dir="outputs",
    run_mode="discovery",           # only DISCOVERY/BACKTEST can write
    run_id="2026-05-11_apg",
    dry_run=False,
    write_files=True,
)
```

### Functions

| Function | Purpose |
|---|---|
| `load_automatic_promotion_inputs(base_dir)` | Load all input artifacts safely |
| `evaluate_candidate_promotion(candidate, context, gates, now)` | Per-candidate gate evaluation |
| `build_automatic_promotion_report(inputs, run_mode, run_id, gates)` | Build full structured report |
| `render_automatic_promotion_markdown(report)` | Render Markdown summary |
| `write_automatic_promotion_report(report, base_dir, run_mode, run_id)` | Write 3 sandbox artifacts (sanitizes + validates) |
| `run_automatic_promotion_governance(...)` | Top-level orchestrator |
| `validate_automatic_promotion_safety(value)` | Walk and detect prohibited phrases / standalone actions |
| `sanitize_automatic_promotion_text(value)` | Sanitize text (preserves disclaimer) |
| `sanitize_label(value)` | Sanitize a label-style string; pure-action labels → neutral marker |
| `sanitize_nested_automatic_promotion_payload(payload)` | Recursive sanitization |

### Data types

| Type | Purpose |
|---|---|
| `PromotionGates` | Tunable thresholds dataclass (conservative defaults via `DEFAULT_GATES`) |
| `PromotionEligibilityResult` | Per-candidate evaluation outcome |
| `PromotionDecision` | Full decision record (also serialized to JSONL log) |
| `AutomaticPromotionReport` | Full structured report |
| `UnsafeAutomaticPromotionArtifactError` | Raised when prohibited language remains |

## Input Artifacts (all read-only)

| Artifact | Path |
|---|---|
| Emerging candidates | `outputs/sandbox/discovery/emerging_candidates.json` |
| Rejected candidates | `outputs/sandbox/discovery/rejected_candidates.json` |
| Discovery memory | `outputs/sandbox/discovery/discovery_memory.json` |
| News-enriched candidates | `outputs/sandbox/discovery/news_enriched_candidates.json` |
| News candidate evidence | `outputs/sandbox/discovery/news_candidate_evidence.json` |
| Replay results | `outputs/sandbox/discovery/replay_results.json` |
| Approval decisions | `outputs/sandbox/discovery/approval_decisions.jsonl` |
| News evidence layer | `outputs/latest/news_evidence_layer.json` |
| News intelligence | `outputs/latest/news_intelligence.json` |
| Daily market narrative | `outputs/latest/market_narrative_daily.json` |
| Data quality report | `outputs/latest/data_quality_report.json` |

All inputs degrade gracefully on missing/malformed/non-object JSON.

## Output Artifacts (sandbox namespace only)

| Artifact | Path | Type |
|---|---|---|
| Candidate snapshot | `outputs/sandbox/discovery/automatic_promotion_candidates.json` | JSON |
| Audit log | `outputs/sandbox/discovery/automatic_promotion_decisions.jsonl` | Append-only JSONL |
| Summary | `outputs/sandbox/discovery/automatic_promotion_summary.md` | Markdown |

## Governance Gates (conservative defaults)

| Gate | Default | Purpose |
|---|---|---|
| `minimum_corrob_score` | `0.65` | Corroboration score required for MONITOR |
| `minimum_source_diversity` | `2` | Distinct sources required |
| `minimum_news_relevance` | `0.4` | News relevance score required |
| `maximum_risk_flags` | `2` | Above this → REJECTED |
| `stale_after_days` | `30` | Beyond this without signal → EXPIRED |
| `minimum_persistence_runs` | `2` | Discovery memory runs required |
| `minimum_persistence_mentions` | `3` | Mention count required |
| `require_watch_status_for_monitor` | `True` | Must already be WATCH to graduate |
| `require_persistence_for_monitor` | `True` | Must have persistence evidence |
| `block_rejected_candidates` | `True` | Rejected list → REJECTED (no override) |
| `block_forbidden_statuses` | `True` | Upstream forbidden actions → REJECTED |

## State Machine

```
DISCOVERED ──gates fail──► DISCOVERED (hold)
DISCOVERED ──gates pass──► DISCOVERED (require_watch_status fails)
WATCH      ──all gates pass──► MONITOR
WATCH      ──soft gates fail──► NEEDS_REVIEW
WATCH      ──risk/forbidden──► REJECTED
WATCH      ──stale──► EXPIRED
*          ──in rejected list──► REJECTED
*          ──upstream forbidden status──► REJECTED
```

## Decision record fields (`PromotionDecision`)

Each `decisions[]` entry contains:

- `ticker`
- `prior_status` (normalized to ALLOWED_STATUSES)
- `proposed_status` (always in ALLOWED_STATUSES, never in FORBIDDEN_STATUSES)
- `decision_type` (`promote_to_monitor` / `demote_to_review` / `reject` / `expire` / `hold_status`)
- `eligibility_result` (sanitized summary of gates passed/failed)
- `evidence_score` (0–1, deterministic weighted mix)
- `evidence_summary` (sanitized)
- `gates_passed`, `gates_failed`
- `risk_flags`, `catalyst_flags` (sanitized)
- `corroboration_score`, `news_relevance_score`, `source_diversity`
- `replay_context`, `memory_context`, `operator_context` (sanitized)
- `safety_flags` (9 hardcoded `True` flags)
- `created_at`, `reason` (sanitized)

## Run-Mode Governance

| Mode | Sandbox write allowed? |
|---|---|
| `DISCOVERY` | Yes |
| `BACKTEST` | Yes |
| `DAILY` | No — treated as dry-run |
| `MANUAL_UPDATE` | No — treated as dry-run |
| `WEEKLY_REVIEW` | No — treated as dry-run |
| `HISTORICAL_REPLAY` | No — treated as dry-run |

`write_automatic_promotion_report()` raises `RunModeViolation` when called directly with a non-sandbox mode. The orchestrator handles this gracefully via `dry_run=True`.

## Sanitization & Validation (3 layers)

1. **Label-level**: every input-derived label passes through `sanitize_label()` at extraction time. Pure-action labels (`BUY`, `SELL`, `HOLD`, etc.) become `"redacted_action_label_context_only"`.
2. **Full-payload**: the serialized JSON payload and rendered Markdown are recursively scrubbed by `sanitize_nested_automatic_promotion_payload()` and `sanitize_automatic_promotion_text()` immediately before write.
3. **Pre-write validation**: `validate_automatic_promotion_safety()` walks the full payload + Markdown + JSONL records, including both dict keys and values. If any prohibited phrase or standalone action token remains, `UnsafeAutomaticPromotionArtifactError` is raised and **no artifact is written**.

### Prohibited content

- Multi-word phrases: `buy now`, `sell now`, `hold now`, `trim now`, `trade now`, `trim position`, `rebalance now`, `add shares`, `buy shares`, `sell shares`, `reduce shares`, `add to watchlist`, `execute trade`, `execute order`, `promote candidate`, `promote to watchlist`, `actionable buy/sell`, `validated buy/sell`, `official recommendation`, `recommend buying/selling/holding`, `i recommend`, `you should buy/sell/hold`, `consider buying/selling`.
- Standalone whole-word actions: `buy`, `sell`, `hold`, `actionable`, `promoted`, `validated`, `approved`, `trade`, `recommendation`.

### Allowed exceptions

The fixed safety disclaimers (and the "Safety Boundary" documentation block in the Markdown) are whitelisted so they can legitimately reference the forbidden tokens while explaining that the artifact is **not** such a recommendation.

## Tests

File: `tests/discovery/test_automatic_promotion_governance.py`
Count: 68 tests across 9 test classes

Coverage: input loading, sanitizer/validator, per-candidate eligibility (WATCH/MONITOR, NEEDS_REVIEW, REJECTED, EXPIRED, forbidden upstream status, replay-negative, missing ticker), report building, deterministic ordering, Markdown rendering, three-artifact writing, run-mode write blocking, JSONL append behavior, dry-run, adversarial input protection (`buy now`, `sell now`, `promote candidate`, etc., forbidden upstream statuses), no-mutation field invariants.
