# Next-Stage Portfolio Intelligence — Technical Specification & Phased Plan

Status: **planning / specification only** · 2026-06-09 · advisory-only · observe-only
Author scope: cross-functional architecture + implementation planning. **No runtime
code is changed by this document.** It is the implementation contract Claude Code
executes phase by phase afterward.

> **Reality check that shapes this whole plan.** A large fraction of the proposed
> architecture *already exists* in this repo. This spec is therefore written as a
> **delta plan**: each workstream is graded `EXISTS` / `PARTIAL` / `MISSING`, and
> phases build only the missing pieces on top of the existing lanes. Re-implementing
> what exists (the broker read-only layer, the operator-control approval/prompt
> plane, the discovery sandbox, the governed namespaces) is explicitly out of scope.

---

## 1. Executive summary

The end state is a **read-only, broker-aware, sandbox-driven, self-improving
portfolio intelligence platform**:

- It reads the user's **actual** Schwab portfolio (read-only) and reconciles it
  against local config — it never trades, never writes to the broker, never moves
  money.
- It manages the user's own portfolio analytically: drift, concentration, leverage,
  cash drag, risk guardrails — all feeding the **single source of truth**,
  `outputs/latest/decision_plan.json`.
- It scans the **broader market** (universe scanner + opportunity radar) for themes,
  sectors, commodities, and public/IPO/private candidates, scoring them with an
  **opportunity model distinct from the portfolio recommendation model**.
- Every idea is **proved in a sandbox** (shadow tracking + shadow portfolios) before
  any human chooses to promote it; the system **cannot** auto-promote a candidate
  into official recommendations.
- It records **events** (recommendations, opportunities, user actions, outcomes) for
  long-run **pattern recognition** that improves probability/risk classification and
  confidence calibration — never price prediction.
- It prompts **system improvements** daily through the operational dashboard — a
  distinct lane from market opportunities — and turns approved improvements (and
  health issues) into **Claude Code prompts**, never into automatic code changes.
- The operator can review and approve/reject/defer from **desktop or phone**, with
  advisory-only / observe-only / blocked-action labels visible everywhere.

The three prompt categories stay strictly separate end-to-end: **(A) Health
remediation**, **(B) Market opportunity research**, **(C) System improvement**.

---

## 2. Current-state audit

Grading: `EXISTS` (built + tested), `PARTIAL` (foundation present, gaps named),
`MISSING` (not present).

### 2.1 Decision engine — `EXISTS`
- `portfolio_automation/decision_engine.py` consolidates structural / portfolio /
  finance / watchlist / market streams → `outputs/latest/decision_plan.json`
  (`observe_only: true`). Protected scores: `signal_score`, `confidence_score`,
  `effective_score`, `conviction_score`, `final_rank_score`, `recommendation_score`.
- **Do not change.** Everything new is a consumer or an upstream optional input.
- Risk/unknown: none for this plan; the only wiring point is making broker holdings
  an *optional input* upstream of the engine (Phase 10), gated and reversible.

### 2.2 Watchlist / signal scanner — `EXISTS` (universe breadth `PARTIAL`)
- `watchlist_scanner/` (scanner, conviction, portfolio_fit, output_writers, daily_memo)
  + `config/signal_registry.yaml` (10 signals). Artifacts: `watchlist_signals.json`,
  `market_opportunities.json`, `theme_signals.json`, `watch_candidates.json`,
  `top100_{daily,weekly,monthly}.json`.
- Reusable: the scanner scoring + theme alignment + `top100` universe loaders.
- Missing: ETF/sector/commodity/theme **universe lists**, private/IPO segregation,
  opportunity scoring distinct from signal scoring.

### 2.3 Discovery / sandbox — `EXISTS`
- `portfolio_automation/discovery/` (corroboration, automatic_promotion_governance,
  approval_workflow, news_integration, discovery_memory) → `outputs/sandbox/discovery/`.
- Lifecycle `DISCOVERED → WATCH → MONITOR → REJECTED`; forbidden statuses
  (BUY/SELL/HOLD/PROMOTED/VALIDATED/ACTIONABLE) are **sanitizer-enforced**
  (`validate_automatic_promotion_safety`). Promotion gates are conservative + tested.
- **Do not weaken** the promotion guard. New sandbox artifacts reuse this guard.

### 2.4 Output namespaces — `EXISTS`
- `portfolio_automation/data_governance.py` `OutputNamespace` (LATEST, POLICY,
  PORTFOLIO, SANDBOX, HISTORICAL, LIVE, USER) + `safe_write_json/text` +
  path-traversal validation. `run_mode_governance.py` two-lane permissions; all modes
  `can_execute_trades=False`.
- **Do not change** the namespace contract. New artifacts pick the correct namespace.

### 2.5 Dashboard / GUI — `EXISTS` (opportunity/improvement card types `MISSING`)
- `gui_v2/` FastAPI cockpit (:8502, `stockbot-dashboard.service`), 5 persona tabs
  (Today/Portfolio/Quant/System/Memo) + portfolio-sync + portfolio-config; `card()`
  contract in `gui_v2/data/shared.py`; `components/operator_panel.html`; mobile
  `bottom_nav` + `mobile_status_bar`. Legacy Streamlit `gui/` is the reversible
  fallback (`docs/STREAMLIT_RETIREMENT.md`).
