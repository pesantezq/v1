# Dashboard / Mobile Polish (Tailwind-idiomatic)

Date: 2026-06-12
Status: Approved (brainstorm, revised after Tailwind discovery) — pending plan → implementation
Owner: Enrique Pesantez
Roadmap: post-activation step 2 ("§14 dashboard/mobile polish"); `next_official_step` stays `observe_and_iterate`.

## Goal

Improve cross-tab consistency and mobile table usability of the `gui_v2` persona cockpit
by (1) migrating the two non-`_ui` tabs onto the shared macro library and (2) adding a
responsive-table wrapper applied to the dense-table tabs, plus a small a11y touch on the
shared badges. Per-purpose tabs KEPT. Template/markup only — NO data-loader, route, or
behavior change. Built in an isolated git worktree (concurrent `gui_v2` sessions live).

## Context (corrected — this is a Tailwind app)

The cockpit uses **Tailwind CSS via CDN** + a shared Jinja macro library
(`components/_ui.html`). Responsive layout is already handled by Tailwind breakpoint
utilities (`sm:`/`md:` — e.g. `main` uses `px-4 sm:px-6`, desktop nav `hidden md:flex`,
`bottom_nav` `md:hidden` with ≥52px tap targets). Accessibility is already decent
(`aria-current="page"`, `role="status"`, `aria-label` on controls). 9 of 11 dashboard
tabs already `import "_ui.html"`; stragglers: `portfolio_config.html`,
`portfolio_sync.html`.

Consequence: a `:root` design-token CSS layer / raw `@media` breakpoints (an earlier
draft idea) would FIGHT Tailwind and is explicitly NOT done. The design system here is
**Tailwind utilities + the `_ui` macros**; polish means converging on them.

## Changes

1. **`components/_ui.html` — add `responsive_table()` macro** that wraps table markup in
   a horizontally-scrollable container using Tailwind:
   ```jinja
   {% macro responsive_table() %}
   <div class="overflow-x-auto -mx-2 sm:mx-0">
     <div class="inline-block min-w-full align-middle px-2 sm:px-0">
       {{ caller() }}
     </div>
   </div>
   {% endmacro %}
   ```
   Usage: `{% call ui.responsive_table() %}<table>...</table>{% endcall %}`.

2. **`components/_ui.html` — a11y on the badge macros.** Add `role="status"` +
   `aria-label="{{ (label or severity) }}"` to `status_badge` and `aria-label` to
   `badge`, without changing their visual classes.

3. **Apply `responsive_table()`** around the primary `<table>` (or wide grid) on the
   dense-table tabs: `portfolio.html`, `system.html`, `portfolio_sync.html`,
   `strategy_tax.html`. (These are the 200+ line table-heavy tabs that overflow on
   phones.)

4. **Consistency migration** — rewrite `portfolio_config.html` + `portfolio_sync.html`
   to `{% import "components/_ui.html" as ui %}` and adopt the shared macros
   (`page_header`, `status_badge`/`badge`, `sev_dot`, card patterns) used by the other 9
   tabs, matching their look. **Preserve exactly**: `portfolio_sync` read-only data
   fields; `portfolio_config`'s gated edit form (the `components/portfolio_edit_form.html`
   include, its POST action, field names, and auth + `GUI_V2_PORTFOLIO_EDIT` gating must
   be untouched — only the surrounding presentation changes).

## Safety / invariants

- Template/markup ONLY. No `gui_v2/data/*.py` loader changes; no routes added/removed; no
  tab merged/renamed. Observe-only banner stays on every tab.
- The `portfolio_config` gated edit form keeps its exact behavior (validate → dry-run →
  confirm → save; auth + env gating). The migration is visual only.
- Built in an isolated git worktree; rebase onto `origin/main` before finalizing so
  concurrent `gui_v2` commits integrate; diff stays template-scoped.

## Excluded (NOT improvements for this codebase / owner)

- **`:root` design-token CSS / raw `@media` breakpoints** — would conflict with Tailwind
  (anti-pattern); Tailwind already provides responsive utilities.
- **External `.css` + `StaticFiles` mount** — unwarranted; Tailwind is CDN-served.
- **Tab consolidation** — owner prefers per-purpose tabs.
- **Data-loader / perf rewrites, new visual features** — outside a polish pass.

## Verification (mechanical — no rendered-UI access)

`tests/test_gui_polish.py` (new):
- Every `/dashboard/*` route returns 200 (or 401 if auth env set) via `TestClient` —
  never 500.
- `portfolio_config.html` and `portfolio_sync.html` both `import "components/_ui.html"`.
- `_ui.html` defines a `responsive_table` macro; the 4 dense-table tabs reference
  `responsive_table` (or render an `overflow-x-auto` wrapper).
- `status_badge`/`badge` macro output contains `role="status"`/`aria-label`.
- `portfolio_config.html` still includes `components/portfolio_edit_form.html` and the
  edit form's POST action/field names are unchanged (grep/asserts).
- `git diff` confirms zero changes under `gui_v2/data/`.

Honest limitation: tests prove no breakage + consistency/responsive-wrapper presence,
not visual quality. Final phone eyeball is the operator's after
`sudo systemctl restart stockbot-dashboard.service`.

## Rollback

Template-only; revert the commits. Behavior unchanged → minimal rollback risk.
