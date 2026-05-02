# Roadmap

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

Next official step remains `daily_memo_discovery_section`.

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

- Never writes to sandbox or any namespace — read-only consumer
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

Next official step: `historical_replay_backtest_for_discovery_candidates`.
