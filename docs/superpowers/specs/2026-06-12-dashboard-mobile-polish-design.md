# Dashboard / Mobile Polish + Style Tokenization

Date: 2026-06-12
Status: Approved (brainstorm) — pending spec review → implementation plan
Owner: Enrique Pesantez
Roadmap: post-activation step 2 ("§14 dashboard/mobile polish"); `next_official_step` stays `observe_and_iterate`.

## Goal

Improve mobile responsiveness, cross-tab visual consistency, and accessibility of the
`gui_v2` persona cockpit by upgrading the SHARED component/style layer (one change → all
converged tabs inherit) and migrating the two stragglers. Per-purpose tabs are KEPT.
Template / CSS / markup only — NO data-loader, route, or behavior change. Built in an
isolated git worktree (concurrent `gui_v2` sessions are live).

## Context (current state)

- Shared component lib `gui_v2/templates/components/_ui.html` + 11 components; 9 of 11
  dashboard tabs already import `_ui`. Stragglers: `portfolio_config.html`,
  `portfolio_sync.html`.
- `base.html` has the viewport meta but a thin (~27-line) inline `<style>` with NO
  `@media` breakpoints → table-heavy tabs don't reflow on phones.
- No `StaticFiles` mount / no `static/` dir.

## Changes

1. **Tokenized style layer (folded-in out-of-scope item, per owner "include out of
   scope as long as they are improvements"):** create
   `gui_v2/templates/components/_styles.html` containing the cockpit CSS with a `:root`
   design-token block (color, spacing, radius, font-scale, and breakpoint values as CSS
   custom properties), the existing inline base CSS migrated in, new `@media`
   breakpoints, and a `.table-wrap { overflow-x: auto; }` rule. `base.html` includes it
   (`{% include "components/_styles.html" %}`) in place of the inline `<style>` block.
   - NOTE: a full external `.css` file + `StaticFiles` mount was considered and
     deliberately NOT done — at ~27 lines of CSS the infra outweighs the benefit. The
     token partial delivers the tokenization improvement with no new serving infra.
     (External static `.css` remains a clean future option if the CSS grows.)

2. **Responsive breakpoints** (in `_styles.html`): `@media (max-width: 640px)` (phone)
   and `@media (max-width: 960px)` (tablet) — collapse multi-column grids to one column,
   scale padding/fonts via tokens, reserve bottom-nav space.

3. **Responsive tables:** add a `responsive_table()` macro to `_ui.html` that wraps a
   table in `<div class="table-wrap">` (horizontal scroll on narrow screens). Apply it on
   the table-heavy tabs: `portfolio.html`, `system.html`, `portfolio_sync.html`,
   `strategy_tax.html`.

4. **Accessibility:** add to the shared components (propagates to all tabs) —
   status/severity badges get `role="status"` + `aria-label`; the desktop nav and
   `bottom_nav.html` get `<nav aria-label="...">`, `aria-current="page"` on the active
   tab, and ≥44px tap targets on bottom-nav items; consistent heading hierarchy.

5. **Consistency migration:** rewrite `portfolio_config.html` + `portfolio_sync.html` to
   import `_ui` and adopt the shared card/badge/table/observe-only-banner components +
   the tokenized styles, matching the other 9 tabs. Preserve their existing data fields
   and the gated config-edit form behavior exactly (read-only/observe-only unchanged).

## Safety / invariants

- Template/CSS/markup ONLY. No `gui_v2/data/*.py` loader changes; no routes added or
  removed; no tab merged or renamed. Observe-only banner stays on every tab.
- The gated config-edit form on `portfolio_config.html` keeps its exact behavior
  (auth + `GUI_V2_PORTFOLIO_EDIT` gating, validate→dry-run→confirm→save). Visual
  migration must not alter the form's POST action, field names, or gating.
- Built in an isolated git worktree; rebase onto `origin/main` before finalizing so
  concurrent `gui_v2` commits are integrated; the diff is template/CSS-scoped.

## Excluded (NOT improvements for this owner / outside polish)

- **Tab consolidation** — the owner explicitly prefers one tab per purpose; merging tabs
  would be a regression for them, so it is excluded despite being "out of scope."
- **Full static `.css` + `StaticFiles` mount** — infra exceeds benefit at this CSS size.
- **Data-loader / perf rewrites and new visual features** — outside a polish pass; no
  clear benefit, added risk.

## Verification (mechanical — no access to the rendered UI)

`tests/test_gui_polish.py` (new):
- Every `/dashboard/*` route returns 200 (or 401 if auth env set) via `TestClient` —
  never 500.
- Every dashboard template imports `_ui` (grep/asserts), including the 2 migrated.
- `_styles.html` contains a `:root` token block, both `@media` breakpoints, and
  `.table-wrap`.
- `base.html` includes `_styles.html` and no longer carries the bulk inline `<style>`.
- Nav markup has `aria-label` + `aria-current`; severity/status badges carry
  `role`/`aria-label`.
- Table-heavy tabs render the `table-wrap` wrapper.
- `git diff` confirms zero changes under `gui_v2/data/`.

Honest limitation: these prove no breakage + responsive/a11y/consistency scaffolding is
present and uniform — they do NOT confirm visual quality. Final "looks right on my
phone" check is the operator's after `sudo systemctl restart stockbot-dashboard.service`.

## Rollback

Template/CSS-only; revert the commits. No data migration. The cockpit's behavior is
unchanged, so rollback risk is minimal.
