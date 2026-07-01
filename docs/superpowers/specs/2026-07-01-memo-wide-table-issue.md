# Issue: memo page emits a wide `<table>` (mobile-first contract regression)

**Status:** RESOLVED 2026-07-01 (branch `fix/gui-responsive-quant-polish`) — the
root cause was the **stale absolute test contract**, not an unsafe table. The
operator work-order table was already scroll-contained (`overflow-x-auto`
ancestor), so it is a legitimate responsive table; the test wrongly rejected all
tables. Fixed by (a) standardizing the operator queue on the `ui.responsive_table()`
macro and (b) replacing `test_memo_route_no_wide_table` with a semantic
`test_memo_route_tables_are_responsive` that allows tables inside an
`overflow-x-auto` ancestor and only fails uncontained wide tables (stdlib
`HTMLParser` helper — no new dependency). See Resolution below.
**Severity:** low (UX/mobile-layout; no decision, scoring, or data impact)
**Original failing test:** `tests/test_gui_dashboard_memo.py::test_memo_route_no_wide_table` (removed)
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

## Resolution (2026-07-01, `fix/gui-responsive-quant-polish`)

Root cause confirmed: **stale absolute test contract**. The production table was
already inside `overflow-x-auto` (a legitimate responsive/contained table); the
old assertion `"<table" not in r.text` rejected *all* tables including safe ones.
This was never a real mobile-layout defect.

Changes:
- `gui_v2/templates/components/operator_panel.html` — `_queue` now wraps its table
  in `{% call ui.responsive_table() %}` (inside the rounded-border container),
  standardizing on the shared macro instead of a hand-written `overflow-x-auto` div.
  All queue columns, report links, badges, empty state, and POST actions preserved.
- `tests/test_gui_dashboard_memo.py` — `test_memo_route_no_wide_table` replaced with
  `test_memo_route_tables_are_responsive` (semantic: every `<table>` must have an
  ancestor whose class contains `overflow-x-auto`), plus positive/negative helper
  unit tests. Uses a small stdlib `HTMLParser` — no BeautifulSoup / new dependency.
  Module docstring updated to say the memo permits only responsive/contained tables.
- `test_memo_route_has_stacked_sections` kept.

Decision/scoring/artifact behavior untouched; operator queue not removed.

## Notes

- Not caused by the SQG program work (2026-07-01) — proven pre-existing by
  stashing the SQG changes and reproducing the failure.
- `preflight.sh` remained green throughout (its FMP-focused suite does not
  include this GUI test), so this never blocked the daily pipeline.