- Missing: dedicated **market-opportunity** and **system-improvement** card/probe
  categories; a global work-order browser; mobile approval-flow polish.

### 2.6 Daily memo / email — `EXISTS`
- `watchlist_scanner/daily_memo.py` → `daily_memo.md/.txt`;
  `portfolio_automation/memo_email_sender.py` (`observe_only`/`no_trade` hardcoded,
  non-blocking) → `outputs/policy/memo_delivery_log.jsonl`.
- Reusable as a presenter for new sections (opportunity radar digest, improvement brief).

### 2.7 OpenAI / API integration — `EXISTS` (opportunity-prompt artifacts `PARTIAL`)
- `theme_engine/` + `theme_discovery/` + `scraped_intel/`; provider routing
  (ollama/anthropic/openai) via config; `ai_budget_summary.json`,
  `ai_usage_events.jsonl`, `theme_engine_llm_metadata.json`. Keyword fallback when LLM
  off. `scraped_intel` has trial/shadow **modes** (PROPOSED→APPROVED_FOR_SHADOW→TRIAL).
- Missing: a first-class **market-opportunity prompt / review-card** artifact set and
  an opportunity approval queue (Section 9).

### 2.8 Outcome tracking / confidence calibration — `EXISTS` (generic event store `MISSING`)
- `decision_outcome_tracker.py` → `outputs/policy/decision_outcomes.jsonl`;
  `confidence_calibration.py`; `pattern_learning.py` → `pattern_efficacy_{w,m,y}.json`.
- Existing JSONL event stores: `decision_outcomes`, `ai_usage_events`,
  `coverage_history`, `memo_delivery_log`.
- Missing: `pattern_events`, `opportunity_events`, `user_action_log`, `outcome_events`
  as a unified learning-loop event spine (Section 11).

### 2.9 Run-mode / data governance — `EXISTS` (see 2.4). Do not change.

### 2.10 Operational health probes — `EXISTS` (opportunity/improvement probes `MISSING`)
- `operator_control/` plane: 14 probes, 10 skills, append-only `work_orders.jsonl` +
  `audit_log.jsonl`, prompt generation (`worker_prompts.py`), worker runner
  (scaffold default; autonomous behind 3-part gate; protected-path quarantine),
  `POST /dashboard/operator/create` + `/dispatch`. Daily health surfaced via
  `daily_run_status.json`, `artifact_registry_status.json`, content_liveness.
- **This plane already implements Health-Remediation prompts (Type A) end-to-end.**
  The deltas are: a **system-improvement** producer + probe category (Type C), and a
  **market-opportunity** probe/card category (Type B).

### 2.11 Tests — `EXISTS`
- ~208 test files; established patterns: AST no-trade scans (brokers),
  `test_observe_only_*` invariants (18+ files), artifact-registry contract tests,
  GUI loader tolerance tests, sandbox-only-write tests. New work mirrors these.

### 2.12 Docs — `EXISTS`
- `ARCHITECTURE_MAP.md`, `OUTPUT_ARTIFACT_CONTRACTS.md`, `DATA_GOVERNANCE.md`,
  `RUN_MODE_GOVERNANCE.md`, `operator_control*.md`, `STREAMLIT_RETIREMENT.md`,
  `SCORING_AND_CONFIDENCE.md`, `CONFIDENCE_CALIBRATION.md`. Update plan in Section 20.

### 2.13 Config — `EXISTS`
- `config.json` (investor, portfolio, watchlist_scanner, theme_engine, scanner,
  api_limits, operator_control); `config/signal_registry.yaml`. New feature flags are
  additive and default-**off**.

### 2.14 Deployment / run scripts — `EXISTS`
- systemd: `stockbot-daily.timer`, `stockbot-dashboard.service`,
  `stockbot-streamlit.service`, `stockbot-sandbox-daily.timer`; `scripts/preflight.sh`,
  `run_daily_safe.sh`. New skills are added as additive, default-inert pipeline stages
  / cron entries.

---

## 3. Target architecture

Four lanes, all read-only/advisory. **Arrows are data, never control.**

**Official lane** (exists; only optional broker input is new):
`Schwab read-only snapshot → normalized_positions → (optional) holdings resolver →
portfolio guardrails (drift/concentration/leverage/cash) → decision_engine →
decision_plan.json → memo / email / GUI (consumers)`.

**Research / sandbox lane** (foundation exists; radar/scoring/shadow are new):
`universe scanner → opportunity radar → {theme / public / ETF / commodity /
private-IPO candidates} → opportunity scoring → sandbox shadow tracking + shadow
portfolios → candidate promotion REVIEW (human) → (optional) watchlist-review only`.
Writes **sandbox namespace only**; cannot reach `decision_plan.json`.

**Operational improvement lane** (health half exists; improvement half is new):
`health probes + quality probes → operator-control work orders (Type A health) ‖
daily system-improvement skill → system_improvement_ideas → improvement approval
queue → Claude Code prompt generator (Type C)`.

