# GUI Decision Center

Status: implemented and verified in v1. Last verified against code and VPS validation on 2026-04-29.

## Purpose

The GUI Decision Center is the operator-facing visual summary for the latest advisory decision plan.

It exists to answer two questions quickly:

- What are the highest-priority observe-only actions right now?
- What risks or capital actions need attention before reviewing full detail?

It is read-only. It does not execute trades, mutate artifacts, or recompute decisions.

The page starts with an explicit observe-only banner:

- `Observe-only decision plan. No trades are executed.`

## Input Artifacts

The GUI compact summary reads these artifacts only:

| Artifact | Role |
| --- | --- |
| `outputs/latest/decision_plan.json` | Primary ranked decision-plan source of truth |
| `outputs/latest/system_decision_summary.json` | Supplemental summary context for insight, change, and health messaging |

Reader path:

- `gui_operator_data.py`
  Normalizes the artifact payloads into a GUI-ready compact brief.
- `gui/app.py`
  Renders the compact brief first, then leaves detailed decision tables and expanders below it.

## Compact Summary Contract

The GUI mirrors the compact daily memo contract.

Required top-of-panel sections:

1. `Top Insight`
   One or two short sentences only.
2. `Top Decisions`
   Maximum 5 ranked decisions.
3. `Capital Actions`
   Grouped SELL / SCALE / BUY summary only.
4. `Risk Focus`
   Maximum 3 items.
5. `What Changed`
   Maximum 3 bullets.
6. `System / Data Health`
   Shown only when degraded or fallback conditions are active.

Final v1 layout:

1. observe-only banner
2. compact `Decision Summary`
3. `Full Decision Plan Queue` below the summary

Rules:

- do not dump full watchlist outputs into the compact brief
- do not show full score breakdowns in the summary block
- do not expose raw JSON field listings
- do not show more than 5 top decisions
- do not surface low-priority overflow items in the compact brief
- prefer grouped summaries over long enumerations

## Top Decisions Row Format

Each compact decision row renders as:

`ACTION SYMBOL | source | urgency | pri X.XXX`

followed by one short human-readable reason line.

Examples:

- `SELL QLD | structural | critical | pri 0.950`
  `Leverage exceeds cap (17.8% vs 15%).`
- `SELL QQQ | structural | high | pri 0.880`
  `Concentration exceeds cap (55.2% vs 40%).`
- `SCALE VFH | portfolio | low | pri 0.550`
  `Drift exceeds rebalance threshold.`
- `WAIT FANG | market | medium | pri 0.550`
  `Relative strength near highs.`

## Reason Formatting

The compact summary does not dump raw long reasons. It formats them for scanability.

Current compact mappings:

| Input pattern | Compact reason |
| --- | --- |
| structural leverage breach | `Leverage exceeds cap (current vs cap).` |
| structural concentration breach | `Concentration exceeds cap (current vs cap).` |
| rebalance / drift | `Drift exceeds rebalance threshold.` |
| relative strength | `Relative strength near highs.` |
| momentum / market breakout | `Momentum breakout near highs.` |
| generic fallback | first cleaned sentence, capped and word-safe |

Rules:

- do not truncate raw text mid-number or mid-word
- do not show artifacts like `...(+2`
- keep compact reasons short and complete
- keep raw long reasons in the full queue only

## Detailed Queue Behavior

The compact summary does not replace detail.

Detailed decision rows remain available below the summary through:

- the existing detailed decision table
- symbol-level inspection controls
- a dedicated full decision-plan queue expander/table

Current run examples may show dozens of rows in the full queue. For example, a validated run exposed `41` total decisions below the compact brief while keeping only the top `5` in the summary.

This separation keeps the first screen skimmable while preserving artifact-level visibility for operators.

## Read-Only Boundary

The GUI Decision Center:

- reads artifacts only
- does not recompute decision rankings
- does not recompute compact decision semantics
- does not alter Decision Engine logic
- does not alter recommendation behavior
- does not execute trades

`decision_plan.json` remains the full source of truth.

## Relationship To Daily Memo

The GUI compact brief and the daily memo share the same presentation intent:

- same capped `Top Decisions`
- same capped `Risk Focus`
- same capped `What Changed`
- same degraded-only `System / Data Health`
- same observe-only boundary
- same preference for short human-readable reasons instead of raw structural paragraphs

The memo is optimized for text/email delivery. The GUI is optimized for operator review with deeper drill-down below the compact brief.

## VPS Validation

Validated on VPS:

- compile check passed
- GUI/operator-data + memo tests passed: `74 passed`
- daily pipeline preserved idempotent behavior
- required artifacts existed:
  - `outputs/latest/decision_plan.json`
  - `outputs/latest/system_decision_summary.json`

Validated compact brief result:

- `available: True`
- `top_decisions: 5`
- `risk_focus: 3`
- `what_changed: 3`
- `health_items: 1`

Interpretation:

- the top-decision cap is working
- risk focus is capped correctly
- change summarization is capped correctly
- degraded/fallback-only health visibility is working
- the compact summary is scan-friendly while the full queue remains available below it

## Manual Visual Validation Checklist

Open:

- `Dashboard`
- `Advanced`
- `Decision Center`

Confirm:

- compact summary appears first
- full queue is available below the summary
- no duplicate or overflow items appear in the compact brief
- structural items lead the summary when present
- system/data health is absent during normal runs and present only during degraded/fallback runs
- top decision reasons are short and human-readable, not raw structural paragraphs

## Next Implementation Step

Treat GUI Decision Center v1 as complete and build the next AI Explanation Layer from `decision_plan.json` without changing Decision Engine semantics or read-only GUI behavior.
