# Roadmap

## Research-Backed Strategy Lab — built + ENABLED (2026-06-12)

- **New sandbox sub-suite** on top of the Portfolio Simulation Suite: academic
  strategy families as tactics (Markowitz mean-variance, Fama-French factor tilt,
  Jegadeesh-Titman momentum, Antonacci dual-momentum, risk-parity-lite, 60/40,
  Moreira-Muir vol-managed, Black-Litterman blend) + the suite's 6 shadow + 8
  profiles + SPY/QQQ = ~24 strategies ranked by a **master strategy score**
  (after-cost-ish, risk-adjusted excess vs SPY, minus overfit/turnover/tax/
  concentration/leverage).
- **Walk-forward OOS validation** (train->test->roll) feeds an overfit penalty;
  **Fama-French factor attribution** explains alpha vs factor exposure (offline,
  degrades gracefully). Modules: research_library, strategy_score, walk_forward,
  factor_data, factor_attribution, run_strategy_lab, strategy_lab_health.
- **ENABLED for production** (config portfolio_sim.enabled=true + strategy_lab.enabled
  =true); weekly run_weekly_safe.sh stage; 5 artifact-registry rows; preflight.
- **Health probe**: strategy_lab_health assessor + new `/strategy-lab-analysis`
  skill + monthly-tool-analysis wiring (GREEN/AMBER/RED; flags fresh-but-empty,
  undocumented tactics, failing-OOS, missing factor data).
- **GUI**: Strategy Lab tab master leaderboard (score/excess/OOS/academic-basis).
- ~42 tests; full suite 7315 pass / 3 pre-existing fails. Docs:
  docs/RESEARCH_STRATEGY_LAB.md + specs/plan. Deferred (documented): regime sims,
  crowd event studies, ensemble, cost/tax model.


## Portfolio Simulation Suite — built (2026-06-12)

- **New sandbox-only, observe-only package** `portfolio_automation/portfolio_sim/`.
  Three sub-projects, all spec'd + planned (`docs/superpowers/specs/2026-06-12-*`,
  `docs/superpowers/plans/2026-06-12-portfolio-sim-suite.md`) then built in one go:
  1. **Historical backtest engine** — the operator's real portfolio + 6 shadow
     portfolios + 8 materialized strategy profiles + SPY/QQQ, over period windows
     (YTD / quarterly / monthly / trailing). **Objective: maximize excess return
     vs the S&P 500** (config `portfolio_sim`), portfolio-anchored, with
     time-weighted + DCA dollar paths and a contribution-sensitivity sweep
     ("based on how much you put in"). Pluggable rebalance policies.
  2. **Crowd-signal tactic** — capped sleeve toward useful crowd states +
     avoid-overlay on caution states; forward shadow-track (real) + labeled
     volume/momentum proxy backtest.
  3. **Forward Monte-Carlo projection** — block-bootstrap of historical monthly
     return vectors; terminal-balance percentiles, prob-reach-target, drawdown
     distribution; seeded/reproducible; "illustration, not forecast".
- **Strategy Documentation discipline** — `strategy_docs.py` producer +
  `/strategy-catalog` skill + a CLAUDE.md *Strategy Documentation Requirement*
  rule: every tactic ships a catalog entry; undocumented tactics don't surface.
- **Governance**: reads HISTORICAL archive, writes SANDBOX; observe-only,
  default-disabled (`config portfolio_sim.enabled=false`); never writes
  decision_plan/config/registry; no trade verbs; run-mode-gated.
- **Wired**: 2 weekly `run_weekly_safe.sh` stages, GUI Strategy Lab
  Backtest + Projection sections, 5 artifact-registry rows, monthly-tool-analysis
  quant-lens health + content-liveness, preflight. ~103 tests. Ships inert.

## Public Knowledge Velocity Layer / Crowd Radar — built (2026-06-12)

- **New sandbox-only, observe-only module** `portfolio_automation/social_intelligence/`
  (Public Knowledge Velocity Layer; GUI label **Crowd Radar**). Classifies the
  *state of public knowledge* around tickers from API-compliant public discussion
  (Reddit-first): `dormant_noise / emerging_dd / crowd_validation / hype_acceleration
  / reflexive_squeeze_risk / known_news_echo / crowd_exhaustion / contrarian_neglect`.
  8 modules: source-governance registry, feature-gated Reddit connector, robust
  ticker extractor, feature aggregation, crowd-state classifier, sample-gated
  social-signal backtest, orchestrator.
- **Hard governance.** Sandbox-only writes (run-mode-gated), `observe_only=true`,
  research-only `recommended_next_step` vocabulary (runtime-asserted to exclude
  trade verbs), capped `crowd_research_priority_score`. Never writes
  `decision_plan.json` / `signal_registry.yaml`. **Default-disabled**
  (`config.json crowd_radar.enabled=false`); kill-switch
  `config/crowd_radar.DISABLED` / `STOCKBOT_CROWD_RADAR_DISABLED=1`. Missing
  REDDIT_* creds degrade to `no_credentials` — never crash the daily run.
- **Wired**: `run_daily_safe.sh` Stage 9c (discovery mode, non-blocking), GUI
  "Crowd Radar" tab, daily-memo "Crowd Radar — Sandbox Research" section, 5
  artifact-registry rows, `/daily-tool-analysis` content-liveness check +
  `portfolio-discovery-health` dispatch. 59 tests (incl. full-pipeline e2e + context-join). See
  `docs/PUBLIC_KNOWLEDGE_VELOCITY_LAYER.md`.
- **Roadmap status.** `next_official_step` stays `observe_and_iterate`. The layer
  ships inert; activation = set `enabled=true` + provision Reddit OAuth creds
  after the source-terms review. Backtest stays `insufficient_data` until enough
  resolved forward-return observations accumulate.

## Next-Stage Portfolio Intelligence — built + activated (2026-06-10)

- **Phases 1–15 shipped (concurrent session, 13 commits `af6f63be..a981da01`).**
  The full `docs/NEXT_STAGE_PORTFOLIO_INTELLIGENCE_SPEC.md`: Phase 1 artifact
  contracts (+418 registry rows), broad-market universe scanner + opportunity
  scoring/radar, sandbox shadow tracking, read-only broker Protocol +
  broker-aware holdings resolver (advisory side-panel), learning-loop event
  store, daily system-improvement skill, market-opportunity prompts + approval
  queues, **Phase 11A Multi-Strategy Portfolio Objective Engine (8 profiles)**,
  Strategy Lab dashboard view, orchestrator + `docs/NEXT_STAGE_IMPLEMENTATION.md`.
  140 tests. All additive / observe-only; never writes `decision_plan.json`.
- **Activated into production (this session, 3 commits).** The workstream was
  built but **dormant** (orchestrator invoked only by its test). Wired the
  next-stage orchestrator as **Stage 10b** of `scripts/run_daily_safe.sh` and the
  artifact-registry validator as **Stage 12**; registered the 5 Schwab broker
  artifacts. Lane is pure (no LLM/FMP/network), non-fatal per producer,
  `observe_only`. First live run: 6/7 steps ok (broker_aware degrades-to-config,
  Schwab unconfigured); registry coverage 46→70/91, 0 required-missing, 0
  critical. Commits `16c374bd`, `256058e3`, `d69b97d4`. Dashboard restarted;
  full VPS suite 7041 passed (3 known-pre-existing failures, not regressions).
- **Roadmap status.** `next_official_step` stays `observe_and_iterate`
  (roadmap-control is GPT's role); recorded as built+activated in
  `.agent/project_state.yaml:next_stage_intelligence` and
  `.agent/phase_status.yaml`.

## Operator control / Claude Code worker (2026-06-09)

- **Phase 1 — shipped (branch `operator-control-work-orders`).** Probe-driven,
  allowlisted, observe-only work-order plane. The dashboard turns existing
  health/quality probes into work orders (create-only — the web app never
  executes a worker, runs shell, or touches trade/broker/scoring logic). New
  `operator_control/` package (probe + skill registries, repair policy,
  append-only work orders + audit, worker-prompt generation, CLI); GUI adapter
  + `operator_panel.html` wired into all five persona tabs; 50 new tests. See
  `docs/operator_control.md`.
- **Phase 2 — shipped (branch `operator-control-worker-runner`).** CLI-only
  **worker runner** (`operator_control/worker_runner.py`). Default = scaffolding
  (isolated `git worktree` + prompt for a human to run). Autonomous headless path
  is hybrid/default-inert behind a three-part gate (config flag +
  `STOCKBOT_OPERATOR_WORKER_AUTONOMOUS=1` + no `config/operator_worker.DISABLED`
  kill-switch), modeled on `auto_apply`. Autonomous may run any mode incl.
  `safe_repair`, contained by worktree + never-merge/never-push + a deterministic
  protected-path diff guard + the skill's test gate + single-flight lock. New:
  `protected_paths.py`, `worktree.py`, `worker_runner.py`, `worker_settings.json`,
  read-only System-tab runner card; 24 new tests (claude subprocess mocked). See
  `docs/operator_control_worker_runner.md`.
- **Phase 3 — shipped (branch `operator-control-worker-runner`).** Completes the
  arc. Default-inert **drain** (`worker_runner drain` + `scripts/operator_worker_drain.sh`)
  that runs eligible orders through the already-gated autonomous path in a
  bounded loop — a NO-OP unless autonomous is enabled; crontab line documented,
  not installed. Read-only **report-review** surface
  (`GET /dashboard/operator/report/<id>`, regex-guarded against traversal) plus
  queue links from completed/failed orders. See
  `docs/operator_control_worker_runner.md`. The operator-control system
  (create → run → schedule+review) is now feature-complete; further work is
  optional polish.
- **Phase 4 — shipped (operator-directed).** GUI **"Repair" button** →
  `POST /dashboard/operator/dispatch` → creates + approves (the click is the
  only gate) + spawns a DETACHED unattended worker that auto-diagnoses then
  fixes. Auth via the box's Claude Code login (worker strips `ANTHROPIC_API_KEY`,
  which otherwise 401s); `config.json` ships `autonomous_worker.enabled=true`
  and the dispatch sets the per-run env. New deterministic **production-impact
  gate** (snapshots `main` + `config.json` + `signal_registry.yaml` +
  `decision_plan.json`; any change → fail + `worker_production_impact`) so a
  worker can never reach production. Separate **operational cost ledger**
  (`worker_cost_log.jsonl`, `worker_runner cost`) — tracked per run + why, NO
  cap, explicitly NOT the FMP/AI decision budget. Residual risk: worker runs as
  root with git-isolation only — recommend an unprivileged user/container before
  heavy `safe_repair` reliance. See `docs/operator_control_worker_runner.md`.
