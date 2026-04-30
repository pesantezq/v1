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

## Post-Phase-0 Next Steps

### Confidence Calibration Feedback Loop

- Wire resolved AI decision validation outcomes into calibration data
- Track prediction accuracy per signal category and decision type
- Tune confidence floors in `signal_registry.yaml` after evidence accumulates
- Gate calibration updates at 20 resolved decisions (existing rule)

### Discovery Engine Foundation

- Define discovery pipeline that consumes `discovery_only` signals from the registry
- Route `STRONG_MOVE_UP`, `VOLUME_SPIKE`, `BREAKOUT_PROXY` through a separate
  corroboration step before they become actionable candidates
- Keep discovery artifacts in `OutputNamespace.SANDBOX` until corroboration passes

### GUI Data Quality + AI Budget Panels

- Add a Data Quality card to the GUI Decision Center's System/Data Health section
  consuming `outputs/latest/data_quality_report.json`
- Add an AI Budget card consuming `outputs/latest/ai_budget_summary.json`
- Both panels are read-only; no new decision logic introduced

### Instrument AI Call Sites

- Add `record_ai_usage_event` calls after each LLM call in
  `decision_explainer.py` and `ai_decision_validator.py`
- Use actual token counts from API responses (not estimates)
- Gate with `with_ai_budget` context manager when hard enforcement is desired
