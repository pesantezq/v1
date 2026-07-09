# GUI Improvements — Phase 4: Cross-Cutting a11y / mobile / perf

**Date:** 2026-07-09
**Status:** implemented
**Owner change scope:** `gui_v2/` presentation layer only. Observe-only consumer.

---

## Program context

Final phase of the four-phase GUI program. Per the program plan, Phase 4 is
"mostly verification by then, plus render/poll-cost tuning" — Phases 1-3 baked
a11y/mobile into the primitives, so this phase verifies coverage and closes the
one real cross-cutting gap found.

## Verification pass (2026-07-09)

- **Poll cost (perf):** 1 tab polls at 60s (Today), 9 at 120s. New Phase 2-3
  loaders read small JSONs (`decision_triage`, `memo_coherence`) once per poll —
  negligible added cost. No change needed.
- **Mobile overflow:** no fixed-width (`w-[NNNpx]`) elements outside the
  `max-w-[1600px]` page container. New Phase 2-3 sections use responsive grids
  (`grid-cols-1 … lg:grid-cols-3`) and per-table `overflow-x-auto`. Clean.
- **a11y focus:** Phase 1 added focus-visible rings to the two shared `_ui`
  macros, but ~30 hand-rolled buttons/links/summaries across 10 templates had no
  keyboard-focus indicator.

## Change — this PR

- **Global `:focus-visible` rule** in `base.html`'s `<style>` block: every
  natively-focusable element (`a`, `button`, `summary`, `[tabindex]`, form
  controls) gets an emerald keyboard-focus outline. One rule covers every tab
  without editing 10 templates or rebuilding the purged Tailwind `app.css`.
  `:focus-visible` keeps it keyboard-only; elements with a bespoke Tailwind ring
  still paint theirs on top (the outline is a floor). Works in both themes.
- **Memo funding-grid null-guard** (Phase-3 review follow-up): the funding grid
  now shows if ANY of value/cash/reserve is present, with each cell individually
  guarded, instead of gating all three on `portfolio_value`.

## Tests

- `tests/test_gui_a11y_global_focus.py` (2)

## Program complete

Phases 1-4 shipped. Remaining backlog (explicitly deferred, not part of this
program): `news_intelligence` surface, per-decision triage badges, discovery
funnel + `pipeline_wiring_status` System-tab surfaces.
