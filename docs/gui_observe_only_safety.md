# GUI Observe-Only Safety Model

## Core Invariant

The StockBot Dashboard v2 is an **artifact consumer and observability surface only**.
It does not execute trades, call broker APIs, emit recommendations, or recompute
decisions. This is a hard architectural boundary, not a configuration option.

---

## What "Official Actions" Means

Only decision-core artifacts constitute official advisory actions:

| Artifact | Role |
|---|---|
| `outputs/latest/decision_plan.json` | Source-of-truth decision list (HOLD / REDUCE / INCREASE / WATCH) |
| `outputs/latest/decision_plan.md` | Human-readable rendering of the same plan |
| `outputs/latest/system_decision_summary.json` | System-level action summary |
| `outputs/latest/decision_explanations.json` | Per-decision rationale |
| `outputs/latest/decision_triage.json` | Triage / priority ordering |

Every other view in the dashboard is **evidence, observability, or proposal** — it
informs the operator but does not constitute an advisory output.

---

## What the GUI Does Not Do

- No `Execute` button.
- No `Trade` button.
- No `Buy Now` / `Sell Now` / `Place Order` label.
- No `Auto-Trade` / `Auto-Approve` workflow.
- No connection to a brokerage API (Schwab sync is a read-only reconcile proposal;
  see `docs/gui_portfolio_config.md`).
- No recomputation of `signal_score`, `confidence_score`, `effective_score`,
  `conviction_score`, `final_rank_score`, or `recommendation_score`.
- No modification of `decision_engine.py` or the scoring pipeline.

These constraints are enforced by a test (`tests/test_gui_dashboard_shell.py`) that
greps all dashboard templates for forbidden labels and fails the build if any are
present.

---

## Observe-Only Banner

The amber banner at the top of every page:

> **Observe-only · No trade execution**

is hardcoded in `gui_v2/templates/base.html` (line 48). It cannot be suppressed by
an environment variable or configuration option. The footer also reads:

> *Advisory only — no trades executed.*

Both are present on every route including `/dashboard/portfolio-config` (the only
write-surface in the GUI).

---

## The One Write Surface: Portfolio Config (Gated)

`/dashboard/portfolio-config/save` is the only POST route that mutates on-disk state.
It writes only:
- `config.json` — the `portfolio.holdings` and `portfolio.cash_available` keys only;
  all other keys are preserved.
- `outputs/policy/portfolio_backups/config.<timestamp>.json` — a backup taken before
  the write.
- `outputs/policy/manual_portfolio_updates.jsonl` — append-only audit record.

This write surface is:
- **Gated** behind `GUI_V2_AUTH_USER` + `GUI_V2_AUTH_PASS` + `GUI_V2_PORTFOLIO_EDIT=1`
  (returns HTTP 403 if any condition is unmet).
- **Observe-only flagged**: the audit record carries `observe_only: true` and
  `no_trade: true`.
- **Never touches** decision-core artifacts or `signal_registry.yaml`.
- **Fully reversible**: the backup can be restored manually at any time.

See `docs/gui_portfolio_config.md` for the full gated-edit model.

---

## Source-of-Truth Invariant

`outputs/latest/decision_plan.json` is the decision source of truth.

The GUI, memo, and explanation layers are consumers of this artifact. They cannot
change it. The only path that writes to `outputs/latest/` is the daily pipeline
(`scripts/run_daily_safe.sh`) running under the `stockbot-daily` systemd timer.

No GUI action causes `decision_plan.json` to be regenerated or modified. The
dashboard reflects the most recently generated plan and clearly shows the `updated_at`
timestamp of each artifact so the operator can judge staleness.

---

## Observe-Only Flag in New Artifacts

Per `CLAUDE.md` (Observe-Only Default), all new observability artifacts produced by
the pipeline carry `observe_only: true` hardcoded. This flag is consumed by health
agents to distinguish proposals from actions. The GUI inherits this: it surfaces the
flag in the relevant cards so the operator can see which outputs are proposals vs.
official actions at a glance.

---

## Related Docs

- `docs/gui_usage.md` — dashboard overview and routes
- `docs/gui_portfolio_config.md` — gated config edit with full safety invariants
- `docs/decision_engine.md` — how decisions are produced
- `CLAUDE.md` — hard boundaries and protected semantics