**Learning loop** (snapshots exist; event spine is new):
`recommendation/opportunity/pattern/user-action/outcome/sandbox/system-improvement
events (JSONL) → confidence calibration + pattern efficacy → better probability/risk
classification (never price prediction)`.

---

## 4. Non-negotiable safety model

Every phase must preserve **all** of these (each becomes a test in Section 19):

- No auto-trading. No order placement. No broker write actions. No money movement.
- No automatic portfolio allocation changes.
- Sandbox/research code **cannot** write `decision_plan.json` or any LATEST official
  recommendation (enforced by `run_mode_governance` + namespace writes).
- A high `boom_score` **alone** can never create a buy recommendation or promote a
  candidate (Section 8 rules).
- Private companies (e.g. SpaceX) are **never** represented as tradeable tickers; they
  are `PRIVATE_WATCH_ONLY` with explicit access routes.
- Approval queues are **artifact-based** and execute nothing. Approving a market
  opportunity may at most move it to watchlist/review; approving a system improvement
  may at most generate a Claude Code prompt / queue item. Code is changed only when the
  user explicitly launches Claude Code.
- Secrets, OAuth tokens, account numbers, broker credentials are **never logged** and
  are redacted/masked in every artifact (reuse `brokers/broker_models.redact/mask`).
- All new layers degrade gracefully (try/except, `degraded_mode`, observe-only dict on
  failure) and never break the daily pipeline.
- `observe_only: true` is hardcoded in every new artifact. The single sanctioned
  mutating exception (`backtesting/auto_apply.py`) is **not** extended by this plan.

---

## 5. Schwab read-only integration — `EXISTS` (specify deltas only)

**Do not re-implement.** The layer is built and AST-tested. Existing modules and the
spec's proposed names map as follows:

| Spec-proposed module | Existing reality | Action |
|---|---|---|
| `brokers/base.py` | (implicit; `schwab_client` is the only client) | OPTIONAL: extract a `BrokerClient` Protocol for future brokers. Low priority. |
| `brokers/schwab_client.py` | `brokers/schwab_client.py` (GET-only, AST-tested) | Reuse. No change. |
| `brokers/schwab_snapshot.py` | `brokers/schwab_sync.py` `run_sync()` | Reuse. |
| `brokers/normalizer.py` | `brokers/broker_models.py` | Reuse. |
| `brokers/sync_status.py` | `brokers/broker_status.py` | Reuse. |

| Spec-proposed artifact | Existing reality |
|---|---|
| `outputs/portfolio/broker_snapshot.json` | `outputs/latest/schwab_portfolio_snapshot.json` |
| `outputs/portfolio/normalized_positions.json` | `outputs/latest/schwab_positions.json` |
| `outputs/portfolio/broker_sync_status.json` | `outputs/latest/broker_sync_status.json` |

**Deltas this plan actually needs (Phase 9):**
1. Decide whether to add namespace-aliased copies under `outputs/portfolio/` for the
   broker-aware manager (Section 6) or keep reading from `outputs/latest/`. *Open
   question 23.9.*
2. OPTIONAL `BrokerClient` Protocol in a new `brokers/base.py` (future-broker
   abstraction) — additive, no behavior change.
3. Confirm OAuth/token storage location for production (Open question 23.2).
- Config/env (exists): `SCHWAB_CLIENT_ID/SECRET/REDIRECT_URI`, `SCHWAB_READ_ONLY_MODE`
  (default true), token at `data/schwab_token.json` (0600, gitignored).
- Tests (exist): AST no-trade, observe_only on all 5 artifacts, config never mutated,
  masking, redaction, fail-closed-when-unconfigured. Keep + extend for any new alias.
- Dashboard: `/dashboard/portfolio-sync` exists. No new card required for Phase 9.

---

## 6. Broker-aware portfolio manager — `MISSING` (new, optional, gated)

Today portfolio math reads `config.json` holdings/cash only; broker artifacts are
never consumed. Add a **holdings resolver** as an optional upstream input.

- New module `portfolio_automation/holdings_resolver.py`:
  - `resolve_holdings(root, prefer_broker: bool) -> (holdings, cash, source, freshness)`.
  - Prefers a **fresh** `schwab_positions.json` + snapshot cash; falls back to
    `config.json` when broker data is stale/missing/unconfigured.
  - Emits `holdings_source` (`broker|config`) + `broker_freshness_age_s` so downstream
    **confidence is lowered** when broker data is stale (reuse degraded-mode pattern).
- Reuses existing `portfolio.py` calc functions (allocations, effective exposure,
  drift, concentration/leverage caps) — **no change to the math**, only the input source.
- New observe-only artifact `outputs/portfolio/broker_aware_portfolio.json`:
  `{observe_only, holdings_source, freshness, drift[], concentration[], leverage,
  cash_drag, config_vs_broker_drift[], confidence_modifier}`.
- Gated by `config.json portfolio.broker_aware.enabled` (default **false**). When off,
  behavior is byte-identical to today.
- Memo/GUI: a Portfolio-tab card "Actual vs config holdings" (read-only).
- Tests: stale broker → config fallback; fresh broker → broker source; confidence
  lowered on stale; **no broker writes**; decision_plan unchanged when flag off.

---

## 7. Universe scanner extension — `PARTIAL`

