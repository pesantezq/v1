# Issue: memo page emits a wide `<table>` (mobile-first contract regression)

**Status:** OPEN — tracked (was an informal known exception; promoted to a bounded issue 2026-07-01)
**Severity:** low (UX/mobile-layout; no decision, scoring, or data impact)
**Failing test:** `tests/test_gui_dashboard_memo.py::test_memo_route_no_wide_table`
**Surfaces:** `/dashboard/memo`

## Symptom

```
AssertionError: Wide <table> element found in memo page — sections should be stacked divs/sections
'<table' is contained here:
    <table class="w-full text-sm border-collapse">
```

The memo route is contractually **mobile-first**: `test_memo_route_no_wide_table`
asserts the rendered HTML contains no `<table>` element (sections must be stacked
`<div>`/`<section>` so they reflow on narrow screens).

## Root cause (identified)

`gui_v2/templates/dashboard/memo.html` imports the operator panel:

```jinja
{% import "components/operator_panel.html" as op %}
```

`gui_v2/templates/components/operator_panel.html` renders a
`<table class="w-full text-sm border-collapse">` (the operator work-order list).
When that panel is rendered on the memo page, the literal table trips the
no-wide-table contract. This is a **template regression** — it is NOT
data-dependent (verified: the class string is a template literal, and the test
fails independently of the working-tree's dirty daily outputs).

It landed alongside the recent memo-surface changes (memo coherence layer +
monthly capital envelope + operator panel embedding); the surfaces "changed
substantially," so the informal exception should not persist.

## Recommended fix (bounded — pick one, do not expand scope)

1. **Preferred:** wrap the operator-panel table in a horizontally-scrollable
   container (`<div class="overflow-x-auto">…</div>`) OR convert it to the
   existing responsive pattern (`components/_ui.html` `responsive_table()` /
   stacked-card fallback used elsewhere), then relax the test to assert
   *no non-scrollable* wide table (allow `<table>` inside `overflow-x-auto`) —
   consistent with how other dashboard pages already scroll wide tables.
2. Alternatively, gate the operator panel out of the *memo* route's mobile view
   (render it only on the operator page), keeping the memo page pure stacked
   sections.

## Acceptance

- `test_memo_route_no_wide_table` passes (or is updated to the
  scrollable-table contract) AND `test_memo_route_has_stacked_sections` still
  passes.
- No change to `dash_memo.py` data, memo content, or any decision/score artifact.

## Notes

- Not caused by the SQG program work (2026-07-01) — proven pre-existing by
  stashing the SQG changes and reproducing the failure.
- Until fixed, `preflight.sh` remains green (its FMP-focused suite does not
  include this GUI test), so this does not block the daily pipeline.
