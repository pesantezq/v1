# GUI v2 — Design Spec

**Date:** 2026-05-15
**Status:** approved sections; awaiting spec-file review before implementation planning
**Scope:** MVP — replace the existing 7000-line `gui/app.py` (Streamlit) with a
new artifact-driven dashboard built on FastAPI + HTMX + Jinja2 + Tailwind.
Two pages fully built (Today, Health); three stubs (Portfolio, Research,
Operations). Streamlit keeps serving everything else during transition.

## Background

The existing Streamlit dashboard at `gui/app.py` is 7000+ lines with 61
render functions and 13 top-level pages. Operator pain points span all
four dimensions:

- **Visual / UX design** — dated layout, cluttered information hierarchy
- **Information architecture** — too many tabs; pages that should be merged
  are separate
- **Code organization** — one massive file; hard to navigate and maintain
- **Framework choice** — Streamlit's interaction model fights the workflow

A complete rebuild is warranted. The existing GUI remains in production
during the transition: both services run simultaneously, and the operator
retires Streamlit only after the new GUI proves itself.

## Hard constraints

These cannot be relaxed under any circumstance:

- The new GUI is a **strict read-only consumer** of `outputs/*` artifacts.
  No writes anywhere. No env mutation. No business-logic computation.
- **No changes** to scoring, allocation, recommendation, decision-engine, or
  any writer. The daily pipeline is untouched.
- **No broker integration, no auto-trading.** Advisory-only stays advisory-only.
- The new GUI runs in its own systemd unit, independent of the daily timer
  and the Streamlit unit. A failure in the new GUI cannot affect either.
- No imports from `gui/` (the Streamlit package). Clean separation lets us
  retire Streamlit independently.

## Architecture

### Stack

- **FastAPI** — Python web framework (matches existing stack)
- **Jinja2** — HTML templates (server-rendered)
- **HTMX** — declarative AJAX via HTML attributes (via CDN; no JS build)
- **Tailwind CSS** — utility-first styling (via CDN in MVP)
- **markdown** — Python library, server-side Markdown → HTML for the memo

### Process model

Single `uvicorn` process. New systemd unit `stockbot-dashboard.service` on
port `8502`. Runs alongside `stockbot-streamlit.service` (port `8501`).
Both run simultaneously. Operator picks which URL.

### File layout

New `gui_v2/` package leaves the existing `gui/` Streamlit package
untouched:

```
gui_v2/
  __init__.py
  app.py                    # FastAPI application + routes
  data/                     # Pure data-collection layer
    __init__.py
    today.py                # collect_today_view(repo_root) -> dict
    portfolio.py            # collect_portfolio_stub(repo_root) -> dict
    research.py             # collect_research_stub(repo_root) -> dict
    health.py               # collect_health_view(repo_root) -> dict
    operations.py           # collect_operations_stub(repo_root) -> dict
  templates/                # Jinja2 templates
    base.html               # shared shell: nav, theme, footer
    today.html
    portfolio.html
    research.html
    health.html
    operations.html
    components/             # reusable widgets
      severity_badge.html
      metric_card.html
      data_table.html
      empty_state.html
  static/
    favicon.svg
    (style.css optional, deferred until compiled Tailwind is needed)
```

### Two-layer separation

Mirrors the pattern of the new Production Health page (`gui/production_health_page.py`):

- `gui_v2/data/<page>.py` — pure functions. No Streamlit, no FastAPI, no
  HTML. Read artifacts, return dicts. Fully unit-testable.
- `gui_v2/app.py` — FastAPI route handlers. Call the data layer, pass the
  dict to Jinja2, return HTML.

The data layer **never raises**. Internal failures become per-section
`{"error": "..."}` keys in the returned dict. This is the same invariant
honored by `collect_production_health()`.

### Module boundaries

- `gui_v2/` imports from:
  - `portfolio_automation.artifacts_registry`
  - `portfolio_automation.env`
  - `tools.status`
  - `tools.smoke_test`
  - `gui_operator_data` (reuses existing loaders where applicable)
- `gui_v2/` does **not** import from `gui/` (Streamlit). Independent
  retirement paths.
- No top-level dependency added to the daily pipeline: `fastapi`,
  `uvicorn`, `jinja2`, `markdown` are only required when the dashboard
  service runs.

