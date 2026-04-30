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
Additive and backward-compatible. 50 tests added.
See `docs/SIGNAL_REGISTRY.md`.

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

## Next Implementation Step

Decide which read-only downstream surface should consume `decision_explanations.*` first, while keeping `decision_plan.json` as the decision source of truth and preserving additive-only behavior.