- **Analysis+health pairing — done.** operator-control liveness wired into the
  daily / monthly / yearly tiers (process-analyst lens).
- **Dashboard auto-update with manual intervention — shipped.** The dashboard
  auto-detects when its served code is behind `origin/main` (startup SHA stamp
  vs a throttled read-only fetch) and surfaces a Deployment card + banner
  (Phase A, read-only). Phase B shows the exact manual update command + records
  a `deploy_update_requested` audit (zero privilege). Phase C (default-inert,
  gated by `GUI_V2_DEPLOY_APPLY=1` + kill-switch) adds a one-click "Apply update
  & restart" that spawns a detached fast-forward-only updater
  (`scripts/dashboard_update.sh`) — never unattended, never non-ff, the web
  process never restarts itself. See `docs/dashboard_auto_update.md`.

## Phase 0 — Infrastructure & Data Governance (In Progress)

### Step 1 — user_id Schema Migration (Complete)

`user_id TEXT NOT NULL DEFAULT 'owner'` added to `run_history` and `snapshots`.
Migration file at `portfolio_automation/migrations/001_add_user_id.py`. Deployed to VPS.
Single-user behavior unchanged. Groundwork for future multi-user scope.

### Step 2 — Data Governance Namespace Utilities (Complete)

Data governance namespace utilities added at `portfolio_automation/data_governance.py`.
Defines `OutputNamespace` enum (LIVE, HISTORICAL, SANDBOX, POLICY, PORTFOLIO, LATEST, USER),
`DataGovernanceError`, `safe_write_text`, `safe_write_json`, `validate_output_path`, and helpers.
Existing outputs remain fully backward-compatible; future modules should use namespace-aware writes.
See `docs/DATA_GOVERNANCE.md`.

### Step 2b — Historical Replay Namespace Enforcement (Complete)

Historical Replay is now the first real consumer of the data governance layer.
`replay_reports.py` and `replay_runner.py` use `safe_write_json` / `safe_write_text`
with `OutputNamespace.HISTORICAL` for all file writes.
A live-path guard (`_assert_safe_replay_output_dir`) rejects any attempt to write to
`outputs/latest`, `outputs/policy`, `outputs/portfolio`, or other live namespaces.
All artifact paths remain identical (`outputs/backtest/`). 41 governance tests added.
Backward-compatible: existing replay command and existing tests unchanged.

### Step 3 — Config-driven Signal Registry (Complete)

Signal definitions centralized in `config/signal_registry.yaml` and
`portfolio_automation/signal_registry.py`.

Defines 10 known signal IDs seeded from the codebase:
- `STRONG_MOVE_UP`, `STRONG_MOVE_DOWN`, `VOLUME_SPIKE`, `BREAKOUT_PROXY`,
  `VOLATILITY_EXPANSION` — from `event_detection.py` EventType enum
- `LEVERAGE_VIOLATION`, `CONCENTRATION_VIOLATION`, `DRIFT_VIOLATION` — from
  `portfolio_automation/decision_engine.py` violation_type strings
- `PORTFOLIO_DRIFT` — finance advisory recommendation id
- `HISTORICAL_MOMENTUM_PROXY` — from `replay_decision_simulator.py` STRATEGY_NAME

`SignalRegistry` provides: `get`, `require`, `all`, `enabled`, `by_category`,
`by_source_domain`, `is_actionable`, `is_discovery_only`, `requires_corroboration`,
`validate_signal_id`, and `annotate_signal`.

Governance enforced at load time:
- `actionable` and `discovery_only` cannot both be `true`
- `discovery_only: true` requires `requires_corroboration: true`
- `default_weight` must be in `[0.0, 1.0]`
- Duplicate `signal_id` rejected
- Unknown signals are unconditionally non-actionable

No live scoring, allocation, or recommendation behavior changed.
Additive and backward-compatible. 56 tests added.
See `docs/SIGNAL_REGISTRY.md`.

### Step 5 — AI Cost Budget Wrapper (Complete)

Observe-only AI usage tracking and cost guardrail layer added at
`portfolio_automation/ai_budget.py`.

Tracks token usage and estimated cost across all optional AI/LLM calls.
Enforces configurable daily and monthly limits in opt-in hard mode.
Default behavior is `observe_only=True` — never blocks, only records.

Key components:
- `AIBudgetConfig` — enable/disable, observe_only, daily/monthly limits
- `AIUsageEvent` — per-call record with timestamp, tokens, cost, allowed flag
- `AIBudgetSummary` — aggregated daily/monthly totals with warning/blocked status
- `estimate_ai_cost()` — static pricing table covering Anthropic, OpenAI, Ollama/local
- `check_ai_budget()` — returns AIUsageEvent; warns at threshold, blocks when
  `observe_only=False` and limit exceeded
- `with_ai_budget` — context manager; raises `AIBudgetExceeded` only when not observe_only
- `record_ai_usage_event()` — appends to `outputs/policy/ai_usage_events.jsonl` (POLICY)
- `load_recent_ai_usage_events()` — tolerates missing file and malformed lines
- `write_ai_budget_summary()` — writes JSON + Markdown to `outputs/latest/` (LATEST)

Pricing coverage: claude-haiku-4-5-20251001, claude-sonnet-4-6, claude-opus-4-7,
gpt-4o-mini, gpt-4o, and all Ollama/local models ($0). Unknown models annotated
with `unknown_pricing: true` in event metadata, cost estimated as $0.00.

Pipeline integration: non-blocking summary write after all AI call sections.
No existing AI call behavior changed. 62 tests added.
See `docs/AI_BUDGET.md`.

### Step 4 — Data Quality Monitor (Complete)

Observe-only data quality layer added at `portfolio_automation/data_quality_monitor.py`.

Detects 13 issue types across two severity levels (critical, warning) and one
informational level, evaluated per-symbol and in aggregate:
- `MISSING_PRICE` (critical), `STALE_PRICE` (warning), `CACHE_ONLY` (warning)
- `FALLBACK_USED` (warning), `MISSING_FUNDAMENTALS` (warning)
- `MISSING_NEWS` (info), `MIXED_SOURCE` (info), `SOURCE_ERROR` (warning)
- `UNKNOWN_SOURCE` (warning), `EXCESSIVE_FALLBACK_RATE` (warning)
- `EXCESSIVE_MISSING_PRICE_RATE` (critical), `DEGRADED_MODE` (warning)
- `INSUFFICIENT_DATA` (info)

Configurable thresholds via `DataQualityConfig` (stale_quote_minutes=1440,
max_fallback_rate_warning=0.30, max_missing_price_rate_critical=0.10).

Artifacts written via `OutputNamespace.LATEST`:
- `outputs/latest/data_quality_report.json`
- `outputs/latest/data_quality_report.md`

Pipeline integration: non-blocking call after watchlist scanner completes;
exceptions caught as warnings so the pipeline always continues.

No scoring, allocation, recommendation, or confidence behavior changed.
Additive and backward-compatible. Tests added.
See `docs/DATA_QUALITY_MONITOR.md`.

---

## Completed: Decision Engine Foundation + Observe-Only Integration

What was built:

- `portfolio_automation/decision_engine.py`
- observe-only integration in `main.py`
- `outputs/latest/decision_plan.json`
- `outputs/latest/decision_plan.md`
- `tests/test_decision_engine.py`
- `tests/test_decision_engine_pipeline.py`

What was completed inside this phase:

- module implemented
- pipeline tests added
- additive pipeline artifacts added
- consolidation and symbol-level dedup completed
- validated final output shape established

Why it matters:

- the system now has one central observe-only action-plan layer
- structural guardrails, portfolio actions, finance guidance, watchlist signals, and market opportunities can be compared in one ranked list
- conflict resolution is explicit instead of being left to downstream readers
- existing recommendation behavior and existing schemas remain unchanged

Current status:

- implemented
- tested
- wired into the daily pipeline in observe-only mode
- additive only, not a replacement for the current recommendation stack

## What "Observe-Only" Means Here

- Decision Engine artifacts are written in parallel with existing outputs
- current recommendation logic is still the operational source of advice
- no trade execution behavior is introduced
- existing consumers are not forced to adopt the decision plan yet

## Next

### Completed: GUI Decision Center v1

- implemented as a read-only Streamlit Decision Center
- consumes `outputs/latest/decision_plan.json`
- consumes `outputs/latest/system_decision_summary.json` when available
- shows an observe-only banner
- renders a compact summary first:
  - `Top Insight`
  - `Top Decisions` capped at `5`
  - `Capital Actions`
  - `Risk Focus` capped at `3`
  - `What Changed` capped at `3`
  - `System / Data Health` only when degraded or fallback context exists
- preserves full decision detail below the summary in the full queue
- uses short human-readable reasons instead of dumping raw long structural text
- preserves the observe-only and artifact-driven boundary