## Data flow per request

```
Browser GET /
  └→ FastAPI route handler
       └→ gui_v2.data.today.collect_today_view(repo_root)
            (reads outputs/latest/{pipeline_run_status,decision_plan,
             daily_memo.md,market_opportunities,system_decision_summary}.json,
             returns a dict; never raises)
       └→ Jinja2 renders templates/today.html with the dict
       └→ HTML response
            └→ HTMX swaps fragments on user interactions (Refresh button,
               auto-refresh every 60s, etc.)
```

## Per-page content (MVP)

| Page | Route | Type | Content |
|------|-------|------|---------|
| Today | `/` | Full | Header (date, run_id, success badge). Top 5 decisions (action, symbol, priority, reason). Capital actions totals (SELL / SCALE / BUY). Risk focus (structural violations). Top movers (`market_opportunities.json` or `watchlist_signals.json`). Daily memo preview (rendered Markdown). HTMX refresh button. Auto-refresh every 60s. |
| Health | `/health` | Full | Overall severity banner. Per-probe metric cards (status / smoke / env). Smoke-test results (FAIL/WARN visible inline; OK folded). Env-var state grouped by namespace, secrets redacted. Registry inventory in collapsible table. HTMX refresh per section. |
| Portfolio | `/portfolio` | Stub | "Coming soon" with current portfolio total, cash, last snapshot timestamp. ~20 LOC. |
| Research | `/research` | Stub | "Coming soon" with discovery candidate counts (watch / discovered / rejected). ~20 LOC. |
| Operations | `/operations` | Stub | "Coming soon" with last 5 `run_history` rows from SQLite. ~30 LOC. |

### Markdown rendering

Daily memo is `outputs/latest/daily_memo.md`. Rendered server-side via
the `markdown` Python library. Result injected into `templates/today.html`
via Jinja2's `safe` filter. No client-side Markdown parsing.

### Charts

No charts in MVP. Decision plan is a ranked list, capital actions are
number totals, memo is prose. None of those need a chart. Add later only
when a specific page benefits.

### HTMX use cases (MVP)

- **Refresh button** on each page → fetches the page-data fragment,
  swaps the relevant section without full page reload.
- **Auto-refresh on Today every 60s** (configurable via `?refresh=N`
  querystring; `?refresh=0` disables) — feels alive without explicit polling.
- **Section-level refresh on Health** for each probe block.

## Visual system

### Color palette (dark default)

| Token | Tailwind | Hex | Use |
|-------|----------|-----|-----|
| bg-base | `zinc-950` | `#09090b` | Page background |
| bg-surface | `zinc-900` | `#18181b` | Cards, panels |
| border | `zinc-800` | `#27272a` | Dividers, card borders |
| text-primary | `zinc-100` | `#f4f4f5` | Body text, headings |
| text-muted | `zinc-400` | `#a1a1aa` | Captions, secondary |
| OK | `emerald-500` | `#10b981` | Green severity |
| INFO | `sky-500` | `#0ea5e9` | Blue severity |
| WARN | `amber-500` | `#f59e0b` | Yellow severity |
| FAIL | `rose-500` | `#f43f5e` | Red severity |

One accent color (`emerald-500`) for primary interactive elements (refresh
button, links). Severity colors used **only** for severity, nowhere else.

### Typography

- Body, navigation, prose: `font-sans` (system stack: `ui-sans-serif`,
  `system-ui`, `-apple-system`, `BlinkMacSystemFont`)
- Numbers, tickers, timestamps, run IDs, paths: `font-mono`
  (`ui-monospace`, `Menlo`, `Consolas`)
- Section headers: `text-xs uppercase tracking-wide text-zinc-400`
- Page titles: `text-2xl font-semibold`
- Metric values: `text-3xl font-mono font-semibold`

### Layout primitives

- Page container: `max-w-7xl mx-auto px-6 py-8`
- Card: `bg-zinc-900 rounded-lg border border-zinc-800 p-6`
- Metric strip: 4-column grid desktop / 2 tablet / 1 mobile
- Section spacing: `space-y-8` between sections; `space-y-4` within a card

### Navigation