Extend the existing scanner; **all outputs land in the sandbox namespace.**

- New config-driven universe lists (`config/universe_lists.yaml`, additive):
  `approved_watchlist` (exists), `broad_market_etfs`, `sector_etfs`, `commodity_proxies`,
  `theme_baskets`, `private_ipo_watch`, `user_themes`. Seed defaults in Open Q 23.4.
- New module `portfolio_automation/universe_scanner.py` (wraps existing
  `universe/` + `scanner/` + `watchlist_scanner` scoring): produces candidate rows with
  `candidate_type ∈ {public_ticker, etf, commodity_proxy, theme_basket,
  private_ipo}` and an `access_route ∈ {ipo_watch, public_supplier, etf, fund, proxy,
  watch_only}`.
- **Private companies** (SpaceX, etc.) → `candidate_type=private_ipo`,
  `access_route ∈ {ipo_watch, public_supplier, etf, proxy, watch_only}`. **Never** a
  tradeable ticker; never priced as one.
- Example seed themes: Space economy, Oil supply shock, AI infrastructure,
  Uranium/nuclear, Defense, Robotics, Cybersecurity, Data-centers/power, Financials
  rotation, International recovery.
- Artifacts (SANDBOX): `outputs/sandbox/universe_scan_candidates.json`,
  `opportunity_radar.json`, `private_ipo_watchlist.json`, `theme_candidates.json`.
- Hard rule (tested): universe candidates **never** enter `decision_plan.json`.

---

## 8. Opportunity scoring — `MISSING` (distinct from recommendation scoring)

New module `portfolio_automation/opportunity_scoring.py`. Deterministic, explainable,
**separate** from the protected portfolio scores (it must not reuse their names).

- Inputs/dimensions (0–1 each): `catalyst_strength`, `price_volume_confirmation`,
  `fundamental_support`, `market_regime_fit`, `portfolio_diversification_value`,
  `access_investability`, `risk_adjusted_timing`, `boom_potential`, `evidence_quality`,
  `liquidity_quality`, `data_quality`; penalties: `hype_penalty`,
  `crowded_trade_penalty`, `single_headline_penalty`, `portfolio_overlap_penalty`.
- Outputs: `opportunity_score`, `boom_score`, `risk_score`, `investability_score`,
  `evidence_score`, `portfolio_fit_score`, `final_status`.
- Statuses: `DISCOVERED, WATCHING, SANDBOX_TRACKING, QUALIFIED,
  APPROVED_WATCHLIST_REVIEW, REJECTED, HYPE_NOISE, ACCESS_LIMITED, PRIVATE_WATCH_ONLY`.
- Rules (each a test): high `boom_score` alone cannot reach `QUALIFIED`/
  `APPROVED_WATCHLIST_REVIEW`; low `investability_score` caps status at
  `ACCESS_LIMITED`/`PRIVATE_WATCH_ONLY`; single-headline ideas require corroboration
  (reuse discovery corroboration); penalties are surfaced explicitly in the GUI.
- Reuses the discovery sanitizer so no forbidden BUY/SELL token can appear.

---

## 9. Market opportunity prompt integration — `PARTIAL`

Connect the existing OpenAI/theme layer to radar/sandbox/approval **without
duplicating** it.

- New thin module `portfolio_automation/market_opportunity_prompts.py`: reads
  `opportunity_radar.json` + `universe_scan_candidates.json`, calls the **existing**
  provider-routed LLM client (respecting `ai_budget`), and writes review cards +
  prompt records. Keyword/deterministic fallback when LLM off.
- Artifacts (SANDBOX): `outputs/sandbox/market_opportunity_prompts.json`,
  `market_opportunity_review_cards.json`, `opportunity_approval_queue.json`.
- It **never** writes official recommendations or buy/sell. It appears in the dashboard
  under a **Market Opportunity** category, visually distinct from system-improvement.

---

## 10. Sandbox shadow tracking & shadow portfolios — `MISSING` (foundation reusable)

New module `portfolio_automation/sandbox/shadow_tracker.py` (research-lane only).

- Per-candidate shadow record: `discovered_date, theme, candidate, candidate_type,
  proxy_tickers[], entry_reference_price (public proxies only), fwd_perf_{1,3,7,30}d,
  volatility, drawdown, news_followthrough, catalyst_persistence,
  diversification_value, would_have_helped_portfolio (bool/score)`.
- Shadow portfolios (simulated, never real): `actual_baseline, target_allocation_baseline,
  engine_followed, lower_risk, discovery_enhanced, boom_bucket`.
- Artifacts (SANDBOX): `outputs/sandbox/shadow_opportunity_tracking.json`,
  `shadow_portfolios.json`, `strategy_comparison.json`, `candidate_promotion_review.json`.
- Reuses `scraped_intel` trial-mode precedent for modes but adds the missing
  per-candidate position tracking. **No real positions, no trades.**

---

## 11. Pattern-recognition learning loop — `PARTIAL` (event spine `MISSING`)

Add a unified append-only event spine under POLICY (mirrors `decision_outcomes.jsonl`).

- Artifacts (POLICY, append-only): `pattern_events.jsonl`, `opportunity_events.jsonl`,
  `user_action_log.jsonl`, `outcome_events.jsonl`.
