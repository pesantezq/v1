# GUI v2 — State &amp; Design Reference

**Status:** authoritative overview of the `gui_v2/` dashboard. Last verified against
code on 2026-07-07.
**Scope:** the whole GUI surface — architecture, routes, data flow, styling,
behaviors, and the invariants that keep it observe-only. Topic-specific detail lives
in the sibling docs cross-linked at the bottom.

The dashboard is a **consumer** in a producer/consumer split. The daily pipeline
(`main.py` + advisory stages) writes JSON artifacts to `outputs/latest/`; the GUI
reads them on each request and renders a view. The decision source of truth —
`outputs/latest/decision_plan.json` — is authored elsewhere and only *displayed* here.

**At a glance:** 10 persona tabs · 28 HTTP routes (10 GET pages / 18 POST + util) ·
~80 artifacts read · **0** writes to the decision core · FastAPI + Jinja2 + htmx +
Tailwind · uvicorn on `:8502` behind a Cloudflare tunnel.

---

## 1. What it is

- **Artifact consumer only** — no scoring, ranking, or recommendation logic runs in
  the GUI. Every number traces to a source artifact, named on the card. The GUI could
  be deleted without changing a single decision.
- **Observe-only** — a persistent banner (top &amp; bottom). Trade verbs
  (BUY/SELL/SCALE) render *only* inside decision-core cards; evidence panels state,
  never instruct. See `docs/gui_observe_only_safety.md`.
- **Two-lane aware** — surfaces the active simulation/sandbox lane and the human-gated
  production promotion lane (Governance tab) without blurring them.
- **Operator plane** — create / dispatch / cancel work orders and gated config edits;
  a control surface with **no trade-execution primitives**. See
  `docs/GUI_OPERATOR_COCKPIT.md`.

## 2. Architecture

Every page is one synchronous pass — authenticate, collect, render. There is no
client-side state store and no database: the filesystem *is* the state, re-read on
every request so new pipeline output appears without a deploy.

**Request lifecycle:**

```
Browser (nav click / htmx poll)
   → GET /dashboard/*
   → _require_auth        (HTTP Basic, env-gated; open if unset)
   → collect_*_view(root) (reads outputs/latest/*.json fresh)
   → Jinja2 template      (dashboard/*.html ⇢ base.html ⇢ _ui.html macros)
   → htmx swaps #content in place
```

**Code layers:**

| Layer | Where | Notes |
|---|---|---|
| Server | `gui_v2/app.py` | FastAPI app, uvicorn `:8502`, **no `--reload`** (code changes need a restart). Mounts `/static`. |
| Auth | `_require_auth` | HTTP Basic via `GUI_V2_AUTH_USER/PASS`, constant-time compare, per-request. Unset ⇒ open mode (tests). |
| Collectors | `gui_v2/data/dash_*.py` | One `collect_*_view(root)` per tab. Pure reads → card/view-model dicts. Legacy `portfolio.py` / `risk_impact.py` feed `dash_portfolio` as data sources. |
| Templates | `gui_v2/templates/dashboard/*.html` | Extend `base.html`; shared macros in `components/_ui.html`. Legacy top-level `templates/*.html` are **unrouted**. |
| Assets | `gui_v2/static/` | Self-hosted + cache-busted: compiled Tailwind `app.css` + vendored `htmx.min.js`, each `?v=<mtime>`. No external CDN at runtime. |

## 3. Persona tabs