Top bar, sticky, dark. Left: app name + last successful run timestamp.
Center: 5 links (Today / Portfolio / Research / Health / Operations);
active route underlined with the accent color. Right: overall severity dot
(driven by the registry probe — one glance "is anything wrong?").

Theme toggle: deferred. Dark-only in MVP.

### Components

- **Severity badge** — `rounded-full px-2 py-0.5 text-xs font-medium`,
  variant determined by severity. Reused for page banners, table cells,
  nav indicator.
- **Metric card** — single number plus muted label, monospace number.
- **Data table** — dense rows, no zebra stripes, single 1px bottom border.
- **Empty state** — when an artifact is missing or stale: muted icon +
  `text-zinc-400` heading + brief explanation + (where applicable) the
  registry's `documented_in` path. No alarmism for optional artifacts.

## Testing

### Unit tests for the data layer

- `tests/test_gui_v2_today.py` — `collect_today_view(repo_root)` shape;
  missing-artifact graceful degradation; never-raises invariant.
- `tests/test_gui_v2_health.py` — `collect_health_view(repo_root)` shape;
  delegates to the same probes as the Production Health page.
- `tests/test_gui_v2_stubs.py` — Portfolio, Research, Operations stubs
  return valid dicts on an empty repo.

### Route smoke tests

`tests/test_gui_v2_routes.py` using FastAPI's `TestClient` (starlette):

- Each route returns HTTP 200
- Content-type is `text/html`
- Key strings present (page title, severity labels)
- HTMX fragment routes work (e.g., `/health/fragment/status`)

### Out of scope

- Headless-browser tests (the existing Streamlit GUI has none either)
- Visual regression tests
- `mypy` type-checking

## Deployment

### Systemd unit

`deploy/systemd/stockbot-dashboard.service`:

```
[Service]
ExecStart=/opt/stockbot/.venv/bin/uvicorn gui_v2.app:app --host 0.0.0.0 --port 8502
EnvironmentFile=/opt/stockbot/.env
Restart=on-failure
```

### Port allocation

- `8501` — existing Streamlit service (unchanged)
- `8502` — new FastAPI dashboard service

No firewall changes by default; operator decides whether to expose `8502`
publicly or restrict via cloud firewall / SSH port-forward.

### Coexistence

Both services run simultaneously and indefinitely. The operator retires
Streamlit only when satisfied with the new GUI. No forced cutover.

### Enabling

Opt-in:

```
sudo systemctl enable --now stockbot-dashboard.service
```

## Dependencies

Added to `requirements.txt`:

- `fastapi>=0.115,<1.0` (~10 MB with starlette)
- `uvicorn[standard]>=0.30,<1.0` (~5 MB)
- `jinja2>=3.1,<4.0` (already transitive)
- `markdown>=3.5,<4.0`
- `httpx` (for `TestClient`; often already present)

## Reversibility / rollback

- Remove the package: `rm -rf gui_v2/`
- Remove the systemd unit: `sudo systemctl disable --now
  stockbot-dashboard.service && rm deploy/systemd/stockbot-dashboard.service`
- Uninstall deps: `pip uninstall fastapi uvicorn markdown` (only if
  desired; no other module depends on them)

Streamlit and the daily pipeline are untouched throughout. A failed
`stockbot-dashboard.service` cannot affect `stockbot-daily.timer` or
`stockbot-streamlit.service`.

## Safety invariants (pinned for the implementation plan)

- `gui_v2/` is a strict reader; only reads from `outputs/*`, the registry,
  and the new tools.
- No artifact writes anywhere in `gui_v2/`.
- No env mutation.
- No imports from `gui/` (Streamlit) into `gui_v2/`.
- Advisory-only / no-trade flags preserved in any rendered output.
- No business-logic computation in any data-collection function.

## Out of scope (deferred)

- Theme toggle (dark-only MVP)
- Charts (none in MVP)
- Forms / write actions (none — read-only is the entire point)
- Authentication / authorization (operator restricts via firewall / SSH tunnel)
- Visual regression tests
- Compiled Tailwind (CDN initially)
- Portfolio, Research, Operations full content (stubs only in MVP)
- Retirement of Streamlit (when operator decides)
- Migration of the remaining ~30 Streamlit pages

These are all genuinely deferred — they are not blockers for the MVP.
Each can be added incrementally without changing the architecture.