### AI Explanation Layer

Completed:

- implemented `portfolio_automation/decision_explainer.py`
- wired it into the pipeline after `decision_plan.json` is written
- writes:
  - `outputs/latest/decision_explanations.json`
  - `outputs/latest/decision_explanations.md`
- uses deterministic logic only in v1
- preserves source attribution and structural authority
- remains additive and read-only

Validated:

- compile checks passed
- `tests/test_decision_explainer.py`: `6 passed`
- `tests/test_decision_engine_pipeline.py`: `42 passed`
- VPS artifact check confirmed:
  - `available: True`
  - `observe_only: True`
  - `count: 5`

### Policy Feedback Loop Using Decision Outcomes

- measure how consolidated decisions perform over time
- compare decision-plan outcomes with later recommendation history
- tune precedence, suppression, and downgrade rules only after outcome evidence exists

---

## Phase 0 Complete

All Phase 0 Infrastructure & Data Governance steps are complete:

1. user_id schema migration
2. Data governance namespace utilities
3. Historical replay namespace enforcement
4. Config-driven signal registry
5. Data quality monitor
6. AI cost budget wrapper

The system now has:
- Namespace-governed output writes across all new layers
- A typed signal catalog with governance rules enforced at load
- Observe-only data quality visibility before scoring
- Observe-only AI cost tracking with optional hard enforcement

---

## Agent Orchestration Layer (Complete)

Repo-native orchestration structure created so Claude, Codex, and GPT can work
from the same project state, rules, roadmap, task templates, checklists, and handoff format.

Key files created:
- `.agent/project_state.yaml` — machine-readable source of truth for current phase, step, forbidden changes, role split, namespace policy
- `.agent/phase_status.yaml` — per-step roadmap status with prerequisites and notes
- `AGENTS.md` — Codex operating instructions
- `CLAUDE.md` (updated) — Claude operating instructions with orchestration context, VPS warning, final report format
- `docs/AGENT_OPERATING_MODEL.md` — role definitions and collaboration workflow
- `docs/AI_COLLABORATION_RUNBOOK.md` — how-to guide for starting features, prompting agents, VPS validation
- `.agent/task_templates/` — 5 reusable prompt templates (claude_feature, codex_docs, codex_review, vps_validation, final_report)
- `.agent/checklists/` — 5 validation checklists (feature_acceptance, regression, artifact_contract, deployment_validation, roadmap_drift)
- `.claude/agents/` — 3 Claude project subagents (portfolio-architect, portfolio-test-reviewer, portfolio-doc-writer)
- `.claude/skills/` — 3 Claude skills (portfolio-feature, portfolio-docs, portfolio-vps-validation)
- `.agents/skills/` — 3 Codex skills (portfolio-review, portfolio-docs, portfolio-task-packet)
- `scripts/agent_context_check.py` — lightweight context summary tool
- `tests/test_agent_context_check.py` — validation tests for the above

No portfolio runtime behavior changed. No output artifact schemas changed. Tests added.

---

## Confidence Calibration Feedback Loop (Complete)

Enhanced the existing `portfolio_automation/confidence_calibration.py` with per-signal and 5-bucket analysis, data quality integration, and LATEST namespace artifact writes.

Key additions:
- `evaluate_confidence_calibration()` — pure evaluation returning `ConfidenceCalibrationSummary` dataclass
- `write_confidence_calibration_report()` — loads outcomes + DQ report, evaluates, writes to `outputs/latest/`
- 5-bucket confidence system: very_low/low/medium/high/very_high
- Per-signal calibration grouped by `source` field; excludes signals with fewer than 5 resolved rows
- `discovery_only` signals always have `suggested_review=False`; no automatic registry edits
- `calibration_gap = average_confidence - hit_rate`; overconfident when gap > 0.15, underconfident when gap < -0.15
- Data quality warnings surfaced from `outputs/latest/data_quality_report.json`
- Signal registry consulted for `is_discovery_only` check; unknown signals treated as discovery-only
- POLICY write (legacy) unchanged; LATEST write added non-blocking via data governance layer
- `run_calibration()` still triggers both writes when `write_files=True`
- 65 new tests added (136 total); all pass

Artifacts:
- `outputs/latest/confidence_calibration.json` — enhanced report with `buckets_5`, `signal_results`, `dq_warnings`
- `outputs/latest/confidence_calibration.md` — human-readable enhanced calibration report
- `outputs/policy/confidence_calibration.json` — legacy report (GUI reads this)
- `outputs/policy/confidence_calibration.md` — legacy markdown

No live scoring, allocation, recommendation, or registry values changed.
See `docs/CONFIDENCE_CALIBRATION.md`.

---

## Run Mode Governance / Operating Mode Separation (Complete)

Centralized run-mode governance added at `portfolio_automation/run_mode_governance.py`.

Two-lane operating model enforced:
- **Official Lane**: `DAILY`, `MANUAL_UPDATE`, `WEEKLY_REVIEW` — write `outputs/latest/`, `outputs/policy/`, `outputs/portfolio/`
- **Research Lane**: `DISCOVERY`, `BACKTEST`, `HISTORICAL_REPLAY` — sandbox/simulation only

Key additions:
- `RunMode` enum with 6 canonical modes
- `RunModePolicy` dataclass (frozen) — per-mode permission set
- `RunModeContext` dataclass — active mode + resolved policy + approval state
- `RunModeViolation` exception
- `normalize_run_mode()` — canonical + legacy alias resolution (`weekly` → WEEKLY_REVIEW, `monthly` → WEEKLY_REVIEW)
- `validate_output_write()` — soft namespace permission check (returns bool)
- `assert_can_write_namespace()` — hard namespace enforcement (raises)
- `assert_can_update_portfolio_state()` — approval-gated portfolio mutation guard
- `assert_can_update_watchlist()` — approval-gated watchlist mutation guard
- `assert_can_emit_recommendation()` — recommendation emission guard
- `is_official_mode()` / `is_research_only_mode()` — lane detection
- `create_run_mode_context()` — convenience factory
- `can_execute_trades=False` enforced for every mode
- Non-blocking integration in `main.py` — normalizes + logs active mode and lane

Artifacts: no output files (pure in-memory governance layer)
Tests: `tests/test_run_mode_governance.py` — 132 passed
Docs: `docs/RUN_MODE_GOVERNANCE.md`

No live scoring, allocation, recommendation, or output schema changed.

---

## Discovery Engine Foundation (Complete)

Sandbox-only, research-lane candidate discovery added at `portfolio_automation/discovery/`.

Modules:
- `news_ticker_discovery.py` — deterministic cashtag/parenthetical/source-provided ticker extraction; noise-word filtering; known_universe allowlist
- `event_classifier.py` — keyword-based classification into 11 event types + unknown; risk_flag for legal/regulatory negative signals
- `candidate_promotion_engine.py` — scoring by mention count, source diversity, event confidence, risk penalty; statuses: DISCOVERED, WATCH, REJECTED only
- `discovery_memory.py` — persistent sandbox candidate memory; tolerates missing/corrupt files; accumulates mention_count and seen_runs
- `discovery_reports.py` — sandbox artifact writer (`emerging_candidates.json`, `rejected_candidates.json`, `discovery_memory.json`, `discovery_memo_section.md`) + `run_discovery_engine` orchestrator

Governance:
- All artifacts written to `outputs/sandbox/discovery/` only
- `can_execute_trades=False`, `discovery_only=True`, `sandbox_only=True` in every artifact
- Disclaimer on every artifact: "Discovery candidates are not buy/sell recommendations."
- `assert_can_write_namespace(..., "sandbox")` enforced before any I/O
- `DISCOVERY` and `BACKTEST` may write sandbox discovery artifacts
- `DAILY`, `MANUAL_UPDATE`, `WEEKLY_REVIEW`, and `HISTORICAL_REPLAY` raise `RunModeViolation` if they try to write
- Corroboration gates: `corroboration_required=True` on every candidate; `corroboration_met` computed by corroboration layer

Not done / pending:
- GUI discovery approval workflow
- Manual promotion proposal (MANUAL_UPDATE + approved=True)
- Daily Memo discovery section
- Historical replay/backtest for discovery candidates

Tests: `tests/discovery/` — 171 passed across 5 test files (plus 58 new in corroboration step)
Docs: `docs/DISCOVERY_ENGINE.md`

---

## Post-Phase-0 Next Steps

### GUI Data Quality + AI Budget + Calibration + Discovery Panels (Complete)

Four read-only GUI panels added to the Advanced Dashboard tab strip:

- **Data Quality Monitor** (`Data Quality` tab) — consumes `outputs/latest/data_quality_report.json`; badges: healthy/warning/critical/unavailable; shows issue breakdown by severity
- **AI Budget Summary** (`AI Budget` tab) — consumes `outputs/latest/ai_budget_summary.json`; badges: within budget/warning/blocked; advisory/observability only note
- **Confidence Calibration** (`Calibration` tab) — consumes `outputs/latest/confidence_calibration.json` (LATEST, with `buckets_5`); note: observe-only, does not automatically change scoring
- **Discovery Sandbox Status** (`Discovery` tab) — consumes `outputs/sandbox/discovery/`; clearly labeled research-only; shows watch/discovered/rejected candidates; no promote buttons

Loaders added to `gui_operator_data.py`:
- `load_data_quality_report(root)` — LATEST namespace
- `load_ai_budget_summary(root)` — LATEST namespace
- `load_confidence_calibration_latest(root)` — LATEST namespace (separate from existing POLICY loader)
- `load_discovery_sandbox_status(root)` — SANDBOX namespace

All panels are read-only. Missing artifacts degrade gracefully. No API calls, no AI calls, no buy/sell language.

Tests: `tests/test_gui_system_health_panels.py` — 66 passed