- New module `portfolio_automation/event_store.py`: `append_event(stream, event)` with a
  shared envelope: `event_id, timestamp, source, run_mode, namespace,
  ticker_or_theme, signal_type, market_context, portfolio_context, confidence,
  recommendation_or_action_or_status, user_decision?, outcome_windows?, evidence,
  data_quality`.
- Learning claims (documented honestly): improves probability/risk classification,
  learns which signals worked in which regimes, flags hype/noise, learns which user
  decisions helped/hurt, and feeds confidence calibration over time. **Never** claims
  price prediction. Consumes into existing `confidence_calibration` + `pattern_learning`.

---

## 12. System Operational Dashboard expansion — `PARTIAL`

Extend `gui_v2` (primary) using the existing `card()` + `operator_panel` pattern.

- New dashboard sections/cards (read existing + new artifacts): System Health, Data
  Quality, Broker Sync, Portfolio Risk (exist); **Universe Scanner, Opportunity Radar,
  Market Opportunity Review, Sandbox Tracking, Candidate Promotion Review, Approval
  Queues, Pattern Memory, Daily System-Improvement Ideas, Claude Code Launch Center,
  Mobile Review Mode** (new).
- Card/probe categories (extend the probe `category` field): `health_probe`,
  `quality_probe`, `broker_sync_probe`, `universe_scan_probe`, `sandbox_probe`,
  `market_opportunity_probe`, `system_improvement_probe`, `approval_queue_item`,
  `system_prompted_idea`.
- Each card carries: `id, title, category, severity_or_priority, status,
  source_artifact, created_at, updated_at, reason, evidence, recommended_next_action,
  allowed_actions, blocked_actions, observe_only`.
- **Visual separation is mandatory**: Broken/degraded (fix) vs Market opportunities
  (research/review) vs System improvements (make the project better) render in three
  clearly distinct zones.
- Loaders must tolerate missing/malformed artifacts (existing pattern; tested).

---

## 13. Approval queues — `PARTIAL` (operator-control reusable)

Artifact-based; execute nothing. Reuse the operator-control append-only + audit model.

- Market-opportunity actions: `approve_to_watchlist_review, reject, keep_watching,
  request_deeper_research, send_to_sandbox, add_to_boom_bucket_review`.
- System-improvement actions: `approve_for_implementation, reject, defer,
  request_more_detail, mark_duplicate, mark_completed, create_claude_code_prompt`.
- Health-remediation actions (exist via operator-control): `create_health_remediation_prompt,
  mark_acknowledged, mark_resolved, defer, request_more_diagnostics`.
- Rules (tested): approving an opportunity never trades and at most → watchlist review;
  approving an improvement never edits code and at most → Claude Code prompt; all
  decisions append-only; rejections/deferrals enforce cooldown (Section 15).
- Artifacts: `outputs/latest/operator_action_queue.json`,
  `outputs/latest/system_improvement_action_queue.json`,
  `outputs/sandbox/opportunity_approval_queue.json`,
  `outputs/policy/user_decisions.jsonl`,
  `outputs/policy/system_improvement_decisions.jsonl`.

---

## 14. Daily system-improvement skill — `MISSING` (the flagship new skill)

A **new** skill `.claude/commands/daily-system-improvement.md` (Type C), distinct from
`daily-tool-analysis` (health) and from any market-opportunity prompt (Type B). Backed
by a deterministic producer `portfolio_automation/system_improvement.py` so it works
with or without an LLM (LLM optional, behind `ai_budget`).

- Inspects: `daily_run_status`, pipeline health, data-quality artifacts, memo, GUI
  artifacts, scanner outputs, sandbox outputs, confidence-calibration, outcome
  tracking, `ai_budget_summary`, docs freshness (doc-audit), test results if available,
  `.agent/project_state.yaml` roadmap.
- Emits ideas (categories): `reliability, observability, dashboard_ux, mobile_ux,
  data_quality, artifact_contract, scanner_coverage, sandbox_quality, pattern_memory,
  confidence_calibration, documentation, testing, security_privacy, performance,
  cost_budget, roadmap_alignment, developer_experience`.
- Idea model fields: `id, title, category, source, created_at, updated_at, status,
  priority, impact_score, urgency_score, effort_score, risk_score, confidence_score,
  roadmap_alignment_score, final_rank_score, summary, evidence, affected_modules,
  affected_artifacts, proposed_change, acceptance_criteria, suggested_tests,
  safety_constraints, blocked_actions, implementation_prompt, owner_decision,
  duplicate_of, cooldown_until, observe_only`.
- Artifacts: `outputs/latest/system_improvement_ideas.json`,
  `system_improvement_brief.md`, `system_improvement_scorecard.json`;
  `outputs/policy/system_improvement_history.jsonl`,
  `system_improvement_decisions.jsonl`, `system_improvement_outcomes.jsonl`.
- **Must not** emit market buy/sell/hold recommendations (tested). Roadmap-aligned:
  reads `next_official_step` and scores `roadmap_alignment`.

---

## 15. Duplicate detection & cooldown — `MISSING`

Shared helper in `system_improvement.py` + opportunity layer.