| Tab | Path | Collector | Poll | Answers |
|---|---|---|---|---|
| Today | `/dashboard/today` | `dash_today` | 60s | Can I trust today's run? Health, decision core, risk, deployable capital. |
| Portfolio | `/dashboard/portfolio` | `dash_portfolio` | 120s | Holdings, advisory picks w/ crowd context, capital plan, weekly deployment, risk/tax evidence. |
| Quant | `/dashboard/quant` | `dash_quant` | 120s | Pattern efficacy, calibration, attribution, retune impact, Kelly sizing, scenario risk. |
| Strategy | `/dashboard/strategy-lab` | `dash_next_stage` | 120s | Strategy leaderboard, backtests, projections, opportunity radar; approve → sandbox/watchlist. |
| Crowd | `/dashboard/crowd-radar` | `dash_crowd_radar` | 120s | Public-knowledge velocity, flock states, social sentiment, source compliance (sandbox). |
| Tax | `/dashboard/strategy-tax` | `dash_strategy_tax` | 120s | Tax scorecard, harvest advisor, 8-profile strategy comparison, Schwab lot presence. |
| Governance | `/dashboard/governance` | `dash_governance` | 120s | Sim-gov promotion queue: pending/approved proposals, AI review, production-apply state; approve/reject/defer. |
| System | `/dashboard/system` | `dash_system` | 120s | Run status, data quality, artifact registry, AI+FMP budget, doc-audit, served-SHA deploy card. |
| Memo | `/dashboard/memo` | `dash_memo` | 120s | The rendered daily investor memo (prose) + coherence reconciliation. |
| Operator | `/dashboard/operator` | `operator_control` | — | Work-order plane: create/dispatch/cancel, quarantine review, repair worker. |

Two auxiliary GET pages sit outside the top-nav: `/dashboard/portfolio-sync` (Schwab
reconciliation, see `docs/gui_remote_access.md`) and `/dashboard/portfolio-config`
(gated holdings/cash edit, see `docs/gui_portfolio_config.md`). Old flat routes
(`/portfolio`, `/health`, `/research`, `/operations`, `/risk-impact`) **302-redirect**
into the persona routes.

## 4. Routes

**GET — page renders (idempotent):** `/` (today alias), `/dashboard/{today,
portfolio, quant, strategy-lab, crowd-radar, strategy-tax, governance, system, memo,
operator}`, `/dashboard/portfolio-sync`, `/dashboard/portfolio-config`,
`/dashboard/operator/report/{id}`, `/dashboard/operator/quarantine/{id}/diff`.

**POST — gated actions (no exec primitives):**

| Path | Effect |
|---|---|
| `…/strategy-lab/decide` | approve/reject/defer strategy → sandbox re-anchor |
| `…/strategy-lab/opportunity-decide` | approve opportunity → extended watchlist |
| `…/governance/decide` | human gate on a promotion proposal |
| `…/portfolio-sync/reconcile` | Schwab read-only reconcile → proposal |
| `…/portfolio-config/validate` | dry-run validate holdings/cash edit |
| `…/portfolio-config/save` | write config (backup + audit; gated) |
| `…/operator/create` | create work order (observe-only) |
| `…/operator/dispatch` | dispatch repair worker (gated) |
| `…/operator/cancel` | cancel a work order |
| `…/operator/request-update` · `…/operator/apply-update` | served-SHA update → gated ff-update + restart |

Mutating POSTs are same-origin checked and env-gated (`GUI_V2_PORTFOLIO_EDIT`,
`GUI_V2_DEPLOY_APPLY`). Approvals move sandbox/watchlist/config state only — never
`decision_plan.json`.

## 5. Data flow

Each collector reads a focused slice of `outputs/latest/`. A card carries its **source
artifact name**, a severity, and a timestamp — so every figure is auditable back to the
producer. Missing/aged artifacts degrade honestly rather than showing a fabricated zero.