### Instrument AI Call Sites (Complete)

Instrumented the real LLM call site in `portfolio_automation/ai_decision_validator.py`.

Key findings and decisions:

- `decision_explainer.py` has **no actual LLM calls** — it is 100% deterministic
  rule-based logic. Nothing to instrument.
- `ai_decision_validator.py` has one real LLM call site: `_try_llm_enhance()` via
  `call_provider()`. This is the only instrumentation target.
- `call_provider()` returns plain text only (no response object with token usage).
  Token counts are estimated from text length (`len(text) // 4`), annotated with
  `metadata.usage_source="estimated_from_length"`.

Changes:

- Added `_estimate_tokens(text)` — rough character-based token estimator.
- Added `_record_validator_event(...)` — best-effort, never raises; wraps
  `check_ai_budget` + `record_ai_usage_event` in try/except with `logger.warning`.
- `_try_llm_enhance` now calls `_record_validator_event` after the LLM call
  (success → `status="success"`; failure → `status="error"`, `completion_tokens=0`).
- `base_dir` threaded from `run_ai_validation` → `build_ai_validation` → `_try_llm_enhance`
  → `_record_validator_event` so test temp dirs work correctly.
- Removed the `TODO` comment from `main.py`'s AI Budget section.

Constraints preserved:

- LLM call is opt-in (`AI_VALIDATOR_USE_LLM=1`); default pipeline makes no LLM calls.
- No extra AI/API calls for instrumentation.
- No scoring, allocation, recommendation, or discovery behavior changed.
- `record_ai_usage_event` failure never blocks the pipeline.

Tests added: `tests/test_ai_decision_validator.py` — 17 new tests in
`TestAiBudgetInstrumentation` (79 total in file).

---

### Discovery Corroboration Implementation (Complete)

Deterministic corroboration scoring layer added to the discovery engine.

New module `portfolio_automation/discovery/corroboration.py`:
- `CorroborationResult` dataclass with score, level, corroboration_met, and per-component fields
- `compute_corroboration()` — weighted formula: source_diversity 35%, mention 20%, event_strength 25%, persistence 20%, risk_penalty −0.20
- `CORROBORATION_MET_THRESHOLD = 0.65`

Levels: `none` (<0.30), `weak` (0.30–0.50), `moderate` (0.50–0.65), `strong` (≥0.65)
`corroboration_met = True` when score ≥ 0.65 (only at "strong" level).

Changes to existing modules:

- `candidate_promotion_engine.py`:
  - `DiscoveryCandidate` gained `corroboration_score` and `corroboration_level` fields
  - `score_candidate()` accepts `seen_runs: int = 0`; calls `compute_corroboration()` and sets all corroboration fields
  - `_determine_status()` requires `corroboration_met=True` for WATCH — high-score first-run candidates stay DISCOVERED until corroboration is met
  - `evaluate_candidates()` accepts `persistence_data: dict[str, int] | None` (ticker → seen_runs from prior memory)

- `discovery_reports.py`:
  - `run_discovery_engine` loads memory BEFORE calling `evaluate_candidates` to build `persistence_data` from prior runs
  - `_candidate_to_dict` serializes `corroboration_score` and `corroboration_level`
  - `_build_memo_markdown` shows corroboration level per WATCH candidate; adds Corroboration Summary section

- `discovery/__init__.py`: exports `CorroborationResult`, `compute_corroboration`, `CORROBORATION_MET_THRESHOLD`

Constraints preserved:
- All discovery remains sandbox/research-lane only; no official artifacts modified
- No buy/sell status produced; CandidateStatus values unchanged
- `corroboration_required=True` remains hardcoded on every candidate
- Governance flags (`discovery_only`, `sandbox_only`, `observe_only`) unchanged

Tests: `tests/discovery/test_corroboration.py` — 58 new tests; 229 total across discovery test suite; 4354 total suite passing

---

### GUI Discovery Approval Workflow (Complete)

Sandbox-only operator audit layer allowing research review decisions for discovery candidates.

New module `portfolio_automation/discovery/approval_workflow.py`:
- `ApprovalDecision` enum — 4 allowed values: `approve_for_research_review`, `keep_watching`, `reject_candidate`, `needs_more_evidence`; no buy/sell/actionable/promoted/validated
- `DiscoveryApprovalDecision` dataclass — hardcoded governance flags: `observe_only=True`, `sandbox_only=True`, `no_trade=True`, `no_official_promotion=True`
- `make_approval_decision()` — validated factory; rejects forbidden decision values
- `record_approval_decision()` — append-only write to `outputs/sandbox/discovery/approval_decisions.jsonl`; validates namespace via `data_governance.validate_output_path` before any write
- `load_approval_decisions()` — reads JSONL; tolerates malformed lines; returns `[]` on missing file
- `build_approval_summary()` — in-memory summary with counts, per-symbol latest decision, governance flags

Changes to existing modules:

- `gui_operator_data.py`:
  - Added `DISCOVERY_APPROVAL_DECISIONS_RELATIVE_PATH` constant
  - Added `load_discovery_approval_decisions(root)` — JSONL loader with malformed-line and tampered-record tolerance
  - Added `load_discovery_approval_summary(root)` — builds summary from decisions; always includes governance flags
  - `load_discovery_sandbox_status()` now returns `approval_decisions` and `approval_summary` keys, loads rejected runtime artifacts from `candidates`, and remains backward compatible with old `rejected_candidates` fixtures

- `gui/app.py`:
  - `_render_discovery_sandbox_tab()` extended with a "Sandbox Review Decisions" section
  - Shows existing approval summary (decision counts, per-symbol latest decision table)
  - For each WATCH candidate: expander with corroboration details, evidence snippets, selectbox, reason text area, and "Record sandbox review decision" button
  - Button calls `record_approval_decision()` — writes to sandbox JSONL only; no official mutations
  - Flash messages via `st.session_state`; rerun on submission

- `discovery/__init__.py`: exports `ApprovalDecision`, `DiscoveryApprovalDecision`, `make_approval_decision`, `record_approval_decision`, `load_approval_decisions`, `build_approval_summary`

Constraints preserved:
- All approval artifacts remain in `outputs/sandbox/discovery/` only
- No official watchlist or portfolio mutation
- No buy/sell/actionable/promoted/validated decisions ever written
- Governance flags validated before every write; any tampering raises ValueError
- Read-side loaders skip tampered valid JSONL records with forbidden decisions or missing/false governance flags
- Approval summary is in-memory only; no separate `approval_summary.json` artifact is written
- No AI/LLM calls, no external API calls, no auto-trading

Tests:
- `tests/discovery/test_approval_workflow.py` — approval model, append-only JSONL, tampered-load filtering
- `tests/test_gui_discovery_approval.py` — GUI loaders, approval summaries, rejected runtime key compatibility
- 301 total across discovery test suite after initial implementation; cleanup hardening adds read-side regression coverage

---

### Repo Cleanup Checkpoint (Complete)

Consolidation pass before `daily_memo_discovery_section`.

Scope:
- Audited discovery approval, corroboration, data governance, AI budget, confidence calibration, GUI health panels, artifact contracts, roadmap, and agent state for docs-to-runtime drift.
- Preserved scoring, allocation, recommendation, discovery promotion, run-mode permissions, artifact schemas, and advisory-only boundaries.

Fixes:
- Clarified `rejected_candidates.json` uses top-level `candidates` at runtime.
- Clarified approval JSONL read-side tamper filtering and in-memory-only approval summary behavior.
- Removed stale approval summary artifact constants.
- Added `docs/REPO_CLEANUP_AUDIT.md` with active files, suspected legacy/dead candidates, cleanup backlog, and safety confirmation.

Cleanup checkpoint preceded `daily_memo_discovery_section`. The memo section is now complete; current next official step is `historical_replay_backtest_for_discovery_candidates`.

---

### Daily Memo Discovery Section (Complete)

Adds a **DISCOVERY RESEARCH [Sandbox Only]** section to `outputs/latest/daily_memo.txt` and `outputs/latest/daily_memo.md`. The section is produced by `generate_daily_memo()` in `watchlist_scanner/daily_memo.py` and reads sandbox artifacts only.

**Scope:**

- New helpers in `daily_memo.py`:
  - `_load_discovery_approval_decisions(path)` — reads and validates approval JSONL; skips tampered records via `is_valid_loaded_approval_record()`
  - `_load_discovery_sandbox_data(root_path)` — loads all four sandbox artifacts; returns `None` if all are empty/missing
  - `_build_discovery_section(data)` — plain-text section builder
  - `_build_discovery_section_md(data)` — Markdown section builder
- `build_daily_memo` and `build_daily_memo_md` accept optional `discovery_data` kwarg; section appears before the footer if data is available
- `generate_daily_memo` loads discovery data non-blocking (exception → section omitted, memo still complete)

**Safety constraints:**

- Never writes to sandbox or produces separate discovery artifacts — sandbox-read-only consumer rendered into existing daily memo outputs
- Approval records validated on load; buy/sell/actionable/promoted/validated rejected defense-in-depth in section builders too
- Missing or corrupt artifact files handled gracefully
- No AI/LLM calls, no external API calls
- Sandbox-only disclaimer mandatory on every render

**Artifacts written:** `outputs/latest/daily_memo.txt`, `outputs/latest/daily_memo.md` (existing paths — no new namespaces)

**Artifacts read (sandbox, read-only):**
- `outputs/sandbox/discovery/emerging_candidates.json`
- `outputs/sandbox/discovery/rejected_candidates.json`
- `outputs/sandbox/discovery/discovery_memory.json`
- `outputs/sandbox/discovery/approval_decisions.jsonl`

**Tests:** 57 new tests (115 total in `tests/test_daily_memo.py`).