- Rejected ideas suppressed for `cooldown_days` (config, default 14).
- Deferred ideas resurface after `cooldown_until`.
- Approved → queue; completed → no repeat unless regression evidence appears.
- Similar open ideas → `duplicate_of` (title/affected-module/category similarity).
- Repeated market opportunities consolidate by theme/candidate.
- Tested with fixtures for each rule.

---

## 16. Claude Code prompt / launch — `PARTIAL` (generator exists for health)

`operator_control/worker_prompts.py` already renders health-remediation prompts with
forbiddens + required reading. Extend templates; reuse the renderer.

- New docs (templates): `docs/prompts/CLAUDE_CODE_HEALTH_REMEDIATION_PROMPT.md`,
  `CLAUDE_CODE_SYSTEM_IMPROVEMENT_PROMPT.md`,
  `CLAUDE_CODE_MARKET_OPPORTUNITY_RESEARCH_PROMPT.md`.
- Every generated implementation prompt includes: repo context, exact problem,
  evidence, affected modules, safety constraints, implementation scope, files to
  inspect, acceptance criteria, tests to run, docs to update, forbidden changes, final
  report format.
- All prompts forbid: auto-trading, order placement, broker writes, money movement,
  unrelated refactors, and changing protected scoring/decision logic without explicit
  approval. (Reuse `GLOBAL_FORBIDDEN_ACTIONS` + protected-path list.)
- Launch: the GUI "Claude Code Launch Center" shows the generated prompt with a
  **copy** button; autonomous dispatch stays behind the existing 3-part gate. The user
  launches Claude Code; the system never edits code on its own.

---

## 17. Mobile-friendly operator workflow — `PARTIAL`

Build on existing `bottom_nav` + `mobile_status_bar`.

- Mobile review mode supports: daily portfolio brief; broker sync status; top system
  health issues; top market-opportunity candidates; top system-improvement ideas;
  actions approve/reject/defer/keep-watching/request-research; copy/launch Claude Code
  prompt where supported; visible advisory-only/observe-only labels; visible blocked
  actions.
- Large tap targets, single-column stacking, sticky status, tables→cards. No new JS
  framework (HTMX only, per existing stack).

---

## 18. Artifact contract plan

Every new artifact is registered in `artifact_registry.yaml` (path, namespace, role,
cadence, producer, consumers, severity_if_missing, consumer_status) and gets a contract
test. Summary (R/L = replace-latest, A = append-only):

| Artifact | NS | Writer | Mode | Degraded behavior |
|---|---|---|---|---|
| `universe_scan_candidates.json` | sandbox | universe_scanner | R/L | empty list + `degraded_mode` |
| `opportunity_radar.json` | sandbox | universe_scanner/opportunity_scoring | R/L | empty + degraded |
| `private_ipo_watchlist.json` | sandbox | universe_scanner | R/L | empty |
| `theme_candidates.json` | sandbox | universe_scanner | R/L | empty |
| `market_opportunity_prompts.json` | sandbox | market_opportunity_prompts | R/L | empty (LLM off → keyword) |
| `market_opportunity_review_cards.json` | sandbox | market_opportunity_prompts | R/L | empty |
| `opportunity_approval_queue.json` | sandbox | approval layer | R/L | empty |
| `shadow_opportunity_tracking.json` | sandbox | shadow_tracker | R/L | empty |
| `shadow_portfolios.json` | sandbox | shadow_tracker | R/L | last-good or empty |
| `strategy_comparison.json` | sandbox | shadow_tracker | R/L | empty |
| `candidate_promotion_review.json` | sandbox | shadow_tracker | R/L | empty |
| `broker_aware_portfolio.json` | portfolio | holdings_resolver | R/L | config-source fallback |
| `pattern_events.jsonl` | policy | event_store | A | skip on error (non-fatal) |
| `opportunity_events.jsonl` | policy | event_store | A | skip |
| `user_action_log.jsonl` | policy | approval layer | A | skip |
| `outcome_events.jsonl` | policy | event_store | A | skip |
| `system_improvement_ideas.json` | latest | system_improvement | R/L | empty + degraded |
| `system_improvement_brief.md` | latest | system_improvement | R/L | "no ideas today" |
| `system_improvement_scorecard.json` | latest | system_improvement | R/L | empty |
| `system_improvement_history.jsonl` | policy | system_improvement | A | skip |
| `system_improvement_decisions.jsonl` | policy | approval layer | A | skip |
| `system_improvement_outcomes.jsonl` | policy | system_improvement | A | skip |
| `operator_action_queue.json` | latest | approval layer | R/L | empty |
| `system_improvement_action_queue.json` | latest | approval layer | R/L | empty |
| `user_decisions.jsonl` | policy | approval layer | A | skip |

All carry `observe_only: true`. JSONL retention: rotate/compact yearly (Open Q 23.9).

---

## 19. Testing plan

Mirror existing patterns (AST scans, `test_observe_only_*`, sandbox-only-write,
registry contract, GUI loader tolerance). Required tests:

- No broker write/order/trade methods (extend existing AST scan to any new broker code).
- Missing Schwab credentials → graceful (exists; keep).
- Secrets/tokens/account numbers redacted/masked in every new artifact.
- `holdings_resolver`: stale/missing broker → config fallback; fresh → broker; flag-off
  → decision_plan unchanged.
- Universe scanner writes sandbox only; candidates never reach `decision_plan.json`.
- Private candidates never typed as tradeable; never priced as tickers.
- Opportunity scoring deterministic; high boom alone cannot promote; low investability
  caps status; penalties surfaced.
- Shadow tracking writes sandbox only; no real positions.
- Approval queues execute nothing; decisions append-only; cooldown suppresses spam.
- Dashboard loaders tolerate missing/malformed artifacts for every new card.
- System-improvement idea schema valid; skill emits no market buy/sell/hold.
- Duplicate detection / cooldown rules.
- Prompt generator output contains the full safety-constraint block + forbiddens.
- Daily pipeline: each new skill enabled / disabled / failing → non-fatal, default-off.
- Artifact-registry contract test for each new artifact.
- Event store: append-only, envelope schema, non-fatal on write error.

---

## 20. Documentation update plan

Update: `README.md` (lanes + new skills), `ARCHITECTURE_MAP.md` (four lanes),
`OUTPUT_ARTIFACT_CONTRACTS.md` (all new artifacts), `PIPELINE_RUNBOOK.md` (new stages),
`RUN_MODE_GOVERNANCE.md` + `DATA_GOVERNANCE.md` (confirm sandbox/policy placement),
`SCORING_AND_CONFIDENCE.md` (opportunity scoring is **separate** from protected scores),
`CONFIDENCE_CALIBRATION.md` (event spine inputs), dashboard docs, Schwab docs (link
existing), and new docs: `docs/UNIVERSE_SCANNER.md`, `docs/OPPORTUNITY_RADAR.md`,
`docs/SYSTEM_IMPROVEMENT_SKILL.md`, `docs/SHADOW_PORTFOLIOS.md`,
`docs/prompts/*`. Update `.agent/project_state.yaml` roadmap entries per phase.
Every doc must restate: advisory-only, broker read-only, official vs sandbox, no
auto-trading, no broker writes, how to run/review/approve/reject/defer.

---

## 21. Phased implementation roadmap

Each phase: additive, default-off, independently shippable, with rollback = remove the
new module + flag (no existing behavior touched). "Creds?" = needs external
credentials. "Approval?" = needs explicit user go-ahead before merge to main.

- **Phase 0 — Audit & spec.** *(this document)* Creds: no. Approval: no.
- **Phase 1 — Artifact contracts & models.** Define dataclasses/schemas + registry
  entries for all new artifacts; no producers yet. Tests: schema + registry. Risk: low.
  Creds: no. Approval: no.
- **Phase 2 — Dashboard data-model expansion.** Add card categories + loaders that
  tolerate-absent for every future artifact (render "not yet produced"). Files:
  `gui_v2/data/*`, `operator_panel.html`, probe `category`. Tests: loader tolerance.
  Creds: no. Approval: no.
- **Phase 3 — Daily system-improvement skill.** `system_improvement.py` (deterministic)
  + `.claude/commands/daily-system-improvement.md` + artifacts. Tests: schema, no
  market verbs, degraded. Creds: no (LLM optional). Approval: skill file = oversight
  edit → **yes** (per `feedback_oversight_config_needs_explicit_signoff`).
- **Phase 4 — System-improvement approval queue + prompt generator.** Reuse
  operator-control append-only + `worker_prompts`. Add Type-C template. Tests: append-
  only, cooldown, prompt safety block. Creds: no. Approval: yes (touches operator plane).
- **Phase 5 — Universe scanner extension.** `universe_scanner.py` +
  `universe_lists.yaml` → sandbox artifacts. Tests: sandbox-only, no decision_plan,
  private typing. Creds: no (FMP free tier). Approval: no.
- **Phase 6 — Opportunity scoring + radar.** `opportunity_scoring.py` + radar artifact.
  Tests: deterministic, boom-alone rule, penalties. Creds: no. Approval: no.
- **Phase 7 — Sandbox shadow tracking + shadow portfolios.** `sandbox/shadow_tracker.py`.
  Tests: sandbox-only, no real positions. Creds: no. Approval: no.
- **Phase 8 — Market-opportunity prompt integration.** `market_opportunity_prompts.py`
  on the existing LLM layer + opportunity approval queue. Tests: no official writes,
  ai_budget respected, keyword fallback. Creds: OpenAI key (optional). Approval: no.
- **Phase 9 — Schwab read-only abstraction polish.** Optional `brokers/base.py`
  Protocol + any `outputs/portfolio/` alias. Tests: extend AST + alias contract. Creds:
  Schwab (for live; stub works without). Approval: no.
- **Phase 10 — Broker-aware portfolio manager.** `holdings_resolver.py` + gated input +
  `broker_aware_portfolio.json`. Tests: fallback, confidence-on-stale, flag-off
  identity, no writes. Creds: Schwab (optional; config fallback otherwise). Approval:
  **yes** (touches the official-lane input path).
- **Phase 11 — Pattern-recognition event store.** `event_store.py` + 4 JSONL streams +
  wiring into calibration/pattern_learning. Tests: append-only, envelope, non-fatal.
  Creds: no. Approval: no.
