# Decision Engine

Status: implemented, tested, and wired observe-only in the daily pipeline.

## Purpose

The Decision Engine is the unified advisory decision layer for the Portfolio Automation System. It converts multiple existing recommendation and signal streams into one ranked, observe-only action plan for operators, downstream UI, memo generation, and future explanation layers.

It is advisory only. It never executes trades, places orders, or changes the behavior of existing recommendation systems.

## Current State

| Item | Status |
| --- | --- |
| `portfolio_automation/decision_engine.py` | Implemented |
| `main.py` observe-only integration | Live |
| `outputs/latest/decision_plan.json` | Written by pipeline |
| `outputs/latest/decision_plan.md` | Written by pipeline |
| `tests/test_decision_engine.py` | Present |
| `tests/test_decision_engine_pipeline.py` | Present |
| Existing recommendation behavior | Unchanged |
| Existing output schemas | Preserved |

The Decision Engine is now the central observe-only action-plan layer. It adds a consolidated decision view without replacing or mutating the existing recommendation outputs.

## Inputs

The engine builds its plan from these sources:

| Source | Input | Notes |
| --- | --- | --- |
| Structural violations | guardrail violation dicts | Highest authority; used for structural `SELL` decisions |
| Portfolio adjustments | portfolio adjustment dicts | Existing portfolio advice normalized into decision records |
| Finance recommendations | scored finance recommendation dicts | Financial guidance normalized into the same plan |
| Watchlist signals | watchlist scanner result rows | Uses conviction, confidence, cooldown, and holding context |
| Market opportunities | broader market opportunity rows | Opportunity layer for contribution or underweight deployment ideas |
| Portfolio context | context dict | Includes holdings, cash, degraded mode, drawdown regime, and active structural violations |

Current context fields used include:

- `total_portfolio_value`
- `cash`
- `current_holdings`
- `degraded_mode`
- `data_mode`
- `drawdown_regime`
- `active_structural_violations`

## Outputs

The observe-only pipeline writes:

- `outputs/latest/decision_plan.json`
- `outputs/latest/decision_plan.md`

`decision_plan.json` is the machine-readable artifact. `decision_plan.md` is the operator-readable summary.

The daily memo/reporting layer now also consumes `decision_plan.json` as an additive downstream input. This does not change the Decision Engine plan shape or any upstream recommendation behavior.

The GUI Decision Center now also consumes `decision_plan.json` and `system_decision_summary.json` through the operator-data layer. This remains read-only and does not recompute decisions.

### Artifact Shape

Current top-level JSON shape:

| Field | Type | Meaning |
| --- | --- | --- |
| `generated_at` | `str` | ISO timestamp for artifact generation |
| `run_mode` | `str` | Pipeline run mode such as `daily` |
| `observe_only` | `bool` | Always `true` for this integration |
| `total_decisions` | `int` | Number of consolidated decision records |
| `decisions` | `list[dict]` | Ranked decision records |

### Decision Record Schema

| Field | Type | Meaning |
| --- | --- | --- |
| `symbol` | `str` | Ticker or logical identifier |
| `decision` | `str` | One of `BUY`, `SELL`, `SCALE`, `HOLD`, `WAIT`, `AVOID` |
| `priority` | `float` | Ranking score used for ordering |
| `urgency` | `str` | One of `critical`, `high`, `medium`, `low` |
| `source` | `str` | One of `structural`, `portfolio`, `finance`, `watchlist`, `market` |
| `recommended_action` | `str` | Operator-facing suggested action |
| `recommended_amount` | `float \| null` | Suggested dollar amount when available |
| `recommended_allocation_pct` | `float \| null` | Suggested allocation percentage when available |
| `reason` | `str` | Consolidated human-readable rationale |
| `risk_flags` | `list[str]` | Merged risk or downgrade flags |
| `confidence` | `float` | Advisory confidence value |
| `inputs_used` | `dict` | Safely merged source-specific inputs used to produce the record |