Next step after this was: `historical_replay_backtest_for_discovery_candidates` — now complete (see below).

---

### Historical Replay Backtest for Discovery Candidates (Complete)

**Scope:** Sandbox-only replay evaluation framework for assessing whether discovery candidates have predictive value over time. No official portfolio mutations, no trade execution, no external API calls.

**New module:** `portfolio_automation/discovery/discovery_replay.py`

**Public functions:**
- `run_discovery_replay(...)` — Full orchestration pipeline
- `load_discovery_replay_inputs(...)` — Load sandbox artifacts for replay
- `evaluate_discovery_candidate_outcomes(...)` — Compute outcome metrics from injected price data
- `summarize_discovery_replay_results(...)` — Aggregate by status, corroboration, approval decision, risk
- `write_discovery_replay_report(...)` — Write sandbox artifacts (DISCOVERY/BACKTEST modes only)

**Output artifacts (all `outputs/sandbox/discovery/`):**
- `replay_results.json` — Summary JSON with governance flags and aggregates
- `replay_results.md` — Markdown report with disclaimer and all comparison sections
- `replay_candidate_outcomes.jsonl` — Per-candidate outcome records (overwritten per run)

**Safety constraints:**
- Never produces BUY/SELL/ACTIONABLE/PROMOTED/VALIDATED statuses
- Forbidden-status candidates filtered at evaluation time
- Run mode governance enforced: DISCOVERY and BACKTEST modes only
- Approval decisions validated via `is_valid_loaded_approval_record()` before use
- Deterministic — same inputs produce same metrics; no randomness, no external calls

**Metrics implemented:**
- Per-window (1, 3, 5, 10, 20 days): `forward_return_pct`, `direction_correct`, `max_drawdown_pct`, `max_runup_pct`, `insufficient_data`
- Aggregates: WATCH vs DISCOVERED, high vs low corroboration, all four approval decisions, risk-flagged vs non-risk, rejected candidate review

**Tests:** 79 tests in `tests/discovery/test_discovery_replay.py`; 405 total in `tests/discovery/`

