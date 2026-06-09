# GUI Persona Cockpit (web + mobile) — Design Spec

**Date:** 2026-06-08
**Status:** Approved (design + 3 key decisions); pending implementation plan
**Roadmap:** `current_step: gui_operator_cockpit_redesign` — this IS the sanctioned step.
**Branch:** `feat/gui-persona-cockpit` off `main` (independent; reads runtime artifacts incl. Schwab's, so it composes regardless of which pending branch merges).

---

## 1. Objective & personas
A production-ready web + mobile cockpit to operate StockBot from desktop or phone as three personas, observe-only:
1. **Portfolio Manager** — review decision-core + risk + watchlist + capital + memo; safely edit *local* portfolio config (gated).
2. **Quant** — review calibration / pattern-efficacy / retune / quant-watch / alpha-attribution / learning-loop, with sample-size + observe/proposal-only labels.
3. **Developer/System** — run health, registry status, data quality, budgets, memo delivery, docs/tests, artifact freshness, failure queue; "is the system trustworthy today?"

## 2. Safety invariants (enforced + tested)
- Global **"Observe-only · No trade execution"** banner.
- Only decision-core artifacts (`decision_plan`, `system_decision_summary`, `decision_explanations`, `decision_triage`) represent official actions. No GUI view creates buy/sell/hold outside them; no non-decision artifact aggregates into one.
- **No forbidden labels anywhere**: `Execute`, `Trade`, `Buy Now`, `Sell Now`, `Place Order`, `Auto-Trade`, `Auto-Approve` (a test greps all templates).
- Portfolio-config + Schwab proposal surfaces: *"Updates local StockBot configuration only. It does not execute trades."* Writes gated (auth + `GUI_V2_PORTFOLIO_EDIT=1`), dry-run + explicit confirm, backup + audit + reversible (reuse `tools/manual_portfolio_update.py` safe writer). No broker order capability.

## 3. Decisions (locked)
1. **Config editing = hybrid**: validate → dry-run before→after diff → explicit second "Confirm & save" → shared safe writer (timestamped backup + audit jsonl + validate). 
2. **Replace routes**: persona `/dashboard/*` become canonical; `/` → 302 `/dashboard/today`; old routes (`/portfolio`,`/research`,`/health`,`/operations`,`/risk-impact`) → 302 to their persona equivalent (nothing 404s).
3. **Write gating**: edit/save renders + accepts only when auth configured AND `GUI_V2_PORTFOLIO_EDIT=1`; else read-only "editing disabled" state. Read views stay open.

## 4. Architecture
- Stack unchanged: FastAPI + Jinja2 + HTMX + Tailwind-CDN, dark theme, optional env basic-auth, `_render` wrapper, existing filters (`severity_classes`/`risk_severity`/`status_label`).
- **New persona data collectors** in `gui_v2/data/` (`shared.py`, `dash_today.py`, `dash_portfolio.py`, `dash_quant.py`, `dash_system.py`, `dash_memo.py`, `dash_portfolio_config.py`, `dash_portfolio_sync.py`) that **reuse the existing collectors** (`today/portfolio/risk_impact/health/operations/research`) and add the extra artifacts, normalizing to the common card shape.
- **Routes:** `/dashboard/today|portfolio|quant|system|memo|portfolio-sync|portfolio-config`; `/` + old paths redirect. All `Depends(_require_auth)`.

### Normalized card shape (`shared.card(...)`)
```python
{"title","status"(ok|warning|red|info|unknown),"label","summary",
 "source_artifacts":[...],"updated_at","severity"(green|yellow|red|gray|blue)}
```
Every card carries `source_artifacts`; an evidence drawer (HTMX) exposes them.

## 5. Responsive shell
`base.html` gains: observe-only banner; **desktop top persona-nav** (`hidden md:flex`); **mobile bottom-nav** (`md:hidden fixed bottom`, 5 tabs: Today/Portfolio/Quant/System/Memo); **mobile status bar**. Mobile-first, 390px, no horizontal scroll: tables `hidden md:block` + a `md:hidden` stacked-card equivalent. Explicit empty states everywhere.
New components: `bottom_nav`, `mobile_status_bar`, `evidence_drawer`, `decision_card`, `source_artifact_label`, `portfolio_edit_form`, `validation_errors` (reuse `metric_card`, `empty_state`, `severity_badge`).

## 6. Persona view content
- **Today** (`/dashboard/today`): answers in <15s — system healthy? decision-core OK? anything to review? what changed since last run? memo to read? (cards from daily_run_status + artifact_registry_status[if present] + decision_plan + risk_delta + memo pointer + broker_sync_status[if present]).
- **Portfolio Manager** (`/dashboard/portfolio`): Top Insight, Decision Queue, Risk Focus, Capital/Allocation, Watchlist/Opportunities, Memo summary, evidence drawer. Reads decision-core + risk_delta + correlation/vol/earnings/exit/cash/tax advisors + watchlist_signals + market_opportunities + news_evidence_layer.
- **Quant** (`/dashboard/quant`): learning status, confidence_calibration, pattern_efficacy_{weekly,monthly,yearly}, retune_impact, gate_retune_suggestions, alpha_attribution, quant_watch_status[if present], kelly_sizing (advisory). Explicit labels: Insufficient history / Thin sample / Observe only / Proposal only / Caution / Improving / Mixed / Weak.
- **System** (`/dashboard/system`): daily_run_status, pipeline_run_status, artifact_registry_status[if present], data_quality_report, fmp_budget_status, ai_budget_summary, memo_delivery_status, doc_audit_status, historical_backfill_status, **broker_sync_status (Schwab health — closes deferred C2 pairing)**, failure queue, daily/monthly/yearly analysis status.
- **Memo** (`/dashboard/memo`): phone-readable — Top Insight, Risk Focus, Portfolio Decisions, Data Quality, Quant Notes, Watchlist Notes; copy/download if patterns exist; no raw hashes/noisy dumps.
- **Portfolio Sync** (`/dashboard/portfolio-sync`): the deferred Schwab view. Desktop: connection status, last sync, account/position counts, holdings-mismatch table, cash mismatch, proposal link/status, source artifacts. Mobile: stacked cards (Connected/Not configured/Error, Last sync, Holdings matched, Mismatches, Cash difference, "Review Differences", "Generate Config Update Proposal"). Reads broker_sync_status / schwab_portfolio_snapshot / schwab_positions / portfolio_reconciliation / portfolio_config_update_proposal. "Generate proposal" control calls `schwab_sync.run_reconcile` (read-only; writes proposal artifact only). Banner: "Updates local config only — no trades." No Execute/Trade/Place-Order labels.

## 7. Portfolio-config edit flow (gated)
`GET /dashboard/portfolio-config` (read config → form, or read-only "editing disabled" when ungated) → `POST .../validate` (HTMX: weight-sum, no-negative shares/cash, concentration & leverage caps, required symbol fields → `validation_errors` + dry-run before→after diff) → explicit **"Confirm & save"** → `POST .../save` via shared safe writer (extract `manual_portfolio_update`'s backup+audit+validate into a reusable function) → success page w/ backup path + revert instructions. The Schwab proposal can pre-fill this form (review → same gated apply).

## 8. Testing
Route rendering (all personas + redirects); `/`→today redirect; mobile card-equivalent present; status-label formatting; explicit empty states; **forbidden-label grep across templates**; config validation (all rules); config save safety (backup created + audit row + reversible) under the flag; write-surface gating (disabled without auth+flag → no form/save); **source-of-truth invariant** (no non-decision card emits buy/sell/hold); Schwab-sync view renders with artifacts absent (empty state) and present (fixtures); account ids masked in the sync view.

## 9. Docs
`docs/gui_usage.md` (personas, routes), `docs/gui_mobile.md`, `docs/gui_remote_access.md` (Tailscale-first, then Cloudflare Tunnel+Access; no public exposure; no hardcoded secrets), `docs/gui_observe_only_safety.md`, `docs/gui_portfolio_config.md` (edit behavior, gating, backup/audit/revert), deployment/service notes (the new routes + `GUI_V2_PORTFOLIO_EDIT` env). CHANGELOG + project_state step.

## 10. Milestones (sequential; review-gated)
1. **Shell + `/dashboard/today`**: base nav/banner/bottom-nav, `shared.card`, new components, route redirects, today cockpit. +tests (redirects, forbidden-label grep, today render).
2. **`/dashboard/portfolio`** (PM read view + evidence drawer).
3. **`/dashboard/quant`** (caution labels).
4. **`/dashboard/system`** (+ Schwab broker_sync_status health card).
5. **`/dashboard/memo`** (phone-readable).
6. **`/dashboard/portfolio-sync`** (Schwab view, mobile cards, generate-proposal read-only control).
7. **`/dashboard/portfolio-config`** (gated hybrid write: validate→dry-run→confirm→shared safe writer + backup/audit).
8. **Docs + review agents** (portfolio-render-reviewer, portfolio-test-reviewer, portfolio-architect, portfolio-doc-auditor; attribution-analyst for quant view; discovery-health for watchlist).

## 11. Risks & mitigations
| Risk | Mitigation |
|---|---|
| "Replace routes" breaks existing tests/links | Redirects (no 404s); update existing gui_v2 tests; keep old data collectors as sources |
| Remote write surface | auth+flag gating, dry-run+confirm, backup+audit+reversible, off by default |
| Forbidden labels slipping in | template grep test |
| Non-decision data implying actions | source-of-truth invariant test; role-gated language |
| Mobile horizontal scroll | tables→cards `md:hidden`/`hidden md:block`; 390px tests |
| Schwab artifacts absent (branch unmerged) | sync view degrades to explicit empty state; reads defensively |

## 12. Deferred / out of scope
- Artifact-registry registration of GUI/Schwab artifacts (until that branch merges).
- Live broker connection (Schwab creds are the operator's step).
- Lens-rollup summary producers (cockpit reads existing artifacts directly).