## Decision Types

The Decision Engine uses a closed decision set:

| Decision | Meaning |
| --- | --- |
| `BUY` | Open or add a new position when rules permit |
| `SELL` | Reduce or exit because structural or portfolio rules require it |
| `SCALE` | Add to an existing holding rather than opening a new one |
| `HOLD` | Do not act now; maintain current position |
| `WAIT` | Opportunity exists but current evidence or guardrails do not permit action |
| `AVOID` | Opportunity is too weak to act on |

## Source Precedence

When multiple sources produce decisions for the same symbol, the engine uses source precedence first.

| Source | Precedence |
| --- | ---: |
| `structural` | 5 |
| `portfolio` | 4 |
| `finance` | 3 |
| `watchlist` | 2 |
| `market` | 1 |

Structural decisions dominate lower-source decisions by design.

## Decision Precedence

Within the same source precedence bucket, consolidation uses decision strength to choose the winner.

| Decision | Precedence |
| --- | ---: |
| `SELL` | 5 |
| `SCALE` | 4 |
| `BUY` | 3 |
| `HOLD` | 2 |
| `WAIT` | 1 |
| `AVOID` | 0 |

This means `SCALE` wins over `BUY` during consolidation because scaling an existing position represents stronger conviction than opening a new one.

## Consolidation Rules

The Decision Engine now consolidates duplicate symbol-level decisions into one final record per symbol.

Current consolidation behavior:

- one final decision per symbol
- duplicate symbol decisions are consolidated
- generic symbols such as `PORTFOLIO` and `UNKNOWN` are skipped from symbol-level dedupe
- winner selection uses:
  1. source precedence
  2. decision precedence
  3. higher priority score
- `risk_flags` are merged with insertion-order dedupe
- `reason` values are joined when multiple non-empty reasons exist
- `inputs_used` are merged safely, with winner keys preserved

Conflict cleanup before consolidation:

- portfolio `HOLD` decisions that contradict an active structural `SELL` on the same specific symbol are suppressed
- structural leverage breaches are resolved to specific leveraged holdings when possible

Validated examples from current behavior:

- structural leverage breach maps to `QLD`
- structural concentration breach maps to `QQQ`
- conflicting portfolio `HOLD` and structural `SELL` duplicates are suppressed or consolidated

## Structural Authority Rules

Structural decisions are authoritative.

Actual current rules:

- structural violations normalize to `SELL`
- structural `SELL` decisions are never downgraded by override logic
- structural decisions outrank all lower-source decisions
- a structural `SELL` on a symbol suppresses contradictory portfolio `HOLD` on that same symbol

This keeps guardrail enforcement visible even when opportunity-oriented sources disagree.

## Override Behavior

`apply_decision_overrides` applies safety caps to non-authoritative opportunity decisions.

Current override rules:

1. `degraded_mode=True` or `data_mode="fallback"` caps `BUY` and `SCALE` to `WAIT`
2. `drawdown_regime in {"bear", "severe"}` caps non-structural `BUY` and `SCALE` to `HOLD`
3. active structural conflict on the same symbol caps `BUY` and `SCALE` to `HOLD`
4. watchlist cooldown and low-confidence handling already map upstream signal decisions to `WAIT` or `HOLD`

Important invariants:

- structural `SELL` is never downgraded
- overrides do not mutate the original input record
- downgrade reasons appear in `risk_flags`

## Example Top Decision Output

Validated top decision output from the integrated observe-only pipeline:

```text
SELL   QLD    pri=0.950 src=structural urgency=critical
SELL   QQQ    pri=0.880 src=structural urgency=high
SCALE  VFH    pri=0.550 src=portfolio urgency=low
WAIT   FANG   pri=0.550 src=market urgency=medium
WAIT   XLRE   pri=0.550 src=market urgency=medium
```

Expected interpretation:

- structural `SELL` decisions appear first
- underweight contribution targets can surface as `SCALE` or `BUY`
- lower-conviction or constrained market opportunities remain `WAIT`

## Testing Coverage

Current test coverage is split across:

- `tests/test_decision_engine.py`
  Core conversion, ranking, override, and summary behavior
- `tests/test_decision_engine_pipeline.py`
  Pipeline integration, schema isolation, serialisability, additive-output behavior, and regression checks

Validated behaviors covered by tests include:

- structural decisions outrank watchlist and market opportunities
- leverage and concentration structural mapping resolve to expected symbols
- additive `decision_plan` output does not mutate existing upstream artifacts
- output records use the closed decision schema
- the plan is JSON-serialisable
- duplicate and conflicting symbol-level decisions are handled by consolidation and suppression logic

Downstream memo coverage also validates that:

- `watchlist_scanner/daily_memo.py` reads `decision_plan.json` safely
- missing `decision_plan.json` degrades to a visible "Decision plan unavailable" message
- structural decisions appear first in memo rendering
- capital actions and structural risk focus render without changing existing memo sections
- total memo test coverage passed at `55` tests in `tests/test_daily_memo.py`

## Daily Memo Integration

Current downstream memo behavior:

| Item | Behavior |
| --- | --- |
| Input artifact | `outputs/latest/decision_plan.json` |
| Reader | `watchlist_scanner/daily_memo.py` |
| Attach point | `generate_daily_memo(...)` attaches the plan only when present |
| Missing file behavior | Memo generation continues and shows `Decision plan unavailable.` |
| Existing memo sections | Preserved |
| Existing recommendation behavior | Unchanged |

The memo layer adds these Decision Engine summaries:

- `Top Insight`
  One or two short sentences only.
- `Top Decisions`
  Top 5 ranked actions with decision, symbol, priority, source, urgency, plain-English reason, and risk flags.
- `Capital Actions`
  SELL / SCALE / BUY summary and total recommended capital when the plan provides amounts.
- `Risk Focus`
  Structural decisions first, with concentration and leverage highlighted when present.
- `What Changed`
  Maximum 3 bullets.
- `System / Data Health`
  Only when degraded or fallback conditions are active.

## GUI Decision Center Integration

Current downstream GUI behavior:

| Item | Behavior |
| --- | --- |
| Input artifacts | `outputs/latest/decision_plan.json`, `outputs/latest/system_decision_summary.json` |
| Reader | `gui_operator_data.py` |
| Renderer | `gui/app.py` Decision Center |
| Decision recomputation | None |
| Missing file behavior | GUI remains available and shows `Decision plan unavailable.` in the compact brief |
| Full detail | Remains available below the summary in tables / expanders |

The GUI compact brief mirrors the memo contract:

- `Top Insight`
- `Top Decisions`
  Maximum 5.
- `Capital Actions`
  Grouped summary only.
- `Risk Focus`
  Maximum 3.
- `What Changed`
  Maximum 3.
- `System / Data Health`
  Only when degraded or fallback conditions are active.

Validated VPS checks for the GUI compact brief:

- compile check passed
- GUI/operator-data + memo tests passed: `74 passed`
- daily pipeline preserved idempotent behavior
- required artifacts existed:
  - `outputs/latest/decision_plan.json`
  - `outputs/latest/system_decision_summary.json`
- compact brief contract returned:
  - `available: True`
  - `top_decisions: 5`
  - `risk_focus: 3`
  - `what_changed: 3`
  - `health_items: 1`

## Safety Statement

The Decision Engine is observe-only and additive-only.

It does:

- unify advisory signals into one ranked plan
- write new decision-plan artifacts
- log top decisions for visibility

It does not:

- execute trades
- override existing recommendation behavior
- rewrite current output contracts
- bypass structural guardrails

## Next Implementation Step

Reuse the shared compact contract for future AI explanation and operator-summary surfaces so memo, GUI, and explanation consumers stay contract-aligned and observe-only.