**Updated:** `portfolio_automation/discovery/__init__.py` (5 new public exports), `docs/DISCOVERY_ENGINE.md`, `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `.agent/project_state.yaml`, `.agent/phase_status.yaml`

**Limitation:** No live price data is available in the repo; all outcome metrics require injected `price_outcomes` dict. An operator must supply historical prices to get resolved candidate metrics. Candidates without price data are marked `insufficient_data=True` and excluded from aggregate calculations.

**Execution order:** Codex review for Discovery Replay, then `email_memo_sender_delivery_track`, then `fmp_news_intelligence_layer`, then `discovery_news_integration`, then `daily_weekly_monthly_ai_market_narratives`, then `news_evidence_layer_for_decision_engine`, then `automatic_promotion_governance_layer` (replaces `manual_promotion_proposal`).

---

### Email Memo Sender / Delivery Track (Complete)

**Scope:** Delivers existing daily memo files (`outputs/latest/daily_memo.txt` / `.md`) by email.  Disabled by default.  No scoring, allocation, recommendations, discovery, or portfolio state changes.

**Module:** `portfolio_automation/memo_email_sender.py`

**Public functions:**

| Function | Description |
|---|---|
| `load_memo_email_config(env)` | Load config from env vars; password never logged |
| `build_memo_email_message(...)` | Build `EmailMessage` with plain-text body + Markdown attachment |
| `send_daily_memo_email(config, message)` | Send via SMTP; dry-run skips SMTP; sanitized errors on failure |
| `write_memo_delivery_status(data, base_dir)` | Write per-run status to `OutputNamespace.LATEST` |
| `append_memo_delivery_log(entry, base_dir)` | Append audit entry to `OutputNamespace.POLICY` JSONL |
| `run_memo_email_delivery(*, run_id, base_dir, ...)` | Full delivery pipeline; always returns status dict |

**Config:** 14 env vars; all optional with safe defaults.  Feature disabled when `MEMO_EMAIL_ENABLED` is not `1`.

**Delivery artifacts:**

- `outputs/latest/memo_delivery_status.json` (LATEST namespace)
- `outputs/policy/memo_delivery_log.jsonl` (POLICY namespace, append-only)

**Idempotency:** Checks `memo_delivery_log.jsonl` for same `run_id` or `memo_date` with `sent=true`.  Skips duplicate sends unless `MEMO_EMAIL_FORCE_RESEND=1`.  Dry-run does not create idempotency records.

**Integration:**

- Non-blocking call added to `watchlist_scanner/daily_memo.py`'s `_main()` after memo files are written
- Non-blocking import + call added to `main.py`'s finalize section
- Loader `load_memo_delivery_status()` added to `gui_operator_data.py`

**Safety:**

- No trades, no portfolio mutations, no AI/LLM calls, no market-data API calls
- Password never appears in artifacts, repr, or logs
- `observe_only: true`, `no_trade: true` hard-coded in every artifact
- Non-blocking by default (`MEMO_EMAIL_STRICT_FAILURE=0`)

**CLI:** `python -m portfolio_automation.memo_email_sender --dry-run / --send / --force-resend`

**Tests:** `tests/test_memo_email_sender.py` — 81 tests across 9 test classes.  All SMTP interactions mocked.

**Files created:** `portfolio_automation/memo_email_sender.py`, `tests/test_memo_email_sender.py`

**Files modified:** `watchlist_scanner/daily_memo.py`, `main.py`, `gui_operator_data.py`, `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/PIPELINE_RUNBOOK.md`, `docs/roadmap.md`, `.agent/project_state.yaml`, `.agent/phase_status.yaml`

---

### FMP News Intelligence Layer (Complete)

**Scope:** Observe-only, rules-first evidence foundation. Ingests raw FMP news articles and emits structured evidence packets for official holdings, watchlist symbols, ETFs/sectors/themes, and sandbox discovery candidates. No BUY/SELL/HOLD outputs, no official watchlist mutation, no candidate promotion, no AI/LLM calls.

**Module:** `portfolio_automation/news/fmp_news_intelligence.py`

**Public functions:**

| Function | Description |
|---|---|
| `normalize_news_articles(raw_articles)` | Normalize raw FMP-style dicts into `NormalizedArticle` objects |
| `dedupe_news_articles(articles)` | Remove duplicates; sort newest-first |
| `extract_news_entities(article)` | Deterministic ticker extraction (source-provided, cashtag, parenthetical, alias map) |
| `classify_news_themes(article)` | Keyword scoring across 16 themes |
| `build_news_evidence_packets(articles, holdings, watchlist, discovery_candidates)` | Group evidence by ticker; assign evidence lanes |
| `write_news_intelligence_report(base_dir, raw_articles, ...)` | Full pipeline + artifact writes |
| `run_fmp_news_intelligence(raw_articles, ...)` | Top-level orchestrator |

**Evidence lanes:**

- `official_monitoring` — tickers in current holdings or official watchlist
- `sandbox_discovery_research` — all other tickers (not promoted)

**Artifacts produced:**

- `outputs/latest/news_intelligence.json` (LATEST namespace)
- `outputs/latest/news_intelligence.md` (LATEST namespace)
- `outputs/sandbox/discovery/news_candidate_evidence.json` (SANDBOX, when sandbox packets exist)

**Safety:** `observe_only: true`, `no_trade: true`, `not_recommendation: true` hardcoded everywhere. No allocation, scoring, recommendation, or decision-engine changes.

**Tests:** `tests/test_fmp_news_intelligence.py` — 91 tests across 8 test classes. No live API required.

**Files created:** `portfolio_automation/news/__init__.py`, `portfolio_automation/news/fmp_news_intelligence.py`, `tests/test_fmp_news_intelligence.py`, `docs/NEWS_INTELLIGENCE.md`

**Files modified:** `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/roadmap.md`, `.agent/project_state.yaml`, `.agent/phase_status.yaml`

**Consumed by future phases:**
- `discovery_news_integration` — evidence packets feed into discovery candidate scoring
- `daily_weekly_monthly_ai_market_narratives` — themes/evidence as narrative context
- `news_evidence_layer_for_decision_engine` — evidence attached to decision plan entries

---

### Discovery News Integration (Complete)

**Scope:** Enriches sandbox discovery candidates with structured news evidence from the FMP News Intelligence layer. Sandbox-only, observe-only. No official state mutation, no candidate promotion, no BUY/SELL/HOLD outputs.

**Module:** `portfolio_automation/discovery/news_integration.py`

**Public functions:**

| Function | Description |
|---|---|
| `load_news_intelligence(base_dir)` | Load `news_intelligence.json` safely |
| `load_news_candidate_evidence(base_dir)` | Load sandbox news evidence safely |
| `load_emerging_candidates(base_dir)` | Load emerging discovery candidates |
| `load_rejected_candidates(base_dir)` | Load rejected discovery candidates |
| `match_evidence_to_candidates(packets, candidates)` | Match by ticker/related_tickers |
| `enrich_candidates(candidates, matched, all_packets)` | Build enriched records |
| `build_integration_summary(enriched, run_mode, ts)` | Markdown summary with disclaimer |
| `write_news_integration_artifacts(base_dir, enriched, md, mode, id)` | Write sandbox artifacts |
| `run_discovery_news_integration(base_dir, run_mode, run_id, dry_run)` | Orchestrator |

**Enrichment:** Matches evidence by ticker, aggregates themes/risk/catalyst flags, computes `news_relevance_score`, `corroboration_news_score`, and `news_context` (`research_supported`/`research_caution`/`research_neutral`/`no_news`).

**News-only tickers:** Tickers in news evidence without discovery candidates added as `candidate_status: "news_only"` — needs corroboration, never auto-promoted.

**Run mode governance:** Only DISCOVERY and BACKTEST may write sandbox artifacts. Other modes treated as dry_run.

**Artifacts produced:**
- `outputs/sandbox/discovery/news_enriched_candidates.json` (SANDBOX)
- `outputs/sandbox/discovery/news_integration_summary.md` (SANDBOX)

**Safety:** `observe_only: true`, `no_trade: true`, `not_recommendation: true`, `discovery_only: true` hardcoded. No official namespace writes. No PROMOTED/VALIDATED/ACTIONABLE/BUY/SELL statuses.

**Tests:** `tests/discovery/test_news_integration.py` — 72 tests across 7 test classes.

**Files created:** `portfolio_automation/discovery/news_integration.py`, `tests/discovery/test_news_integration.py`, `docs/DISCOVERY_NEWS_INTEGRATION.md`

**Files modified:** `portfolio_automation/discovery/__init__.py`, `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/roadmap.md`, `.agent/project_state.yaml`, `.agent/phase_status.yaml`

---

### Daily / Weekly / Monthly AI Market Narratives (Complete)

**Scope:** Observe-only layer that turns existing structured artifacts into daily, weekly, and monthly operator-readable market narratives. No recommendations, no official state mutation, no trading commands.

**Module:** `portfolio_automation/market_narratives.py`

**Public functions:**

| Function | Description |
|---|---|
| `load_all_inputs(base_dir)` | Load all input artifacts safely; degrades on missing/malformed |
| `validate_narrative_safety(text)` | Check text for prohibited instruction patterns |
| `build_market_narrative_report(period, inputs, base_dir)` | Build structured `MarketNarrativeReport` |
| `render_market_narrative_markdown(report)` | Render report as Markdown |
| `write_market_narrative_report(period, report, base_dir)` | Write JSON + MD to LATEST namespace |
| `run_market_narratives(base_dir, periods, write_files)` | Top-level orchestrator |

**Data types:** `NarrativeInputSummary`, `NarrativeTheme`, `NarrativeRisk`, `NarrativeCatalyst`, `NarrativeDiscoveryContext`, `MarketNarrativeReport`

**Narrative periods:** daily (what changed, top themes, risks/catalysts), weekly (persistent themes, discovery context, operator review), monthly (regime/theme context, system health, review areas)

**Safety validator:** `validate_narrative_safety()` scans generated text for 19 prohibited instruction patterns (buy now, sell now, execute trade, promote candidate, etc.) and returns detected violations.

**Artifacts produced** (all LATEST namespace):
- `outputs/latest/market_narrative_daily.json` + `.md`
- `outputs/latest/market_narrative_weekly.json` + `.md`
- `outputs/latest/market_narrative_monthly.json` + `.md`

**AI support:** Deferred. All generation is deterministic keyword/rules-based.

**Safety:** `observe_only: true`, `no_trade: true`, `not_recommendation: true` hardcoded. No POLICY/PORTFOLIO/SANDBOX writes. Discovery context always sandbox-labeled with disclaimer.

**Tests:** `tests/test_market_narratives.py` — 79 tests across 9 test classes.

**Files created:** `portfolio_automation/market_narratives.py`, `tests/test_market_narratives.py`, `docs/MARKET_NARRATIVES.md`

**Files modified:** `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/roadmap.md`, `.agent/project_state.yaml`, `.agent/phase_status.yaml`

---

### News Evidence Layer (Complete)

**Scope:** Capped, observe-only layer that converts news, narrative, and discovery evidence into decision-engine-adjacent context. Cannot override decisions, scoring, allocation, recommendations, or watchlists. Hard influence cap: `context_only`.

**Module:** `portfolio_automation/news_evidence_layer.py`

**Public functions:**

| Function | Description |
|---|---|
| `load_all_inputs(base_dir)` | Load all input artifacts safely; degrades on missing/malformed |
| `build_news_evidence_layer_report(inputs, base_dir)` | Build structured `NewsEvidenceLayerReport` |
| `render_news_evidence_markdown(report)` | Render Markdown report |
| `write_news_evidence_layer_report(report, base_dir)` | Write JSON + MD to LATEST (sanitizes & validates) |
| `run_news_evidence_layer(base_dir, write_files)` | Top-level orchestrator |
| `validate_news_evidence_safety(value)` | Walk and detect prohibited phrases |
| `sanitize_news_evidence_text(value)` / `sanitize_label(value)` / `sanitize_nested_news_evidence_payload(payload)` | Sanitizers |

**Data types:** `NewsEvidenceInputSummary`, `TickerNewsEvidence`, `DecisionNewsContext`, `NewsRiskEvidence`, `NewsCatalystEvidence`, `NewsEvidenceLayerReport`, `UnsafeNewsEvidenceArtifactError`

**Evidence classification:**
- `evidence_strength`: `none` / `weak` / `moderate` / `strong` (by article count + source diversity)
- `context_effect`: `informational` / `risk_context` / `catalyst_context` / `confidence_context`
- No `BUY`/`SELL`/`HOLD`/`PROMOTED`/`VALIDATED`/`ACTIONABLE` values are ever emitted

**Sanitizer:** Same three-layer pattern as market narratives — label sanitization, full-payload sanitization, pre-write JSON+Markdown validation. Blocks writes via `UnsafeNewsEvidenceArtifactError` if prohibited language remains.

**Artifacts produced** (all LATEST namespace):
- `outputs/latest/news_evidence_layer.json`
- `outputs/latest/news_evidence_layer.md`

**Safety:** All seven safety flags hardcoded (`observe_only`, `no_trade`, `not_recommendation`, `no_decision_override`, `no_score_mutation`, `no_allocation_mutation`, `no_watchlist_mutation`). No POLICY/PORTFOLIO/SANDBOX writes. Decision actions and reasons copied read-only.

**Tests:** `tests/test_news_evidence_layer.py` — 74 tests across 8 test classes including adversarial input protection and no-mutation boundary verification.

**Files created:** `portfolio_automation/news_evidence_layer.py`, `tests/test_news_evidence_layer.py`, `docs/NEWS_EVIDENCE_LAYER.md`

**Files modified:** `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/roadmap.md`, `.agent/project_state.yaml`, `.agent/phase_status.yaml`

---

### Automatic Promotion Governance Layer (Complete — replaces Manual Promotion Proposal)

**Scope:** Deterministic, observe-only, sandbox-only governance layer that automatically evaluates discovery candidates against explicit gates and graduates qualified candidates to MONITOR. Replaces the previously planned `manual_promotion_proposal` step. No BUY/SELL/HOLD/ACTIONABLE/PROMOTED/VALIDATED/APPROVED/TRADE/RECOMMENDATION outputs. No portfolio, watchlist, scoring, allocation, recommendation, or decision mutation.

**Module:** `portfolio_automation/discovery/automatic_promotion_governance.py`

**Public functions:**

| Function | Description |
|---|---|
| `load_automatic_promotion_inputs(base_dir)` | Load all input artifacts safely |
| `evaluate_candidate_promotion(candidate, context, gates, now)` | Per-candidate gate evaluation |
| `build_automatic_promotion_report(inputs, run_mode, run_id, gates)` | Build full structured report |
| `render_automatic_promotion_markdown(report)` | Markdown summary |
| `write_automatic_promotion_report(report, base_dir, run_mode, run_id)` | Write 3 sandbox artifacts (sanitized + validated) |
| `run_automatic_promotion_governance(...)` | Top-level orchestrator |
| `validate_automatic_promotion_safety(value)` / `sanitize_automatic_promotion_text(value)` / `sanitize_label(value)` / `sanitize_nested_automatic_promotion_payload(payload)` | Sanitizer + validator helpers |

**Data types:** `PromotionGates` (with `DEFAULT_GATES`), `PromotionEligibilityResult`, `PromotionDecision`, `AutomaticPromotionReport`, `UnsafeAutomaticPromotionArtifactError`, plus `ALLOWED_STATUSES` / `FORBIDDEN_STATUSES` frozensets.

**Allowed statuses:** `DISCOVERED`, `WATCH`, `MONITOR`, `REJECTED`, `EXPIRED`, `NEEDS_REVIEW`.
**Forbidden statuses (never emitted):** `BUY`, `SELL`, `HOLD`, `ACTIONABLE`, `PROMOTED`, `VALIDATED`, `APPROVED`, `TRADE`, `RECOMMENDATION`.

**Governance gates (conservative defaults):**
- `minimum_corrob_score: 0.65`, `minimum_source_diversity: 2`, `minimum_news_relevance: 0.4`
- `maximum_risk_flags: 2`, `stale_after_days: 30`
- `require_watch_status_for_monitor: True`, `require_persistence_for_monitor: True`
- `block_rejected_candidates: True`, `block_forbidden_statuses: True`

**State machine:** WATCH + all gates pass → MONITOR; soft-gate-only failures → NEEDS_REVIEW; risk overflow or rejected list or forbidden upstream status → REJECTED; stale → EXPIRED; otherwise hold prior status.

**Run mode governance:** Only `DISCOVERY` and `BACKTEST` may write. Other modes return as dry-run.

**Sanitizer:** Same three-layer pattern as market narratives / news evidence layer — label sanitization, full-payload sanitization (including dict keys), pre-write JSON + Markdown + JSONL validation. Standalone whole-word action detection covers `buy`/`sell`/`hold`/`actionable`/`promoted`/`validated`/`approved`/`trade`/`recommendation`. Pure-action labels rewritten to `redacted_action_label_context_only`. Writer raises `UnsafeAutomaticPromotionArtifactError` if residual violations remain.

**Artifacts produced** (all SANDBOX namespace):
- `outputs/sandbox/discovery/automatic_promotion_candidates.json`
- `outputs/sandbox/discovery/automatic_promotion_decisions.jsonl` (append-only audit)
- `outputs/sandbox/discovery/automatic_promotion_summary.md`

**Tests:** `tests/discovery/test_automatic_promotion_governance.py` — 68 tests across 9 test classes (safety constants, input loading, sanitizer/validator, eligibility evaluator across all state transitions, report builder determinism, Markdown rendering, three-artifact writing, run-mode write blocking, JSONL append behavior, dry-run, adversarial input protection, no-mutation field invariants).

**Files created:** `portfolio_automation/discovery/automatic_promotion_governance.py`, `tests/discovery/test_automatic_promotion_governance.py`, `docs/AUTOMATIC_PROMOTION_GOVERNANCE.md`
**Files modified:** `portfolio_automation/discovery/__init__.py`, `docs/OUTPUT_ARTIFACT_CONTRACTS.md`, `docs/roadmap.md`, `.agent/project_state.yaml`, `.agent/phase_status.yaml`

**Manual Promotion Proposal:** Marked `superseded_by_automatic_promotion_governance_layer` in `.agent/phase_status.yaml`. If a future operator-driven UI workflow is desired, it should build on top of the sandbox automatic promotion artifacts rather than replacing them.

**Roadmap status after Codex review:** `automatic_promotion_governance_layer` is complete, VPS validated, and Codex-reviewed with no blocking implementation issues. The active agent state is now `awaiting_next_approved_roadmap_step`; the next implementation step should be selected by the user/GPT from the next approved roadmap options.

---

### GUI Operator Cockpit Redesign — first slice: Automatic Promotion Review Panel (Complete)

**Scope:** UI/UX layer only. No portfolio logic, discovery logic, scoring, allocation, recommendation, decision-engine, automatic-promotion-governance behavior, broker/API execution, auto-trading, or artifact schemas were changed. The cockpit reads existing sandbox artifacts and surfaces them in a card-based, beginner-friendly, expandable view.

**Step name:** `gui_operator_cockpit_redesign`
**Primary build slice:** `gui_automatic_promotion_review_panel`

**Module touchpoints:**

| File | Change |
|---|---|
| `gui_operator_data.py` | Added 3 new path constants, 3 new loaders, an aggregator, and wired `automatic_promotion` into `load_operator_dashboard_data()` |
| `gui/app.py` | Added 8 reusable UI helpers, added `page_automatic_promotion()`, registered `Automatic Promotion` in the nav `PAGES` list and router |
| `tests/test_gui_automatic_promotion.py` | New — 31 tests across 7 test classes |
| `docs/GUI_OPERATOR_COCKPIT.md` | New — purpose, page list, helpers, safety boundaries, future cockpit roadmap |
| `docs/roadmap.md` | This entry |
| `.agent/project_state.yaml` / `.agent/phase_status.yaml` | Updated |

**Reusable UI helpers added** (intended for use by future cockpit pages):

| Helper | Purpose |
|---|---|
| `render_status_badge(text, tone)` | Inline HTML badge |
| `render_metric_card(title, value, subtitle, badges)` | Card with label/value/subtitle/badges |
| `render_section_header(title, subtitle)` | Section header with caption |
| `render_empty_state(message, icon)` | Friendly empty-state info |
| `render_safety_flags(safety_flags, missing)` | Safety boundary panel — one badge per expected flag |
| `render_candidate_card(decision, key_prefix)` | Single candidate card with expander |
| `_status_tone(status)` / `_status_explanation(status)` | Maps status → tone / plain-English line |

All helpers reuse the existing `_operator_dashboard_css()` / `_badge()` / `_render_operator_card()` foundations so all cockpit pages share one visual language.

**Loaders added** (`gui_operator_data.py`):

| Loader | Returns |
|---|---|
| `load_automatic_promotion_candidates(root)` | `dict` with `available` flag |
| `load_automatic_promotion_summary_markdown(root)` | `str` (empty if missing) |
| `load_automatic_promotion_decisions(root)` | `list[dict]` (malformed lines skipped) |
| `load_automatic_promotion_data(root)` | Aggregator with stable shape; never raises; wired into `load_operator_dashboard_data` |

**Automatic Promotion Review page layout:**

1. Header + safety disclaimer
2. 6-card top metrics row: Total Reviewed, Moved to Monitor, Needs Review, Rejected, Expired, Safety Status
3. Safety Boundary panel — one badge per expected safety flag, with a warning if any are missing or False
4. "What does each status mean?" expander — plain-English explanations
5. Grouped candidate sections by status (MONITOR / NEEDS_REVIEW / REJECTED / EXPIRED) — each candidate is a card with an expander showing evidence score, corroboration, news relevance, source diversity, gates passed/failed, risk/catalyst flags, replay/memory/operator context, and raw JSON
6. Producer-rendered Markdown summary (collapsed by default)
7. Recent decisions audit log (last 50 JSONL records, collapsed by default)
8. Governance gates table (collapsed by default)
9. Footer with generated_at, run_mode, run_id, source artifact paths

**Safety confirmation:**
- GUI is strictly read-only; no writes, no portfolio/watchlist/allocation/scoring/recommendation/decision mutation
- No broker/API/auto-trading code
- No LLM/AI calls
- Aggregator never invents action labels; unknown statuses fall into the defensive `OTHER` bucket
- Cockpit helper text uses no trading-instruction language outside the fixed safety disclaimer — verified by automated test
- All 9 expected safety flags surfaced and re-checked at render time

**Tests:** `tests/test_gui_automatic_promotion.py` — 31 tests across 7 test classes (loader graceful degradation, valid parsing, aggregator stable shape, candidate grouping, safety flag detection, dashboard wiring, read-only invariants, content safety, GUI helper import safety).

**Future cockpit roadmap:** Same helpers can power a refreshed Dashboard landing page, a News Evidence Layer panel, a Market Narrative panel, and a unified Discovery Sandbox panel. None of these require backend changes.

---

### GUI Operator Cockpit Redesign — slice 2: Dashboard Landing Card Refresh (Complete)

**Scope:** UI/UX additive layer only. Adds a card-based "Cockpit Summary" grid at the top of the existing **Dashboard** page using the reusable helpers from slice 1. Every existing dashboard widget below the new summary remains untouched. No portfolio logic, scoring, allocation, recommendation, decision-engine, automatic-promotion-governance, or artifact-schema changes.

**Step name:** `gui_operator_cockpit_redesign`
**Slice:** `dashboard_landing_card_refresh`

**Module touchpoints:**

| File | Change |
|---|---|
| `gui_operator_data.py` | Added 2 new path constants (`NEWS_EVIDENCE_LAYER_RELATIVE_PATH`, `MARKET_NARRATIVE_DAILY_RELATIVE_PATH`), 2 new loaders (`load_news_evidence_layer`, `load_market_narrative_daily`), and wired both into `load_operator_dashboard_data()` |
| `gui/app.py` | Added `_render_cockpit_summary_grid(bundle)` helper; added single 2-line invocation at the top of `page_dashboard()` (before the existing system summary block); imported the two new loaders |
| `tests/test_gui_cockpit_summary.py` | New — 19 tests across 6 test classes |
| `docs/GUI_OPERATOR_COCKPIT.md` | Added "Dashboard cockpit summary (slice 2)" section + updated Future Cockpit Roadmap |
| `docs/roadmap.md` | This entry |
| `.agent/project_state.yaml` / `.agent/phase_status.yaml` | Updated |

**Cockpit Summary layout (8-card grid in 2 rows of 4):**

| # | Card | Source artifact |
|---|---|---|
| 1 | Portfolio Status | `outputs/latest/system_decision_summary.json` (`system_health`) |
| 2 | Today's Market Narrative | `outputs/latest/market_narrative_daily.json` (`top_headline`) |
| 3 | Decision Plan | `outputs/latest/decision_plan.json` (`decisions` length) |
| 4 | Data Quality | `outputs/latest/data_quality_report.json` (`overall_health`, `issues`) |
| 5 | News Evidence | `outputs/latest/news_evidence_layer.json` (`ticker_contexts` length) |
| 6 | Automatic Promotion | Aggregator: `monitor_count` + `needs_review_count` from sandbox artifacts |
| 7 | Memo Delivery | `outputs/latest/memo_delivery_status.json` (`sent`/`skipped`/`enabled`) |
| 8 | Safety Boundary | Fixed reminder ("Observe-only. No trades. No portfolio mutation.") |

Each card uses `render_metric_card(title, value, subtitle, badges)` + `render_status_badge(text, tone)` from slice 1. Tone is determined per-card by the artifact's health/availability values.

**Safety confirmation:**
- GUI remains strictly read-only; no writes, no portfolio/watchlist/allocation/scoring/recommendation/decision mutation
- No broker/API/auto-trading code
- No LLM/AI calls
- New loaders verified by test to not write or modify any files
- Cockpit summary helper uses no trading-instruction language (`buy now`, `sell now`, `execute trade`, etc.) outside the fixed Safety Boundary card's disclaimer wording — verified by automated test (`test_helper_avoids_forbidden_trading_language`)
- Existing dashboard widgets below the summary are not touched

**Tests:** `tests/test_gui_cockpit_summary.py` — 19 tests across 6 test classes (loader graceful degradation, valid parsing, aggregator wiring, cockpit helper source-level checks: defined / invoked / uses reusable components / renders 7 cards / avoids forbidden trading language, read-only invariants, tone logic).

**Future cockpit roadmap progress:**
- ✅ Slice 1: `gui_automatic_promotion_review_panel`
- ✅ Slice 2: `dashboard_landing_card_refresh`
- Slice 3 (candidate): `news_evidence_layer_panel`
- Slice 4 (candidate): `market_narrative_panel`
- Slice 5 (candidate): `unified_discovery_sandbox_panel`

---

## P&L Maximization Roadmap (2026-05-15)

**Spec:** `docs/superpowers/specs/2026-05-15-pnl-maximization-roadmap.md`
**Reference doc:** `docs/PNL_ADVISORS.md`
**Validation:** `python scripts/validate_pnl_advisors.py`

### Phase 1 — shipped 2026-05-15

| Module | Tests | Artifact |
|---|---|---|
| `portfolio_automation/exit_advisor.py` | 22 | `outputs/latest/exit_advisor.{json,md}` |
| `portfolio_automation/cash_deployment_plan.py` | 20 | `outputs/latest/cash_deployment_plan.{json,md}` |
| `portfolio_automation/correlation_risk_advisor.py` | 22 | `outputs/latest/correlation_risk_advisor.{json,md}` |

Also: decision_plan.json gained a top-level `portfolio_context` field
(additive) so cash_deployment_plan can read `total_portfolio_value`
without traversing per-decision `inputs_used`.

### Phase 2 — shipped 2026-05-15

| Module | Tests | Artifact |
|---|---|---|
| `portfolio_automation/earnings_gate.py` | 17 | `outputs/latest/earnings_gate.{json,md}` |
| `portfolio_automation/vol_regime_advisor.py` | 16 | `outputs/latest/vol_regime_advisor.{json,md}` |
| `portfolio_automation/tax_harvest_advisor.py` | 15 | `outputs/latest/tax_harvest_advisor.{json,md}` |

`earnings_gate` ships with `earnings_lookup=None` because no FMP-compliant
earnings-calendar endpoint is registered yet. The gate degrades to
`status="no_earnings_source"` and `gate="HOLD"`. Wire the lookup when the
endpoint exists.

### Phase 3 — shipped 2026-05-15

| Module | Tests | Artifact |
|---|---|---|
| `portfolio_automation/kelly_sizing_advisor.py` | 13 | `outputs/latest/kelly_sizing_advisor.{json,md}` |
| `portfolio_automation/alpha_attribution_report.py` | 12 | `outputs/latest/alpha_attribution_report.{json,md}` |

Both gated at ≥20 resolved decisions per group; below the gate they
degrade to `status="insufficient_data"`. They become informative as the
outcome history grows.

### Pipeline integration — shipped 2026-05-15

All eight advisors wired into `main.py` inside `_write_decision_engine_outputs`
after the existing observe-only layers (outcome tracker, triage, calibration,
performance attribution). Each advisor has an independent try/except so a
single failure cannot break any other layer.

### Phase 4 — NOT shipped (gated)

Each Phase 4 item modifies a CLAUDE.md-protected file and changes
semantics of a score field listed under `protected_semantics` in
`.agent/project_state.yaml`:

- **P4.1** — calibrated sizing multipliers in `conviction.py`
- **P4.2** — decision engine consumes exit_advisor as a downgrade source
  (`portfolio_automation/decision_engine.py`)
- **P4.3** — multi-timeframe trend confirmation in scanner signal_score
  (`watchlist_scanner/scanner.py` + `scoring.py`)
- **P4.4** — regime-aware allocation feedback from vol_regime_advisor
  (`allocation_engine.py`)

Each item requires a per-item explicit user approval before scope unlock.

Phase 4 status update (2026-05-20): all four items shipped under explicit
per-item approval — P4.1 (commit 593c10cd), P4.4 (commit 428c1a54), P4.2
(commit 3f2fa125), P4.3 (commit 5cf0e9e2).

## Tactical Retune + Observability v2 (2026-05-18..20)

A two-session hardening cycle landed across commits `4223654c..9ae0c45a`
(14 commits). All changes are additive / observe-only; no scoring or decision
semantics changed.

### Allocation Gauge Tactical Retune (Complete — 2026-05-18)

Operator-approved gauge-only retune across five surfaces (allocation_engine,
portfolio_construction, decision_engine absolute cap, cash_deployment_plan,
allocation_preview). Sizing dollars roughly double; conviction / band /
Kelly / regime machinery is unchanged. See
`docs/CHANGELOG_DECISIONS.md#allocation-gauge-tactical-retune` and
`docs/ALLOCATION_POLICY.md`.