- **Phase 12 — Dashboard UI / mobile workflow.** Render all new cards + mobile review
  mode. Tests: render + mobile responsive markers. Creds: no. Approval: no.
- **Phase 13 — Claude Code launch/copy center.** GUI surface for prompts (copy +
  gated dispatch). Tests: copy present, no auto-exec. Creds: no. Approval: no.
- **Phase 14 — Documentation & roadmap.** Per Section 20. Creds: no. Approval: no.
- **Phase 15 — Full validation & safety review.** Full suite + safety checklist
  (Section 4) + `portfolio-test-reviewer` + architecture review. Creds: no. Approval:
  **yes** (final sign-off before treating live).

---

## 22. Recommended implementation order (risk-reducing)

1. **Contracts/models first** (Phase 1) — everything depends on stable schemas.
2. **Dashboard loaders second** (Phase 2) — tolerate-absent so later producers light
   up cards with zero GUI churn.
3. **System-improvement skill early** (Phases 3–4) — pure-internal, no market/broker
   risk, immediate operator value, exercises the approval+prompt path.
4. **Sandbox/universe/opportunity/shadow** (Phases 5–8) — research lane, sandbox-only,
   no official-lane risk.
5. **Event store** (Phase 11) — can slot in parallel after Phase 1; feeds learning.
6. **Broker read-only polish then broker-aware manager** (Phases 9–10) — read-only and
   stubbed until credentials exist; official-lane wiring (Phase 10) only **after**
   contracts + tests exist and behind a default-off flag.
7. **UI/mobile + launch center + docs + final review** (Phases 12–15) last.

Official decision-engine wiring is the **last** functional step and is gated +
reversible.

---

## 23. Open questions / decisions needed

1. Schwab API access readiness (live app approval, redirect URI on VPS)?
2. Where do broker OAuth tokens live in production — local `data/` (current), VPS-only,
   or an encrypted store?
3. Primary dashboard target for new cards: `gui_v2` (FastAPI, recommended) — confirm
   Streamlit stays legacy-only?
4. Default universe lists (which ETFs/sectors/commodities/themes ship as defaults)?
5. Boom-bucket size / cap (how many boom candidates tracked at once)?
6. Approval actions: file-based artifacts only, or dashboard-interactive POST (the
   operator-control plane supports interactive — confirm scope)?
7. Claude Code launch from phone/VPS: copy-only on mobile, dispatch desktop-only?
8. System-improvement skill: deterministic-first (recommended) vs OpenAI-assisted from
   day one?
9. Retention/compaction policy + latest-vs-append for the new JSONL event streams?
10. Should `broker_aware_portfolio` ever feed `decision_plan` inputs, or remain a
    read-only side-panel only (recommended: side-panel until explicitly approved)?

---

## 24. Final output (summary)

- **Summary:** evolve the repo into a read-only, broker-aware, sandbox-driven,
  self-improving portfolio intelligence platform across 4 lanes; ~50% of the
  architecture already exists and is reused, not rebuilt.
- **Files inspected:** `README.md`, `CLAUDE.md`, `AGENTS.md`,
  `.agent/project_state.yaml`, `docs/ARCHITECTURE_MAP.md` + governance/scoring docs,
  `portfolio_automation/{decision_engine,data_governance,run_mode_governance,
  pattern_learning,confidence_calibration,decision_outcome_tracker}.py`,
  `portfolio_automation/brokers/*`, `portfolio_automation/discovery/*`,
  `watchlist_scanner/*`, `universe/*`, `scanner/*`, `theme_engine/*`,
  `theme_discovery/*`, `scraped_intel/*`, `operator_control/*`, `gui_v2/*`,
  `config.json`, `config/signal_registry.yaml`, `artifact_registry.yaml`, `tests/*`
  (~208), `outputs/*` namespaces.
- **Current-state findings:** Sections 2 + 5–16 (per-workstream EXISTS/PARTIAL/MISSING).
- **Target architecture:** Section 3 (four lanes).
- **Proposed artifacts:** Section 18 (25 new artifacts, namespaced + degraded behavior).
- **Phase roadmap:** Section 21 (Phases 0–15).
- **Test plan:** Section 19. **Docs plan:** Section 20. **Safety constraints:** Section 4.
- **Open questions:** Section 23.
- **Next recommended prompt to begin implementation (Phase 1):**

  > "Implement Phase 1 of `docs/NEXT_STAGE_PORTFOLIO_INTELLIGENCE_SPEC.md`: add the
  > dataclasses/schemas and `artifact_registry.yaml` entries for all new artifacts in
  > Section 18 — **no producers, no pipeline wiring, no runtime behavior change yet**.
  > Every new artifact: `observe_only: true`, correct `OutputNamespace`, contract +
  > registry test. Respect all Section 4 safety constraints. Do not touch the decision
  > engine, scoring, broker write paths, or protected semantics. End with the standard
  > final report."

---

*This is a planning/specification document. It changes no runtime behavior and
introduces no auto-trading, broker writes, order endpoints, or market buy/sell
execution. Implementation proceeds only on explicit, phase-by-phase instruction.*
