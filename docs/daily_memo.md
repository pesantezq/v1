# Daily Memo

Last verified against code on 2026-04-28.

## Purpose

The daily memo layer converts portfolio-system outputs into operator-readable text and Markdown summaries.

It is a reporting surface, not a decision engine. It reads existing artifacts, summarizes them, and preserves the advisory-only boundary of the system.

## What Was Built

The daily memo layer now includes additive Decision Engine coverage.

Current integration behavior:

- `watchlist_scanner/daily_memo.py` safely reads `outputs/latest/decision_plan.json`
- `generate_daily_memo(...)` attaches the decision plan only when the artifact is present
- no `decision_engine.py` logic was changed
- no recommendation logic was changed
- missing `decision_plan.json` is handled gracefully

## Why It Matters

This integration gives operators one memo that now includes both the existing system summary and the current ranked decision plan.

Practical benefits:

- structural risks are visible in the same report as opportunities
- capital actions are easier to review without opening raw JSON
- memo consumers can see the top observe-only actions without changing recommendation behavior
- future GUI and explanation layers can align to the same plan summary contract

## Input Artifact Contract

The memo layer still depends on `outputs/latest/system_decision_summary.json` as its primary source.

Decision Engine integration adds one optional additive input:

| Artifact | Required | Role |
| --- | --- | --- |
| `outputs/latest/system_decision_summary.json` | Yes | Existing memo summary source |
| `outputs/latest/decision_plan.json` | No | Additive Decision Engine input for ranked action summaries |

### Expected Decision Plan Shape

The memo layer expects a top-level object with:

- `generated_at`
- `run_mode`
- `observe_only`
- `total_decisions`
- `decisions`

Each row in `decisions` is expected to support these memo-facing fields when present:

| Field | Use in memo |
| --- | --- |
| `decision` | action label in Top Decisions and Capital Actions |
| `symbol` | symbol display |
| `priority` | ranked priority shown to operators |
| `source` | source attribution |
| `urgency` | urgency display |
| `reason` | plain-English explanation |
| `risk_flags` | operator-visible caution flags |
| `recommended_amount` | capital summary when available |
| `inputs_used.violation_type` | structural concentration / leverage highlighting |

The memo layer treats the artifact as read-only input and does not attempt to repair or reinterpret Decision Engine semantics.

## Output Behavior

The memo is a brief decision-focused summary, not a data dump.

Current section budget:

- `Top Insight`
  One or two short sentences only.
- `Top Decisions`
  Maximum 5 ranked decisions.
- `Capital Actions`
  Grouped SELL / SCALE / BUY summary only.
- `Risk Focus`
  Maximum 3 items.
- `What Changed`
  Maximum 3 bullets.
- `System / Data Health`
  Shown only when degraded or fallback conditions are active.

Memo constraints:

- do not dump full watchlist outputs
- do not include full score breakdowns
- do not expose raw JSON fields
- do not include more than 5 decisions
- do not list low-priority or suppressed items beyond the top ranked set
- prefer grouping over long enumerations
- full detail remains in JSON artifacts and GUI surfaces

### Top Decisions

Shows the top 5 decision rows, including:

- decision or action
- symbol
- priority
- source
- urgency
- plain-English reason
- risk flags when present

### Capital Actions

Summarizes action-bearing decisions:

- `SELL`
- `SCALE`
- `BUY`

When `recommended_amount` values exist, the memo also reports the total recommended capital amount across those actions.

### Risk Focus

Prioritizes structural risk visibility:

- structural decisions are summarized first
- concentration risk is called out when present
- leverage risk is called out when present

This keeps guardrail-driven actions visible in the operator memo even when lower-priority opportunities also exist.

### What Changed

Summarizes only the highest-signal recent changes:

- up to 3 bullets
- favors explicit change items over verbose summary metadata
- avoids replaying the full summary artifact

## Missing-File Behavior

Decision Engine memo support is intentionally non-blocking.

If `outputs/latest/decision_plan.json` is missing:

- memo generation still succeeds
- the compact memo still renders
- the new decision section reports `Decision plan unavailable.`

This prevents memo/reporting regressions when the decision-plan artifact is absent, delayed, or intentionally disabled.

## Observe-Only Boundary

The daily memo remains a reporting-only layer.

It does:

- read additive artifacts
- summarize ranked decisions
- surface structural risks and capital suggestions for review

It does not:

- execute trades
- alter Decision Engine logic
- change recommendation outputs
- mutate upstream artifacts
- bypass observe-only or advisory-only constraints

## Test Coverage

`tests/test_daily_memo.py` was updated to validate the new integration.

Covered behaviors include:

- missing decision-plan file handled gracefully
- valid decision-plan file rendered into memo output
- top decisions truncated to 5
- structural decisions appearing ahead of lower-priority actions
- capital action totals rendering when amounts are available
- degraded-only health section rendering
- removal of legacy verbose memo sections

Validation result:

- `55` tests passed

## Recommended Extension

The next clean extension is to reuse the same decision-plan helper layer for a future GUI Decision Center.

That keeps:

- memo output
- GUI decision summaries
- AI explanation summaries

aligned to the same observe-only decision-plan contract and fallback behavior.

## Next Implementation Step

Promote the current memo helper logic into a shared read-only presentation helper for Decision Engine consumers so the memo and future GUI Decision Center stay consistent without duplicating formatting or missing-file handling.