| Collector | Representative artifacts |
|---|---|
| `dash_today` | `daily_run_status` · `decision_plan` · `risk_delta` · `cash_deployment_plan` |
| `dash_portfolio` | `decision_plan` · `cash_deployment_plan` · `risk_delta` · `exit_advisor` · `earnings_gate` · `correlation_risk_advisor` · `vol_regime_advisor` · `tax_harvest_advisor` · `watchlist_signals` · `news_evidence_layer` · `portfolio_snapshot` |
| `dash_quant` | `pattern_efficacy_{weekly,monthly,yearly}` · `confidence_calibration` · `alpha_attribution_report` · `retune_impact` · `kelly_sizing_advisor` · `scenario_risk` · `quant_watch_status` · `run_manifest` |
| `dash_next_stage` | `strategy_leaderboard` · `portfolio_backtest` · `portfolio_projection` · `opportunity_radar` · `market_opportunity_review_cards` · `operator_action_queue` · `system_improvement_ideas` · `shadow_portfolios` |
| `dash_crowd_radar` | `public_knowledge_velocity` · `crowd_multi_source_velocity` · `flock_intelligence` · `social_sentiment_status` · `social_source_compliance` · `crowd_radar_activation_check` |
| `dash_governance` | `pending_proposals` · `approved_proposals` · `daily_ai_review_result` · `daily_governance_status` · `production_application_state` |
| `dash_system` | `daily_run_status` · `data_quality_report` · `artifact_registry_status` · `ai_budget_summary` · `fmp_budget_status` · `doc_audit_status` · `pipeline_run_status` · `broker_sync_status` |
| `dash_strategy_tax` | `strategy_tax_scorecard` · `tax_harvest_advisor` · `strategy_comparison` · `schwab_tax_lots` |
| `dash_portfolio_sync` | `schwab_portfolio_snapshot` · `schwab_positions` · `portfolio_reconciliation` · `portfolio_config_update_proposal` · `broker_sync_status` |

## 6. Components &amp; styling

**Shell &amp; macros:**
- `base.html` — observe-only banner, sticky persona nav, theme toggle, mobile
  bottom-nav, footer, asset links.
- `components/_ui.html` — the macro library: `hero_stat`, `status_card`,
  `status_badge`/`badge`, `sev_dot`, `sev_rail`, `section_header`, `page_header`,
  `timestamp`, `evidence`, `empty_state`, `all_clear`, `action_chip`,
  `responsive_table`.
- `components/` — `decision_card`, `operator_panel`, `portfolio_edit_form`,
  `mobile_status_bar`, `bottom_nav`, `validation_errors`, `_charts`.
  (The Phase-1 consolidation deleted the orphaned `metric_card`, `severity_badge`,
  `evidence_drawer`, and `source_artifact_label` components — callers now use the
  `_ui` macros directly.)

**Severity system (semantic, ≠ accent).** One token vocabulary drives every badge,
dot, and rail, through **two** single-source macros — never inline ladders:
- `_sev_classes` → the translucent `/15` badge/hero fill (`status_badge`, `badge`,
  `hero_stat`).
- `sev_rail` → the solid `/70` left-rail bar (`status_card` plus the portfolio and
  strategy-lab hero rails).

Vocabulary: `green` (ok) · `yellow`/`amber` (warning) · `red` · `blue`/`sky` (info) ·
`gray` (unknown). Both macros accept the aliases (`amber`≡`yellow`, `sky`≡`blue`) so a
loader emitting either spelling renders correctly. Two drift bugs traced to hand-copied
ladders that lacked a branch: `amber` fell through to gray and hid below-floor /
concentration warnings (fixed 2026-07-07), and a `blue` rail fell through to gray and
greyed the strategy-lab "Best Balance" card (fixed by the `sev_rail` consolidation
2026-07-08). Timestamps color by age: fresh = muted, >26h = amber, >50h = rose.

**Styling pipeline:**
- **Tailwind** — compiled &amp; purged to `static/app.css` by
  `scripts/build_dashboard_css.sh` (standalone CLI, no node). Replaced the runtime Play
  CDN — no in-browser JIT, no external dependency. Rebuild after adding utility classes;
  the script is arch-aware (x64/arm64) and includes `@plugin @tailwindcss/typography`
  for the `prose` memo block. The ~100MB CLI binary is git-ignored; only `app.css` is
  committed.
- **Theme** — dark by default; light via `html[data-theme="light"]` bounded overrides +
  `prefers-color-scheme`. Preference persisted in `localStorage`, applied pre-paint to
  avoid flash. See `docs/gui_mobile.md` for responsive detail.
- **Cache-bust** — `static_v()` appends `?v=<mtime>` to css/js, so a rebuild is fetched
  without a hard refresh.