### Structural Caps Widened (Complete — 2026-05-18)

`config.json:growth_mode.concentration_cap 0.40→0.60`,
`leverage_cap 0.15→0.25`. Cap-breach SELL rules are unchanged — only the
thresholds moved.

### ml_advisor Enabled (Complete — 2026-05-18)

`config/base.json:ml_advisor.enabled false→true`. Now exceeds
`MIN_RECORDS_FOR_HIGH_CONFIDENCE = 30` thanks to the resolver fixes.

### Safe-Wrapper 17-Stage Build-Out (Complete — 2026-05-18..20)

`scripts/run_daily_safe.sh` expanded from 1 stage to 17:
`0, 1, 2, 3, 4, 5, 6, 7, 7b, 7c, 7d, 7e, 8, 8b, 9, 9b, 10, 11`. Stage 1
(main pipeline) is the only fail-fast stage; everything after it is
non-blocking. See `docs/PIPELINE_RUNBOOK.md` for the full table.

### Observability v2 Modules (Complete — 2026-05-18..19)

Six new observe-only modules under `portfolio_automation/`:

- `risk_delta_advisor.py` — exposure vs caps + 1d 95% VaR
- `retune_impact_tracker.py` — gauge fingerprint ledger + outcome attribution
- `fmp_budget_telemetry.py` — daily FMP call usage + news outcome
- `daily_run_status.py` — official-lane wrapper status
- `resolution_due_probe.py` — stuck-resolution surface
- `news/run_news_intelligence.py` — pipeline-facing news runner

