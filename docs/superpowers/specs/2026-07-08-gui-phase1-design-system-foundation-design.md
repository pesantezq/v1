# GUI Improvements — Phase 1: Design-System Foundation

**Date:** 2026-07-08
**Status:** approved (design), pending spec review
**Owner change scope:** `gui_v2/` presentation layer only. Observe-only consumer.
No decision/scoring/allocation/broker code. No artifact schema changes.

---

## Program context (why Phase 1 first)

"Continue with GUI improvements" is a program of four sub-projects, sequenced so
each phase makes the next cheaper ("build forward"):

1. **Phase 1 — Design-system foundation (this spec).** Consolidate the shared
   primitive layer (`components/_ui.html` macros + overlapping standalone
   components) into one coherent, documented set, with a11y/mobile baked into the
   primitives. Everything else renders through it.
2. **Phase 2 — Close data-surfacing gaps.** Surface shipped-but-unrendered
   backend data (memo recovery caveat / peak context, quant-watch probes,
   weekly-deployment detail, any `outputs/latest/*.json` with no consumer) using
   Phase-1 primitives.
3. **Phase 3 — Per-tab deep-dives.** Make the highest-value tabs (Today →
   Portfolio → Memo) best-in-class on the solid primitives + complete data.
4. **Phase 4 — Cross-cutting a11y / mobile / perf pass.** Mostly verification by
   then, plus render/poll-cost tuning.

Each phase is its own spec → plan → PR and is independently shippable. This spec
covers Phase 1 only.

## Guiding principle

**Pixel-preserving consolidation.** Phase 1 changes *how* live markup is
produced, not how it looks. The PR4 visual-regression guards must stay green;
any visual diff is a bug to fix, not an intended change. Intentional restyling is
deferred to Phase 3. This keeps Phase 1 low-risk and the VR guards meaningful.

## Current state (verified against code 2026-07-08)

- `app.py` renders `dashboard/*.html` for the 10 persona tabs, plus top-level
  `operator.html`; `base.html` is the shell.
- **Six top-level templates are unrouted dead code:** `today.html`,
  `portfolio.html`, `research.html`, `operations.html`, `risk_impact.html`,
  `health.html` (app.py renders the `dashboard/` versions instead; 0 references
  in `app.py`).
- Standalone components `metric_card.html` and `severity_badge.html` are used
  **only** by those dead legacy templates. `source_artifact_label.html` has **0**
  consumers.
- Standalone `empty_state.html` is used by live `dashboard/portfolio_sync.html`
  (and dead legacy) while `_ui.html` already defines an `empty_state` macro —
  two implementations of the same primitive.
- Standalone `evidence_drawer.html` is used by live `portfolio_config` +
  `portfolio_sync`; `_ui.html` has an `evidence` macro — needs reconciliation.
- Hand-rolled severity color literals (`bg-emerald-500/15`, `text-rose-300`,
  etc.) appear outside `_sev_classes` in 7 live tabs: `crowd_radar`,
  `portfolio_config`, `memo`, `system`, `portfolio`, `strategy_lab`,
  `strategy_tax`.
- Unwrapped `<table>`s (not inside `ui.responsive_table()`): `strategy_lab` (5),
  and one each in `crowd_radar`, `portfolio`, `quant`.

## Scope — work items

### 1. Delete dead code
- Remove unrouted legacy top-level templates: `gui_v2/templates/today.html`,
  `portfolio.html`, `research.html`, `operations.html`, `risk_impact.html`,
  `health.html`. Keep `base.html` and `operator.html`.
- Remove now-orphaned standalone components:
  `gui_v2/templates/components/metric_card.html`, `severity_badge.html`,
  `source_artifact_label.html`.
- Pre-delete guard: for each file, re-confirm 0 references in `gui_v2/app.py`,
  no `{% include %}` / `{% import %}` / `render_template` reference anywhere in
  `gui_v2/`, and no test asserts on it. If any reference exists, migrate that
  consumer first.

### 2. Collapse duplicate primitives onto `_ui` macros (live templates)
- Migrate live consumers of standalone `empty_state.html` (`portfolio_sync`) to
  `ui.empty_state`; then delete standalone `empty_state.html`.