- **Mobile** — responsive grids, fixed bottom-nav under `md`, and every wide table
  wrapped in `overflow-x-auto` so the body never scrolls sideways.
- **Keyboard a11y** — interactive macros carry a `focus-visible:` ring (emerald on the
  `page_header` Refresh button, subtle zinc on the evidence `<summary>`) so keyboard-only
  operators can see focus; the ring is suppressed for mouse clicks.

## 7. Behaviors

- **Auto-refresh (data)** — each tab htmx-polls itself (Today 60s, others 120s),
  swapping freshly-rendered content. Collectors re-read artifacts each poll, so new
  pipeline output appears with no reload. Manual **Refresh** button too.
- **Code updates (manual)** — uvicorn runs without `--reload`; Python/template changes
  require `sudo systemctl restart stockbot-dashboard`.
- **Deploy card** — the System tab compares served-SHA vs `origin/main` and offers a
  gated one-click fast-forward update + restart (`GUI_V2_DEPLOY_APPLY=1`). See
  `docs/dashboard_auto_update.md`.
- **Auth** — HTTP Basic (`GUI_V2_AUTH_USER/PASS`), per request; unset ⇒ open mode.
  Static assets are unauthenticated. See `docs/gui_remote_access.md` and
  `docs/DASHBOARD_HOSTING.md`.
- **Operator plane** — create/dispatch/cancel work orders; protected-path edits are
  quarantined; the autonomous repair worker is cost-capped and gated.
- **Gated edits** — config save writes a backup + audit record and only runs with the
  edit gate on; a failed validation writes nothing.

## 8. Invariants

1. **Consumer, never author.** No decision/scoring recompute; no writes to
   `decision_plan.json`.
2. **Observe-only surface.** Trade verbs appear only in decision-core cards; the banner
   is hard-coded, top and bottom.
3. **Every figure is sourced.** Cards name their artifact and timestamp; no
   unattributed number on the page.
4. **Honest degradation.** Missing artifacts show explicit empty states; aged data
   colors amber/rose; a degraded plan reads as a warning — never a fabricated zero.
5. **Null-guarded reads.** New fields are optional; a pre-feature artifact renders the
   older shape gracefully (proven live with the capital-envelope rollout).
6. **Gated mutation, no execution.** POST actions are same-origin, env-gated, and touch
   sandbox/watchlist/config only. No broker or order primitive exists in this layer.

## 9. Recent changes

### Backlog completion (2026-07-09)

Closes the three items deferred from the 4-phase program:

| Change | Area | Status |
|---|---|---|
| Per-decision triage badges (Critical/Action/Monitor/Ignore) | portfolio · `dash_portfolio` | shipped |
| News Intelligence section — `news_intelligence.json` packets | portfolio · `dash_portfolio` | shipped |
| Pipeline Wiring + Discovery Pulse cards (Discovery & Wiring group) | system · `dash_system` | shipped |

All pure consumers, observe-only, verb-free surfaces. Spec:
`docs/superpowers/specs/2026-07-09-gui-backlog-completion.md`.

### Phase 4 — cross-cutting a11y / mobile / perf (2026-07-09)

| Change | Area | Status |
|---|---|---|
| Global `:focus-visible` outline for all interactive elements | `base.html` | shipped |
| Memo funding-grid per-cell null guard | memo | shipped |

Verification: poll cost unchanged (Today 60s / others 120s); no fixed-width
mobile-overflow elements; new sections use responsive grids + per-table
`overflow-x-auto`. Completes the 4-phase GUI program. Spec:
`docs/superpowers/specs/2026-07-09-gui-phase4-a11y-mobile-perf.md`.

### Phase 3 — per-tab deep-dives (2026-07-09)

Complete operator-workflow data on the three highest-value tabs:

| Change | Area | Status |
|---|---|---|
| Decision triage card (bucket counts, verb-free) | today · `dash_today` | shipped |
| Triage breakdown on the advisory-queue header | portfolio · `dash_portfolio` | shipped |
| Memo Coherence panel — `memo_coherence.json` (funding, investor summary, reconciliation verdict) | memo · `dash_memo` | shipped |

Verb rule preserved: triage surfaces are counts only; action verbs stay on the
Portfolio decision cards. Deferred: `news_intelligence`, per-decision triage
badges, discovery/wiring (System-tab). Spec:
`docs/superpowers/specs/2026-07-09-gui-phase3-per-tab-deep-dives.md`.

### Phase 2 — close data-surfacing gaps (2026-07-09)

Quant-lens cluster — surface shipped-but-unrendered data via Phase-1 primitives:

| Change | Area | Status |
|---|---|---|
| Active Quant Concerns — one card per `quant_watch_status.active[]` probe (was a count only) | quant · `dash_quant` | shipped |
| Regime / Crowd / Strategy Breakdown — `quant_feedback` by_regime/by_crowd_state/by_strategy tables | quant · `dash_quant` | shipped |
| Retune Impact card — current-gauge 1d hit-rate / mean-return evidence (verdict still deferred to the memo) | quant · `dash_quant` | shipped |

Scale note: `hit_rate` is a fraction (×100 → %); `retune_impact.mean_return_1d`
and `quant_feedback.mean_return` are **already** percent-valued (rendered as-is —
do not ×100). Deferred to Phase 3 (per-tab): `decision_triage`, full
`memo_coherence` surface, `news_intelligence`. Spec:
`docs/superpowers/specs/2026-07-09-gui-phase2-data-surfacing.md`.

### Phase 1 — design-system foundation (2026-07-08 / 09)

| Change | Area | Status |
|---|---|---|
| Delete 6 unrouted legacy templates + 2 orphan components | templates | shipped |
| Collapse evidence / empty-state duplication onto `_ui` macros | `portfolio_config` · `portfolio_sync` | shipped |
| Extract `sev_rail` macro; retire 3 hand-copied rail ladders (fixes strategy-lab "Best Balance" gray rail) | `_ui` · portfolio · strategy_lab | shipped |
| Keyboard `focus-visible` rings on Refresh button + evidence summary | `_ui` macros | shipped |

Table-wrapper consolidation onto `ui.responsive_table()` was **deferred**: the existing
wrappers carry border/visibility chrome the macro does not reproduce, and every candidate
swap would alter mobile layout without a pixel-preserving path (the overflow-guard test is
already green).

### 2026-07-07

| Change | Area | Status |
|---|---|---|
| Surface the capital-deployment plan (Capital card + Weekly Deployment section + Today glance) | portfolio · today | shipped |
| Fix `amber` severity token rendering as neutral gray (hid below-floor / concentration warnings) | `_ui` macros · presenter | shipped |
| Age-based timestamp coloring for stale-data honesty | `app.py` · `_ui` | shipped |
| Wrap unguarded tables in `overflow-x-auto` (mobile) | crowd · governance | shipped |
| Vendor htmx locally + self-host compiled Tailwind (drop both CDNs) | static · `base.html` | shipped |
| Cache-bust static assets (`?v=mtime`) | `app.py` · `base.html` | shipped |
| Fix double-escaped ampersands in section/page headers | 4 templates | shipped |

Verified by 317+ GUI tests and a headless screenshot pass of the live service; see
`docs/CHANGELOG_DECISIONS.md` (2026-07-07 entries).

---

## Related docs

- `docs/gui_decision_center.md` — the decision-center view detail
- `docs/GUI_OPERATOR_COCKPIT.md` — operator control plane
- `docs/gui_observe_only_safety.md` — observe-only guarantees
- `docs/gui_portfolio_config.md` — gated config edit
- `docs/gui_mobile.md` — responsive / mobile behavior
- `docs/gui_remote_access.md` · `docs/DASHBOARD_HOSTING.md` — access &amp; hosting
- `docs/dashboard_auto_update.md` — served-SHA deploy card
- `docs/gui_usage.md` — operator usage guide