See `docs/ARCHITECTURE.md` and `docs/OUTPUT_ARTIFACT_CONTRACTS.md`.

### GUI v2 Risk & Impact Tab (Complete — 2026-05-19)

New route `GET /risk-impact` consolidating the four v2 artifacts. New
Jinja filter `risk_severity`. Today page links to the panel via a
clickable Risk & Impact summary card.

### Outcome Resolver Fixes (Complete — 2026-05-19)

Three coordinated fixes — FMP fallback in `outcome_evaluator`, natural
resolution in `ml_history.auto_resolve_pending_records`, and FMP price
augmentation in `decision_outcome_tracker`. See `docs/FEEDBACK_LOOP.md` and
`docs/EVALUATION_AND_LEARNING_LOOP.md`.

### Daily Memo Verdict + Pulse + Risk Delta + Advisor Stack (Complete — 2026-05-18..20)

Memo now opens with Today's Verdict (mood ladder), shows a stale-data banner
when artifacts are ≥2 days old, surfaces Portfolio Pulse, a Risk Delta block,
and an Advisor Stack with FMP budget + retune impact 1d hit-rate lines.
Top Decisions reason regex compacted; Top Movers <4 collapses to one line;
Decision Hit Rate dedups adjacent identical entries; Discovery Research has
a single-line empty state; System / Data Health rolls into counts. See
`docs/daily_memo.md`.

Tests: 5949 → 6056 passed across the cycle.

Next official step: `observe_and_iterate` — let resolved-outcome history
accumulate so `retune_impact_tracker.outcome_attribution` becomes
statistically meaningful. No new module work is queued.

---

## Known Issues (Open Defects)

Tracked open defects observed during operation. These do not change
`next_official_step` (still `observe_and_iterate`); they are follow-up
fixes to surface, not roadmap steps.

### KI-1 — theme_engine OpenAI calls are not metered (discovery telemetry false-zero)

- **Surfaced:** 2026-06-01 inaugural monthly-tool-analysis (AMBER), `portfolio-discovery-health`.
- **Symptom:** `discovery_pulse_status.openai_cost_usd_month` and the FMP
  discovery cost telemetry report **0.0**, falsely implying no discovery spend.
- **Root cause:** `watchlist_scanner/theme_engine.py` never calls
  `record_ai_usage` around its OpenAI call. Smoking gun: **zero** `theme_engine`
  events in `outputs/policy/ai_usage_events.jsonl`.
- **True state:** real OpenAI call confirmed (latency ~4153ms, key filled);
  true spend is in `ai_budget_summary.json` (~$0.0007/mo, negligible).
- **Follow-up:** wire `record_ai_usage_event` into `theme_engine.py` around
  the OpenAI call (task_name `theme_engine.daily`). Until then, read true
  OpenAI cost from `ai_budget_summary.json`, not the discovery pulse status.
- **Severity:** low (telemetry-only; no decision/spend impact).

### KI-2 — FMP scanner still in fallback (tier_b.evidence_count=0)

- **Surfaced:** 2026-06-01 inaugural monthly-tool-analysis (AMBER), `portfolio-discovery-health`.
- **Symptom:** `tier_b.evidence_count=0` and `fmp_calls_month=0` despite a
  fresh `top100_watchlist.json`; the scanner path reports `fmp_succeeded:false`,
  collapsing the active universe toward the static fallback set.
- **Follow-up:** verify `FMP_API_KEY` and confirm `scanner/candidate_scanner.py`
  is not silently catching the FMP failure (recurring `fmp_succeeded:false`).
- **Severity:** medium (universe coverage degraded to fallback).

### Self-healing (tracked, not a defect)

- **Learning-loop outcome-maturation gap.** `pattern_learning.py:_match_outcome`
  joins forward-only (`signal_time >= snapshot_date`); the earliest snapshot is
  2026-05-29, so the 594 mature outcomes dated before that are structurally
  unreachable and `pattern_efficacy_monthly` reads `resolved_1d=0` / null
  `vs_baseline_pp` for every tag. **Not a code bug** — first non-null 1d efficacy
  expected ~2026-06-05, 7d ~2026-06-09. A resolve-then-attribute back-join
  (via `signal_outcomes` tags) is noted as a design enhancement to surface
  efficacy earlier during each new gauge era.