- Reconcile `evidence_drawer.html` vs `ui.evidence`: if functionally
  equivalent, migrate `portfolio_config` + `portfolio_sync` to `ui.evidence` and
  delete the standalone; if it is genuinely richer (e.g., drawer semantics),
  keep it, add a header comment stating why it is distinct from `ui.evidence`,
  and leave a note in the reference doc. Decision made at implementation time
  after reading both.
- End state: exactly one implementation per primitive.

### 3. Kill severity-token drift
- In the 7 live tabs, replace hand-rolled severity color literals with the
  single-source macros (`ui._sev_classes`, `ui.status_badge`, `ui.sev_dot`,
  `ui.badge`). Surgical: only severity-*semantic* colors (green=ok / amber=warn
  / red / blue=info / gray=unknown). Do **not** touch non-semantic accent colors
  (e.g., a green Refresh button chrome) — those are not severity signals.
- Where a template computes severity inline, keep that logic; only the
  class-string emission moves to the macro.

### 4. Table responsiveness
- Wrap every remaining raw `<table>` not already inside `ui.responsive_table()`
  in it: `strategy_lab` (5), `crowd_radar` (1), `portfolio` (1), `quant` (1).
- Wrapping must not change desktop layout (the wrapper is `overflow-x-auto`
  with `min-w-full`); verify no visual diff on wide viewport.

### 5. a11y baseline into the primitives
- Add a visible `focus-visible` ring to interactive macro elements (the
  `page_header` Refresh button; `<summary>` disclosures in `evidence`).
- Confirm `aria-label` / `role` present on `status_badge` / `badge` (already
  there) and add where missing on newly-consolidated interactive elements.
- Keep contrast changes out of scope unless a token fails WCAG AA on inspection;
  deeper a11y is Phase 4.

### 6. Documentation
- Update `docs/GUI_V2_REFERENCE.md` §6 (Components & styling) to reflect the
  single primitive source and the removed standalone components; add a one-line
  changelog row in §9.

## Out of scope (Phase 1)
- Any intentional visual restyle, new layout, or new card type (Phase 3).
- Surfacing new backend data / new artifacts (Phase 2).
- Deep a11y (full ARIA landmarks, keyboard nav audit) and perf tuning (Phase 4).
- Any change to collectors (`data/dash_*.py`), routes, or the observe-only
  invariants.

## Testing & safety
- Run the full GUI test suite (`pytest -q gui_v2` / the repo's GUI test modules;
  317+ tests) — must stay green.
- Run the PR4 visual-regression guards — must stay green (pixel-preserving).
  Any diff is investigated and fixed, not baselined away, unless it is a
  provably-correct token fix (e.g., an `amber`-as-gray bug), which is then
  re-baselined with a note.
- Add/adjust tests where a migration changes an import path, a macro call, or
  removes a template a test referenced.
- `python -m py_compile` is N/A for templates; instead assert templates render
  via existing route tests.

## Acceptance criteria
1. The 6 dead legacy templates and 3 orphan components are removed; `grep` finds
   no dangling references in `gui_v2/`.
2. Each shared primitive (card, stat, badge/severity, empty-state, evidence,
   section/page header, table wrapper) has exactly one implementation, in
   `components/_ui.html` (or a single documented standalone if intentionally
   kept).
3. No hand-rolled severity color literal remains in the 7 live tabs (all route
   through `_sev_classes` / macros).
4. Every `<table>` in the live dashboard templates is wrapped in
   `ui.responsive_table()`.
5. Interactive macro elements have a visible focus ring.
6. Full GUI suite + visual-regression guards green; `GUI_V2_REFERENCE.md`
   updated.
7. Observe-only invariants unchanged: no new writes to `decision_plan.json`,
   trade verbs only in decision-core cards, banner intact.

## Risks
- **Shared-primitive blast radius.** A macro change touches many tabs. Mitigation:
  pixel-preserving principle + VR guards + full suite; migrate one primitive /
  one tab per commit for a clean bisect.
- **Hidden legacy reference.** A template thought dead is still reachable.
  Mitigation: the item-1 pre-delete guard (grep + tests) before each removal.
- **Severity-literal false positives.** A green that is chrome, not a severity
  signal, gets wrongly macro-ized. Mitigation: only migrate colors tied to a
  severity value; leave static accents.

## Rollback
Pure presentation-layer, git-reversible. No data migration, no artifact writes,
no schema change. Revert the branch to roll back.
