# GUI Persona Cockpit — Usage Guide

## Overview

StockBot Dashboard v2 is an **advisory-only**, read-only web interface that surfaces
pipeline artifacts for operator review. It never executes trades, never calls a broker
API, and never recomputes decisions. Every view is an artifact consumer.

The dashboard runs on FastAPI + Jinja2 + HTMX + Tailwind at **port 8502** and is
managed by the `stockbot-dashboard` systemd unit (`deploy/systemd/stockbot-dashboard.service`).

---

## Three Personas + Memo

The cockpit is organized around three operator personas plus a standalone Memo page
and two portfolio-management utility views.

### 1. Operator / Portfolio Manager (`/dashboard/today` and `/dashboard/portfolio`)

**Who:** The operator reviewing today's advisory recommendations and current holdings.

**`/dashboard/today`** — Daily snapshot. Answers "what matters right now?":
- Decision plan status card (reads `outputs/latest/decision_plan.json`)
- System health badge (reads `outputs/latest/daily_run_status.json`)
- Risk-delta card (reads `outputs/latest/risk_delta.json`)
- Memo excerpt
- Each card shows an evidence drawer (see below) listing the source artifacts

**`/dashboard/portfolio`** — Holdings and P&L view:
- Current portfolio snapshot (reads `outputs/portfolio/portfolio_snapshot.json`)
- Allocation summary
- P&L attribution cards (reads `outputs/policy/profit_attribution.json`)
- Evidence drawers on each card

### 2. Quant / Researcher (`/dashboard/quant`)

**Who:** The operator reviewing signal quality, pattern-loop health, and backtesting
status.

Content:
- Signal outcomes summary (reads `outputs/performance/signal_outcomes.csv`)
- Confidence Calibration + Calibration Trend cards (reads `confidence_calibration.json` plus `outputs/history/*/confidence_calibration.json` for the trend; see `docs/CONFIDENCE_CALIBRATION.md`)
- Walk-forward / backtest health (reads output from `backtesting/backtest_health.py`)
- Weight-tuning suggestions (reads `outputs/performance/weight_tuning_suggestions.json`)
- Pattern-loop status cards
- Evidence drawers on each card

### 3. Developer / System (`/dashboard/system`)

**Who:** The operator reviewing pipeline health, cron status, and error rates.

Content:
- Daily run status (reads `outputs/latest/daily_run_status.json`)
- FMP budget telemetry (reads `outputs/latest/fmp_budget_status.json`)
- AI-cost trend (reads `outputs/policy/ai_usage_events.jsonl`)
- Discovery health (reads `outputs/sandbox/discovery/`)
- Applied-fix verification ledger
- Evidence drawers on each card

### 4. Memo (`/dashboard/memo`)

The rendered daily advisory memo. Reads `outputs/latest/daily_memo.md` (or
`outputs/latest/daily_memo.txt`).

---

## `/dashboard/*` Routes

| Route | Persona | Primary artifacts |
|---|---|---|
| `/dashboard/today` | Operator | `decision_plan.json`, `daily_run_status.json`, `risk_delta.json` |
| `/dashboard/portfolio` | Portfolio Manager | `portfolio_snapshot.json`, `profit_attribution.json` |
| `/dashboard/quant` | Quant | `signal_outcomes.csv`, `weight_tuning_suggestions.json`, backtest artifacts |
| `/dashboard/system` | Developer | `daily_run_status.json`, `fmp_budget_status.json`, `ai_usage_events.jsonl` |
| `/dashboard/memo` | All | `daily_memo.md` |
| `/dashboard/portfolio-sync` | Portfolio Manager | Schwab reconcile proposal (read-only) |
| `/dashboard/portfolio-config` | Portfolio Manager | Holdings + cash edit (gated; see `docs/gui_portfolio_config.md`) |

### Root and Old-Route Redirects

- `/` redirects to `/dashboard/today` (302).
- Old routes redirect to their persona equivalent:
  - `/portfolio` → `/dashboard/portfolio`
  - `/risk-impact` → `/dashboard/portfolio`
  - `/research` → `/dashboard/quant`
  - `/health` → `/dashboard/system`
  - `/operations` → `/dashboard/system`

These redirects are registered in `gui_v2/data/shared.py` (`REDIRECT_MAP`) and wired
in `gui_v2/app.py` at startup. Bookmarks to old routes continue to work.

---

## Evidence Drawers

Every status card includes a collapsible **Sources** link at the bottom. Expanding it
lists the exact artifact file paths that were read to produce the card. This provides
a direct traceability path from the displayed information back to the on-disk artifact.

Evidence drawers are rendered by `gui_v2/templates/components/evidence_drawer.html`
and populated via the `source_artifacts` field of each normalized card (see
`gui_v2/data/shared.py:card()`).

---

## Observe-Only Banner

Every page displays a persistent amber banner at the top of the page:

> **Observe-only · No trade execution**

This is hardcoded in `gui_v2/templates/base.html` and cannot be removed without
modifying the template. It is present on all dashboard routes. See
`docs/gui_observe_only_safety.md` for the full observe-only model.

---

## Authentication

The dashboard is open by default. Optional HTTP Basic Auth is enabled by setting both
`GUI_V2_AUTH_USER` and `GUI_V2_AUTH_PASS` in the environment. Credentials are compared
with constant-time comparison. See `docs/gui_remote_access.md` for secure remote access.

---

## Related Docs

- `docs/gui_mobile.md` — mobile browser usage
- `docs/gui_remote_access.md` — secure remote / phone access
- `docs/gui_observe_only_safety.md` — observe-only model and source-of-truth invariant
- `docs/gui_portfolio_config.md` — gated portfolio config edit
- `docs/superpowers/specs/2026-06-08-gui-persona-cockpit-design.md` — design spec
- `docs/superpowers/plans/2026-06-08-gui-persona-cockpit.md` — implementation plan
